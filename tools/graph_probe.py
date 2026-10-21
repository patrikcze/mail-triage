#!/usr/bin/env python3
"""Opt-in Microsoft Graph mailbox access probe for Mail Triage (stages A–E).

Independent of the Mail Triage connectors. It proves, step by step, whether an approved
public-client registration can read the signed-in user's own mailbox:

  A  capability      broker/runtime, client configuration, connectivity (no sign-in)
  B  authentication  MSAL interactive sign-in (broker if available, else system browser + PKCE)
  C  authorization   granted delegated scopes
  D  mailbox         GET /me/mailFolders/inbox/messages?$top=N&$select=id,isRead
  E  message read    GET /me/messages/{id}/$value, parsed in memory (explicit confirmation)

Safety:
- Prints only status values: never tokens, subjects, addresses, bodies or message ids.
- Tokens stay in process memory (no MSAL cache file); the broker, when present, owns caching.
- Only GET requests to Microsoft Graph; read state is compared before and after stage E.
- A socket guard restricts this process to Microsoft identity/Graph hosts and loopback.
- Message content is parsed in memory and discarded; nothing is written to disk.

Usage:
  python tools/graph_probe.py --login-hint you@corp.example            # stage A only
  python tools/graph_probe.py --client-id GUID --login-hint you@corp.example --sign-in
  python tools/graph_probe.py --client-id GUID --login-hint you@corp.example --sign-in --read-mime 1

The client id may also come from MAIL_TRIAGE_GRAPH_CLIENT_ID. Never use another
application's client id (Outlook, Graph PowerShell, Azure CLI …).
"""

from __future__ import annotations

import argparse
from email import policy
from email.parser import BytesParser
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request

AUTHORITY_HOST = "login.microsoftonline.com"
GRAPH_HOST = "graph.microsoft.com"
GRAPH = f"https://{GRAPH_HOST}/v1.0"
ALLOWED_HOSTS = frozenset({AUTHORITY_HOST, GRAPH_HOST})
SCOPE_METADATA = "Mail.ReadBasic"
SCOPE_CONTENT = "Mail.Read"
WRITE_SCOPE_MARKERS = ("readwrite", "send")
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
MAC_BROKER_REDIRECT = "msauth.com.msauth.unsignedapp://auth"
MAX_RETRIES = 3
MAX_RETRY_AFTER = 30

# Microsoft Entra error codes → (category, minimum supported change).
AADSTS = {
    "700016": ("app_registration_unavailable", "client id is not registered in this tenant; obtain an approved registration"),
    "50011": ("redirect_uri_mismatch", "register the redirect URI used by this flow on the app registration"),
    "500113": ("redirect_uri_mismatch", "register a redirect URI on the app registration"),
    "65001": ("consent_required", "delegated consent has not been granted for the requested scope"),
    "65004": ("consent_declined", "consent was declined during sign-in"),
    "90094": ("admin_approval_required", "an administrator must grant consent (user consent is disabled)"),
    "90095": ("admin_approval_required", "submit the admin consent request or ask IT to grant consent"),
    "50105": ("user_not_assigned", "assign the user to the enterprise application"),
    "7000218": ("public_client_flows_disabled", "enable public client flows / 'Mobile and desktop' platform on the registration"),
    "50194": ("authority_mismatch", "single-tenant app: use the tenant authority, not /common or /organizations"),
    "50020": ("wrong_tenant", "account is not a member of the app's tenant"),
    "53000": ("conditional_access_device", "Conditional Access requires a compliant/managed device"),
    "53001": ("conditional_access_device", "Conditional Access requires a domain-joined/registered device"),
    "53003": ("conditional_access_blocked", "Conditional Access policy blocks this client; IT must include it"),
    "530003": ("conditional_access_device", "device management required by Conditional Access"),
    "50076": ("mfa_challenge", "complete MFA interactively (interactive flow required)"),
    "50079": ("mfa_challenge", "MFA registration required"),
    "50158": ("mfa_challenge", "external security challenge not satisfied"),
    "50126": ("invalid_credentials", "sign-in failed: invalid credentials"),
    "50053": ("account_locked", "account locked or sign-in blocked"),
}


class ProbeBlocked(ConnectionError):
    pass


def emit(stage: str, key: str, value) -> None:
    print(f"[{stage}] {key}: {value}")


def redact_correlation(value: str | None) -> str:
    return f"{value[:8]}…" if value else "n/a"


# --- network boundary ------------------------------------------------------------------

def _is_loopback(host) -> bool:
    if host is None:
        return True
    host = (host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)).strip("[]").lower()
    if host in ("localhost", "ip6-localhost"):
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def install_allowlist_guard(hosts=ALLOWED_HOSTS) -> set[str]:
    """Allow DNS only for `hosts` (and loopback); allow connects only to addresses they resolved to."""
    hosts = {h.lower() for h in hosts}
    approved: set[str] = set()
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def getaddrinfo(host, *args, **kwargs):
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else host
        if not (_is_loopback(name) or str(name).lower().rstrip(".") in hosts):
            raise ProbeBlocked(f"graph_probe blocks DNS lookup of {name!r}")
        result = real_getaddrinfo(host, *args, **kwargs)
        approved.update(str(info[4][0]) for info in result)
        return result

    def check(sock, address):
        if sock.family == getattr(socket, "AF_UNIX", object()):
            return
        target = address[0] if isinstance(address, tuple) else address
        if not (_is_loopback(target) or str(target) in approved):
            raise ProbeBlocked(f"graph_probe blocks connection to {target!r}")

    def connect(self, address):
        check(self, address)
        return real_connect(self, address)

    def connect_ex(self, address):
        check(self, address)
        return real_connect_ex(self, address)

    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    return approved


def proxy_hosts() -> set[str]:
    hosts = set()
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        value = os.environ.get(name)
        if value:
            host = urllib.parse.urlsplit(value if "://" in value else f"http://{value}").hostname
            if host:
                hosts.add(host.lower())
    return hosts


# --- error categories -------------------------------------------------------------------

def categorize_auth_error(result: dict) -> tuple[str, str]:
    codes = [str(c) for c in (result.get("error_codes") or [])]
    text = f"{result.get('error', '')} {result.get('error_description', '')}"
    codes += re.findall(r"AADSTS(\d+)", text)
    for code in codes:
        if code in AADSTS:
            category, hint = AADSTS[code]
            return category, f"AADSTS{code}: {hint}"
    lowered = text.lower()
    if "sso extension is inactive" in lowered or ("broker" in lowered and "unavailable" in lowered):
        return "broker_unavailable", "Enterprise SSO extension not configured by MDM; use browser flow or ask IT"
    if "access_denied" in lowered or "cancel" in lowered:
        return "user_cancelled", "sign-in was cancelled or denied by the user"
    if "invalid_client" in lowered:
        return "invalid_client", "client id or platform configuration is invalid"
    if "timed out" in lowered or "connection" in lowered:
        return "connectivity", "could not reach Microsoft identity platform"
    return "unknown_auth_error", str(result.get("error") or "unknown")[:80]


def oauth_error_fields(result: dict) -> str:
    """Protocol error fields and AADSTS codes only; descriptions may carry identifiers."""
    codes = sorted({str(c) for c in (result.get("error_codes") or [])}
                   | set(re.findall(r"AADSTS(\d+)", str(result.get("error_description", "")))))
    error = re.sub(r"[^a-z_]", "", str(result.get("error", "")).lower())[:40] or "none"
    subcode = re.sub(r"[^a-z_]", "", str(result.get("error_subcode", "")).lower())[:40] or "none"
    return f"error={error} subcode={subcode} aadsts={','.join(codes) or 'none'}"


def categorize_graph_error(status: int, body: bytes | str = b"") -> tuple[str, str]:
    try:
        data = json.loads(body or b"{}")
        code = str(data.get("error", {}).get("code", ""))
    except (ValueError, AttributeError):
        code = ""
    if status == 401:
        return "graph_unauthorized", f"{code or 'InvalidAuthenticationToken'}: token rejected or expired"
    if status == 403:
        return "graph_forbidden", f"{code or 'Forbidden'}: delegated permission missing or blocked by policy"
    if status == 404 and code in ("MailboxNotEnabledForRESTAPI", "MailboxNotHostedInExchangeOnline"):
        return "mailbox_incompatible", f"{code}: no Exchange Online mailbox reachable via Graph"
    if status == 404:
        return "not_found", code or "item not found"
    if status in (429, 503, 504):
        return "throttled", code or "throttled or temporarily unavailable"
    return "graph_error", f"HTTP {status} {code}".strip()


# --- Graph HTTP (GET only) --------------------------------------------------------------

def tls_context() -> ssl.SSLContext:
    """Verified TLS: OS trust store (truststore) first, then certifi, then Python defaults.

    python.org builds on macOS do not read the system keychain, so the default context alone
    cannot verify Microsoft's certificates there. Verification is never disabled.
    """
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class GraphResponse:
    def __init__(self, status: int, body: bytes, headers: dict):
        self.status, self.body, self.headers = status, body, headers

    def json(self) -> dict:
        return json.loads(self.body or b"{}")


def urllib_get(url: str, headers: dict, timeout: float = 30) -> GraphResponse:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != GRAPH_HOST:
        raise ProbeBlocked(f"refusing non-Graph URL host {parts.hostname!r}")
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=tls_context()) as response:
            return GraphResponse(response.status, response.read(), dict(response.headers))
    except urllib.error.HTTPError as error:
        return GraphResponse(error.code, error.read(), dict(error.headers or {}))


class Graph:
    """Minimal read-only Graph client (GET only). `transport` is injectable for tests."""

    def __init__(self, token: str, transport: Callable[..., GraphResponse] = urllib_get,
                 sleep: Callable[[float], None] = time.sleep):
        self._token = token
        self._transport = transport
        self._sleep = sleep

    def __repr__(self) -> str:
        return "Graph(<token redacted>)"

    def get(self, path: str, params: dict | None = None, accept: str = "application/json") -> GraphResponse:
        url = GRAPH + path + ("?" + urllib.parse.urlencode(params, safe="$,") if params else "")
        headers = {"Authorization": f"Bearer {self._token}", "Accept": accept,
                   "Prefer": 'IdType="ImmutableId"'}
        for attempt in range(MAX_RETRIES):
            response = self._transport(url, headers)
            if response.status not in (429, 503, 504) or attempt == MAX_RETRIES - 1:
                return response
            try:
                delay = float(response.headers.get("Retry-After", 2 ** attempt))
            except (TypeError, ValueError):
                delay = 2 ** attempt
            self._sleep(min(max(delay, 0), MAX_RETRY_AFTER))
        return response


# --- stage A: capability ----------------------------------------------------------------

def _run(cmd: list[str]) -> str:
    if not shutil.which(cmd[0]):
        return ""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def mac_broker_facts() -> dict:
    extension = "com.microsoft.CompanyPortalMac.ssoextension"
    return {
        "company_portal_installed": bool(_run(["mdfind", "kMDItemCFBundleIdentifier == 'com.microsoft.CompanyPortalMac'"]).strip()),
        "sso_extension_installed": extension in _run(["pluginkit", "-m", "-p", "com.apple.AppSSO.idp-extension"]),
        "sso_extension_configured_by_mdm": extension in _run(["app-sso", "-l"]),
    }


def broker_runtime_status() -> tuple[bool, str]:
    try:
        import pymsalruntime  # noqa: F401
    except ImportError:
        return False, 'pymsalruntime not installed (pip install "msal[broker]>=1.31,<2")'
    except Exception as error:  # raises RuntimeError when the macOS SSO extension is inactive
        category, hint = categorize_auth_error({"error_description": str(error)})
        return False, f"{category}: {hint}"
    return True, "available"


def stage_a(args) -> dict:
    facts = {}
    emit("A", "platform", f"{platform.system()} {platform.release()} {platform.machine()}")
    emit("A", "python", platform.python_version())
    try:
        import msal
        facts["msal"] = msal.__version__
    except ImportError:
        facts["msal"] = None
    emit("A", "msal", facts["msal"] or 'missing (pip install "msal[broker]>=1.31,<2")')
    if sys.platform == "darwin":
        for key, value in mac_broker_facts().items():
            emit("A", key, value)
    broker_ok, broker_note = broker_runtime_status()
    facts["broker"] = broker_ok
    emit("A", "broker_runtime", broker_note)
    emit("A", "planned_auth_method", "broker" if broker_ok else "system browser, auth code + PKCE (http://localhost)")

    client_id = args.client_id or os.environ.get("MAIL_TRIAGE_GRAPH_CLIENT_ID", "")
    facts["client_id"] = client_id if GUID.match(client_id) else ""
    emit("A", "client_id_configured", "invalid format (expected GUID)" if client_id and not facts["client_id"]
         else bool(facts["client_id"]))
    hint_domain = args.login_hint.split("@", 1)[1] if "@" in args.login_hint else ""
    facts["tenant"] = args.tenant or hint_domain
    emit("A", "tenant_authority", "configured" if facts["tenant"] else "missing (use --tenant or --login-hint)")
    if facts["tenant"]:
        url = f"https://{AUTHORITY_HOST}/{urllib.parse.quote(facts['tenant'])}/v2.0/.well-known/openid-configuration"
        try:
            with urllib.request.urlopen(url, timeout=15, context=tls_context()) as response:
                facts["tenant_resolved"] = response.status == 200
        except (urllib.error.URLError, OSError):
            facts["tenant_resolved"] = False
        emit("A", "tenant_resolved", facts["tenant_resolved"])
    try:
        status = urllib_get(f"{GRAPH}/me", {"Accept": "application/json"}, timeout=15).status
        emit("A", "graph_reachable_tls", f"yes (unauthenticated HTTP {status})")
    except (urllib.error.URLError, OSError) as error:
        emit("A", "graph_reachable_tls", f"no ({type(error).__name__})")
    if not facts["client_id"]:
        emit("A", "prerequisite", "approved public-client app registration (client id) with delegated "
             f"{SCOPE_METADATA}/{SCOPE_CONTENT}, redirect URIs http://localhost and {MAC_BROKER_REDIRECT}, "
             "and admin consent where user consent is disabled")
    return facts


# --- stage B/C: authentication and authorization ---------------------------------------

def requested_scopes(read_mime: int) -> list[str]:
    scopes = [SCOPE_CONTENT if read_mime else SCOPE_METADATA]
    if any(marker in s.lower() for s in scopes for marker in WRITE_SCOPE_MARKERS):
        raise ValueError("graph_probe never requests write scopes")
    return scopes


def stage_b(client_id: str, tenant: str, login_hint: str, scopes: list[str], msal_module=None,
            timeout: int = 300) -> dict | None:
    if msal_module is None:
        import msal as msal_module
    app = msal_module.PublicClientApplication(
        client_id, authority=f"https://{AUTHORITY_HOST}/{tenant}",
        enable_broker_on_mac=True, enable_broker_on_windows=True)
    emit("B", "auth_method", "broker" if getattr(app, "_enable_broker", False) else "system browser (auth code + PKCE)")
    result = None
    accounts = app.get_accounts(username=login_hint or None)
    if accounts:
        result = app.acquire_token_silent(scopes, account=accounts[0])
        emit("B", "silent", "success" if result and "access_token" in result else "not available")
    if not result or "access_token" not in result:
        result = app.acquire_token_interactive(
            scopes, login_hint=login_hint or None, prompt=None if login_hint else "select_account",
            parent_window_handle=app.CONSOLE_WINDOW_HANDLE, timeout=timeout)
    result = result or {}
    if "access_token" not in result:
        category, hint = categorize_auth_error(result)
        emit("B", "authentication", f"failed: {category}")
        emit("B", "detail", hint)
        emit("B", "oauth_error", oauth_error_fields(result))
        emit("B", "correlation_id", redact_correlation(result.get("correlation_id")))
        return None
    emit("B", "authentication", "success")
    emit("B", "token_source", result.get("token_source", "unknown"))
    return result


def stage_c(result: dict, scopes: list[str]) -> bool:
    granted = {s.split("/")[-1].lower() for s in str(result.get("scope", "")).split()}
    missing = [s for s in scopes if s.lower() not in granted]
    writes = sorted(s for s in granted if any(marker in s for marker in WRITE_SCOPE_MARKERS))
    emit("C", "requested_scopes", " ".join(scopes))
    emit("C", "granted_mail_scopes", " ".join(sorted(s for s in granted if s.startswith("mail."))) or "none")
    if writes:
        emit("C", "warning", f"token carries write scopes not requested by this probe: {' '.join(writes)}")
    emit("C", "authorization", "granted" if not missing else f"missing {' '.join(missing)}")
    return not missing


# --- stage D/E: mailbox proof -----------------------------------------------------------

def stage_d(graph: Graph, top: int) -> list[dict] | None:
    response = graph.get("/me/mailFolders/inbox/messages", {"$top": str(top), "$select": "id,isRead"})
    emit("D", "http_status", response.status)
    if response.status != 200:
        category, hint = categorize_graph_error(response.status, response.body)
        emit("D", "mailbox", f"failed: {category}")
        emit("D", "detail", hint)
        emit("D", "request_id", redact_correlation(response.headers.get("request-id")))
        return None
    data = response.json()
    items = data.get("value", [])
    emit("D", "messages_retrieved", len(items))
    emit("D", "has_next_page", "@odata.nextLink" in data)
    return items


def read_state(graph: Graph, message_id: str) -> bool | None:
    response = graph.get(f"/me/messages/{urllib.parse.quote(message_id, safe='')}", {"$select": "isRead"})
    return response.json().get("isRead") if response.status == 200 else None


def inspect_mime(raw: bytes) -> dict:
    """Structural facts only; never returns header values or body text."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    try:
        body_found = message.get_body(preferencelist=("plain", "html")) is not None
    except Exception:
        body_found = False
    return {
        "parsed": len(message.keys()) > 0,
        "has_received": bool(message.get_all("Received")),
        "has_authentication_results": bool(message.get_all("Authentication-Results")),
        "has_message_id": bool(message.get("Message-ID")),
        "body_found": body_found,
    }


def stage_e(graph: Graph, items: list[dict]) -> bool:
    ok = True
    for index, item in enumerate(items, 1):
        quoted = urllib.parse.quote(item["id"], safe="")
        before = read_state(graph, item["id"])
        response = graph.get(f"/me/messages/{quoted}/$value", accept="message/rfc822")
        emit("E", f"message_{index}_http_status", response.status)
        if response.status != 200:
            category, hint = categorize_graph_error(response.status, response.body)
            emit("E", f"message_{index}", f"failed: {category} ({hint})")
            ok = False
            continue
        facts = inspect_mime(response.body)
        response.body = b""
        emit("E", f"message_{index}_original_mime", facts["parsed"])
        emit("E", f"message_{index}_parsed", facts["parsed"] and facts["body_found"])
        emit("E", f"message_{index}_transport_headers",
             f"received={facts['has_received']} auth_results={facts['has_authentication_results']} "
             f"message_id={facts['has_message_id']}")
        after = read_state(graph, item["id"])
        unchanged = before is not None and before == after
        emit("E", f"message_{index}_read_state_unchanged", unchanged)
        ok = ok and facts["parsed"] and unchanged
    return ok


def confirm_read(count: int, prompt: Callable[[str], str] = input) -> bool:
    print(f"Stage E reads {count} message(s) as MIME into memory only. Nothing is printed or saved.")
    try:
        return prompt("Type READ to continue: ").strip() == "READ"
    except EOFError:
        return False


# --- main -------------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-id", default="", help="approved public-client app id (GUID)")
    parser.add_argument("--tenant", default="", help="tenant domain or id (default: login-hint domain)")
    parser.add_argument("--login-hint", default="", help="your work account (not printed)")
    parser.add_argument("--sign-in", action="store_true", help="run stages B–D (interactive sign-in)")
    parser.add_argument("--timeout", type=int, default=300, help="seconds to wait for interactive sign-in")
    parser.add_argument("--read-mime", type=int, default=0, choices=(0, 1, 2),
                        help="stage E: read this many messages as MIME (requests Mail.Read, asks to confirm)")
    args = parser.parse_args(argv)

    extra = proxy_hosts()
    install_allowlist_guard(set(ALLOWED_HOSTS) | extra)
    emit("A", "network_allowlist", " ".join(sorted(ALLOWED_HOSTS)) + (" + configured proxy" if extra else ""))

    facts = stage_a(args)
    if not args.sign_in:
        return 0 if facts["client_id"] else 2
    if not (facts["msal"] and facts["client_id"] and facts["tenant"]):
        emit("B", "authentication", "not attempted: prerequisites from stage A are missing")
        return 2
    scopes = requested_scopes(args.read_mime)
    result = stage_b(facts["client_id"], facts["tenant"], args.login_hint, scopes, timeout=args.timeout)
    if result is None:
        return 2
    authorized = stage_c(result, scopes)
    graph = Graph(result["access_token"])
    result.clear()
    if not authorized:
        emit("D", "mailbox", "not attempted: delegated permission missing")
        return 2
    items = stage_d(graph, max(1, args.read_mime))
    if items is None:
        return 2
    if not args.read_mime:
        return 0
    if not items:
        emit("E", "message_read", "skipped: inbox is empty")
        return 0
    if not confirm_read(len(items)):
        emit("E", "message_read", "skipped: not confirmed")
        return 0
    return 0 if stage_e(graph, items[: args.read_mime]) else 2


if __name__ == "__main__":
    sys.exit(main())
