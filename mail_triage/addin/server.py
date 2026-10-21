#!/usr/bin/env python3
"""Outlook add-in server: serves the task pane on https://localhost and triages messages locally.

The add-in (static/) runs inside Outlook with the user's own access; no Entra app registration or
Graph consent is involved. It reads a message's internet headers (when Outlook provides them) and
body and POSTs them to this server on localhost only. The server normalizes them in memory with
Mail Triage's own code, decides with the normal engine (teachings, rules, sender memory, local
model, policy) and records the decision in the local history, like a mail-client scan.

Logs never contain subjects, addresses, bodies or message ids. Start it with
``./triage addin install`` (background service) or ``./triage addin run`` (foreground).
See docs/OUTLOOK_ADDIN.md.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import struct
import subprocess
import sys
import zlib

from . import certs
from .. import config as configmod, privacy, report
from ..backends.base import BackendError
from ..connectors.base import ConnectorError, Mailbox, MessageInfo
from ..connectors.outlook.common import PROVENANCE, normalize_source
from ..engine import Engine
from ..mime import parse_message, provenance_value
from ..store import Store, message_key

PORT = 48765
ORIGIN = f"https://localhost:{PORT}"
STATIC = Path(__file__).resolve().parent / "static"
FILES = {"/manifest.xml": "application/xml", "/manifest-readonly.xml": "application/xml",
         "/taskpane.html": "text/html; charset=utf-8",
         "/taskpane.js": "text/javascript; charset=utf-8"}
MAX_BODY = 4_000_000
# Office.js on Mac also loads MicrosoftAjax from ajax.aspnetcdn.com, and Office.js in Outlook on the web
# uses eval internally (both observed via CSP reports). Our own scripts never evaluate strings.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-eval' https://appsforoffice.microsoft.com "
       "https://ajax.aspnetcdn.com; "
       "connect-src 'self' https://appsforoffice.microsoft.com; img-src 'self' data:; "
       "style-src 'self' 'unsafe-inline'; frame-src https://appsforoffice.microsoft.com; base-uri 'none'; "
       "report-uri /csp-report")
CSP_HEADER = "Content-Security-Policy"  # violations are still reported (host + directive only)


def png(size: int) -> bytes:
    """Solid-colour square icon, generated so no binary assets live in the repository."""
    row = b"\x00" + bytes((0x2B, 0x6C, 0xB0)) * size
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * size)) + chunk(b"IEND", b""))


def build_source(payload: dict) -> str:
    """Office.js fields → Mail Triage structured source (transport headers kept, not original MIME)."""
    fields = {name: payload.get(name, "") for name in ("headers", "body", "sender", "subject", "internetMessageId")}
    if not all(isinstance(value, str) for value in fields.values()):
        raise ConnectorError("invalid payload")
    source = normalize_source(headers=fields["headers"], sender=fields["sender"], subject=fields["subject"],
                              body=fields["body"], html=bool(payload.get("html") is True),
                              message_id=fields["internetMessageId"])
    return source


def inspect(source: str) -> dict:
    """Structural facts only."""
    mail = parse_message(source)
    headers = mail["headers"]
    return {
        "parsed": True,
        "headers_available": headers.get(PROVENANCE.lower()) == provenance_value("structured-with-headers"),
        "received": "received" in headers,
        "authentication_results": "authentication-results" in headers,
        "message_id": bool(mail["message_id"]),
        "body": bool(mail["body"].strip()),
    }


def safe_text(value, limit: int = 40) -> str:
    return "".join(c for c in str(value) if c.isalnum() or c in ".-_ :?")[:limit]


def timing_text(timing) -> str:
    if not isinstance(timing, dict):
        return "timing=?"
    parts = []
    for key, value in list(timing.items())[:8]:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parts.append(f"{safe_text(key, 16)}={int(value)}ms")
    return "timing " + (" ".join(parts) or "?")


def client_facts(payload: dict) -> str:
    client = payload.get("client") if isinstance(payload.get("client"), dict) else {}
    safe = lambda v: "".join(c for c in str(v) if c.isalnum() or c in ".-_ ?")[:32]  # noqa: E731
    sets = client.get("sets") if isinstance(client.get("sets"), dict) else {}
    return (f"platform={safe(client.get('platform', '?'))} version={safe(client.get('version', '?'))} "
            + " ".join(f"mailbox{safe(k)}={bool(v)}" for k, v in sorted(sets.items())))


class AddinSource:
    """Connector identity for history rows written by the add-in (never collides with mail-client scans)."""
    name = "outlook-addin"


class Triage:
    """Owns the history database and engine on one worker thread (sqlite connections are per-thread)."""

    def __init__(self, config_path: Path | None = None, router_factory=None, store_path: Path | None = None):
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mail-triage-addin")
        self._pool.submit(self._open, config_path, router_factory, store_path).result()

    def _open(self, config_path, router_factory, store_path):
        if router_factory is None:
            from ..backends import create as router_factory
        self.config = configmod.load(config_path)
        home = privacy.private_dir(configmod.home())
        self.store = Store(store_path or home / "history.sqlite")
        self.store.purge_older(int(self.config.privacy["retention_days"]))
        self.engine = Engine(self.config, self.store, AddinSource(), router_factory, out=lambda _line: None)
        self.run = self.store.start_run(AddinSource.name)

    def call(self, fn, *args):
        return self._pool.submit(fn, *args).result()

    # -- worker-thread operations ------------------------------------------------------------

    def decide(self, payload: dict, source: str, headers_available: bool) -> dict:
        info = message_info(payload, headers_available)
        mailbox = Mailbox(account_of(payload), MAILBOX)
        decision, origin = self.engine.decide_one(mailbox, info, source)
        return {"source": origin, "verdict": decision.verdict, "category": decision.category,
                "junk_likelihood": decision.likelihood}

    def known(self, account: str, ids: list[str]) -> list[bool]:
        fingerprint = self.engine.fingerprint
        return [bool(mid) and not self.store.needs_decision(message_key(account, mid, mid), "new", fingerprint)
                for mid in ids]

    def report(self) -> Path:
        html_path, _ = report.write(self.store, self.config, configmod.home() / "reports", None)
        return html_path

    def _close(self):
        self.store.commit()
        self.store.finish_run(self.run, dict(self.engine.counts))
        self.engine.close()
        self.store.close()

    def close(self):
        self.call(self._close)
        self._pool.shutdown()


MAILBOX = "Outlook"


def account_of(payload: dict) -> str:
    account = payload.get("account")
    return account.strip().lower()[:320] if isinstance(account, str) and account.strip() else "outlook"


def message_info(payload: dict, headers_available: bool) -> MessageInfo:
    mid = str(payload.get("internetMessageId") or "").strip()[:998]
    sender = str(payload.get("sender") or "").strip().lower()[:320]
    subject = str(payload.get("subject") or "")[:998]
    received = str(payload.get("received") or "")[:40]
    # Without a Message-ID, derive a stable local identity from fields Outlook gave us.
    native = mid or "addin:" + hashlib.sha256("\0".join((sender, subject, received)).encode()).hexdigest()[:32]
    return MessageInfo(native_id=native, message_id=mid, sender=sender, subject=subject, received=received,
                       headers_available=headers_available)


def open_file(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    elif sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 — a local report file


class Handler(BaseHTTPRequestHandler):
    server_version = "MailTriageAddin"
    triage: Triage | None = None  # set by serve()
    verbose = False

    def log_message(self, *args):  # default logging would print request lines; keep the console status-only
        pass

    def _send(self, status: int, body: bytes, kind: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _diag(self):
        """Failed-step diagnostics from the task pane: step/error code and durations only."""
        length = self._length()
        if 0 < length <= 4_000:
            try:
                entry = json.loads(self.rfile.read(length))
                print(f"[diag] error={safe_text(entry.get('error'), 120)} {timing_text(entry.get('timing'))}", flush=True)
            except (ValueError, AttributeError):
                pass
        self._send(204, b"", "text/plain")

    def _csp_report(self):
        length = self._length()
        if 0 < length <= 64_000:
            try:
                print(f"[csp] {csp_summary(json.loads(self.rfile.read(length)))}", flush=True)
            except ValueError:
                pass
        self._send(204, b"", "text/plain")

    def _length(self) -> int:
        """Declared body length; -1 when missing or malformed (callers then answer 4xx)."""
        try:
            return int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return -1

    def _allowed(self) -> bool:
        return self.headers.get("Origin") == ORIGIN and self.headers.get("X-Mail-Triage") == "probe"

    def _json_body(self, limit: int):
        length = self._length()
        if not 0 < length <= limit:
            return None
        return json.loads(self.rfile.read(length))

    def _log(self, line: str) -> None:
        if self.verbose:
            print(line, flush=True)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._send(200, json.dumps({"ok": True, "service": "mail-triage-addin"}).encode(),
                              "application/json")
        if path in FILES:
            extra = {CSP_HEADER: CSP} if path.endswith(".html") else {}
            return self._send(200, (STATIC / path.lstrip("/")).read_bytes(), FILES[path], extra)
        if path in ("/icon-16.png", "/icon-32.png", "/icon-64.png", "/icon-80.png", "/icon-128.png"):
            return self._send(200, png(int(path[6:-4])), "image/png")
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path == "/csp-report":
            return self._csp_report()
        if not self._allowed():
            return self._send(403, b'{"error":"forbidden"}', "application/json")
        if self.path == "/diag":
            return self._diag()
        if self.path == "/known":
            return self._known()
        if self.path == "/report":
            return self._report()
        if self.path not in ("/triage", "/probe"):
            return self._send(404, b'{"error":"not found"}', "application/json")
        length = self._length()
        if not 0 < length <= MAX_BODY:
            return self._send(413, b'{"error":"size"}', "application/json")
        try:
            payload = json.loads(self.rfile.read(length))
            source = build_source(payload)
            facts = inspect(source)
        except (ValueError, ConnectorError, AttributeError):
            self._log("[addin] message: parse failed (no content logged)")
            return self._send(422, b'{"parsed":false}', "application/json")
        self._log(f"[addin] {client_facts(payload)}")
        self._log("[addin] message: " + " ".join(f"{k}={v}" for k, v in facts.items()))
        if "timing" in payload:
            self._log(f"[addin] {timing_text(payload.get('timing'))}")
        if self.triage is not None:
            try:
                result = self.triage.call(self.triage.decide, payload, source, facts["headers_available"])
                facts.update(result)
                # Verdict/category go to the user's task pane and local history only; logs stay content-free.
                self._log(f"[addin] decided source={result['source']}")
            except BackendError as error:
                facts["classified"] = False
                print(f"[addin] model unavailable ({type(error).__name__})", flush=True)
            except Exception as error:
                facts["classified"] = False
                print(f"[addin] could not decide one message ({type(error).__name__})", flush=True)
        del source, payload
        self._send(200, json.dumps(facts).encode(), "application/json")

    def _known(self):
        try:
            payload = self._json_body(64_000)
            ids = payload.get("ids") if isinstance(payload, dict) else None
            if not isinstance(ids, list) or len(ids) > 500 or not all(isinstance(i, str) for i in ids):
                raise ValueError
        except ValueError:
            return self._send(422, b'{"error":"invalid"}', "application/json")
        known = self.triage.call(self.triage.known, account_of(payload), ids) if self.triage else [False] * len(ids)
        self._send(200, json.dumps({"known": known}).encode(), "application/json")

    def _report(self):
        if self.triage is None:
            return self._send(503, b'{"error":"unavailable"}', "application/json")
        try:
            path = self.triage.call(self.triage.report)
        except Exception as error:
            print(f"[addin] report failed ({type(error).__name__})", flush=True)
            return self._send(500, b'{"error":"report"}', "application/json")
        open_file(path)
        self._send(200, b'{"opened":true}', "application/json")


def csp_summary(report: dict) -> str:
    """Directive and blocked origin only (never full URLs, which could carry identifiers)."""
    body = report.get("csp-report", report) if isinstance(report, dict) else {}
    blocked = str(body.get("blocked-uri", "?"))
    origin = "/".join(blocked.split("/")[:3]) if "://" in blocked else blocked
    directive = str(body.get("effective-directive") or body.get("violated-directive") or "?").split(" ")[0]
    clean = lambda v: "".join(c for c in v if c.isalnum() or c in ":/.-_*'?")[:80]  # noqa: E731
    return f"directive={clean(directive)} blocked={clean(origin)}"


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Browsers drop idle pre-opened TLS connections; that is not an error worth a traceback.
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, ssl.SSLError, TimeoutError)):
            return
        print(f"[addin] request error: {type(sys.exc_info()[1]).__name__}", flush=True)


def serve(verbose: bool = False, config_path: Path | None = None) -> int:
    """Run the add-in server in the foreground (the background service runs exactly this)."""
    cert, key = certs.paths()
    if not cert.exists():
        print("[addin] no certificate yet: run  ./triage addin install", flush=True)
        return 2
    privacy.install_network_guard()  # this process only ever talks to loopback
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    Handler.verbose = verbose
    Handler.triage = Triage(config_path)
    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"[addin] port {PORT} is already in use (is the background service running?)", flush=True)
        Handler.triage.close()
        return 3
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f"[addin] serving {ORIGIN} (loopback only, network guard on)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        Handler.triage.close()
        print("[addin] stopped", flush=True)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mail Triage Outlook add-in server (localhost only).")
    parser.add_argument("--verbose", action="store_true", help="log per-message status lines (never content)")
    parser.add_argument("--config", help="settings file (default: config.toml in the data folder)")
    args = parser.parse_args(argv)
    return serve(args.verbose, Path(args.config) if args.config else None)


if __name__ == "__main__":
    sys.exit(main())
