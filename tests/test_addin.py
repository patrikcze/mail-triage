"""Tests for mail_triage.addin (server, certs, service, CLI glue): synthetic data only.

No TLS, no fixed port, no real model, and never the real launchctl/security/openssl or data folder.
"""

import contextlib
import http.client
import io
import json
import os
from http.server import ThreadingHTTPServer
from pathlib import Path
import plistlib
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

from helpers import FakeRouter, TempHome, default_config

from mail_triage import cli, config as configmod
from mail_triage.addin import certs, server, service
from mail_triage.backends.base import BackendError
from mail_triage.connectors.base import Mailbox
from mail_triage.engine import Engine
from mail_triage.store import Store, message_key

STATIC = Path(__file__).resolve().parent.parent / "mail_triage" / "addin" / "static"

SUBJECT = "Synthetic subject 7"
SENDER = "sender@shop.example"
BODY = "Synthetic body text for the probe."
MESSAGE_ID = "<synthetic-id-7@example.net>"
HEADERS = ("Received: from mx.example.com by mx.example.net; Mon, 1 Jan 2024 00:00:00 +0000\r\n"
           "Authentication-Results: mx.example.net; spf=pass\r\n"
           f"Message-ID: {MESSAGE_ID}\r\n")
PAYLOAD = {"headers": HEADERS, "body": BODY, "sender": SENDER, "subject": SUBJECT,
           "internetMessageId": MESSAGE_ID, "html": False, "account": "me@example.com",
           "received": "2024-01-01T00:00:00.000Z",
           "client": {"platform": "Mac", "version": "16.8", "sets": {"1.8": True, "1.13": False}}}
SECRETS = (SUBJECT, SENDER, BODY, "synthetic-id-7")
RESULT = {"source": "model", "verdict": "keep", "category": "personal", "junk_likelihood": "unlikely"}


class SourceAndFactsTests(unittest.TestCase):
    def test_all_facts_true(self):
        facts = server.inspect(server.build_source(PAYLOAD))
        self.assertTrue(all(value is True for value in facts.values()), facts)
        self.assertEqual({"parsed", "headers_available", "received", "authentication_results", "message_id", "body"},
                         set(facts))
        self.assertFalse(any(secret in str(facts) for secret in SECRETS))

    def test_empty_headers(self):
        facts = server.inspect(server.build_source({**PAYLOAD, "headers": ""}))
        self.assertFalse(facts["headers_available"] or facts["received"] or facts["authentication_results"])
        self.assertTrue(all(isinstance(value, bool) for value in facts.values()))

    def test_non_string_field(self):
        with self.assertRaises(server.ConnectorError):
            server.build_source({**PAYLOAD, "headers": 5})

    def test_csp_summary(self):
        report = {"blocked-uri": "https://cdn.example/path/x.js?id=secret", "violated-directive": "script-src-elem https://x"}
        expected = "directive=script-src-elem blocked=https://cdn.example"
        self.assertEqual(expected, server.csp_summary({"csp-report": report}))
        self.assertEqual(expected, server.csp_summary(report))
        self.assertEqual("directive=? blocked=?", server.csp_summary({"csp-report": {}}))
        self.assertEqual("directive=? blocked=?", server.csp_summary([]))

    def test_client_facts(self):
        text = server.client_facts({"client": {"platform": "Mac<script>", "version": "1.2", "sets": {"1.8": 1, "1.13": 0}}})
        self.assertNotIn("<", text)
        self.assertNotIn(">", text)
        self.assertEqual("platform=Macscript version=1.2 mailbox1.13=False mailbox1.8=True", text)
        self.assertEqual("platform=? version=? ", server.client_facts({"client": "x"}))

    def test_safe_text_and_timing_text(self):
        self.assertEqual("ab c.1", server.safe_text("a<b> c.1;\n"))
        self.assertEqual(5, len(server.safe_text("abcdefghij", 5)))
        self.assertEqual("timing load=12ms body=3ms",
                         server.timing_text({"load": 12.7, "body": 3, "bad": "x", "flag": True}))
        self.assertEqual("timing=?", server.timing_text("nope"))
        self.assertEqual("timing ?", server.timing_text({}))

    def test_png(self):
        data = server.png(16)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(b"IHDR", data[12:16])
        self.assertEqual((16, 16), (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")))


class MessageInfoTests(unittest.TestCase):
    def test_message_id_is_native_and_message_id(self):
        info = server.message_info(PAYLOAD, True)
        self.assertEqual((MESSAGE_ID, MESSAGE_ID), (info.native_id, info.message_id))
        self.assertEqual((SENDER, SUBJECT, "2024-01-01T00:00:00.000Z"), (info.sender, info.subject, info.received))
        self.assertIs(True, info.headers_available)
        self.assertIs(False, server.message_info(PAYLOAD, False).headers_available)

    def test_missing_id_derives_stable_native_id(self):
        payload = {**PAYLOAD, "internetMessageId": ""}
        first, second = server.message_info(payload, True), server.message_info(dict(payload), True)
        self.assertEqual("", first.message_id)
        self.assertTrue(first.native_id.startswith("addin:"))
        self.assertEqual(first.native_id, second.native_id)
        self.assertNotIn(SUBJECT, first.native_id)
        other = server.message_info({**payload, "subject": "Synthetic subject 8"}, True)
        self.assertNotEqual(first.native_id, other.native_id)
        self.assertTrue(server.message_info({}, True).native_id.startswith("addin:"))

    def test_lowercases_sender_and_truncates(self):
        info = server.message_info({"sender": "  Sender@Shop.Example ", "internetMessageId": "m" * 2000,
                                    "subject": "s" * 2000, "received": "r" * 100}, True)
        self.assertEqual("sender@shop.example", info.sender)
        self.assertEqual((998, 998, 40), (len(info.message_id), len(info.subject), len(info.received)))
        self.assertEqual(320, len(server.message_info({"sender": "a" * 500}, True).sender))

    def test_account_of(self):
        self.assertEqual("outlook", server.account_of({}))
        self.assertEqual("outlook", server.account_of({"account": "   "}))
        self.assertEqual("outlook", server.account_of({"account": 5}))
        self.assertEqual("me@example.com", server.account_of({"account": " Me@Example.com "}))
        self.assertEqual(320, len(server.account_of({"account": "a" * 500})))


class CountingRouter(FakeRouter):
    def __init__(self, config=None):
        super().__init__(config)
        self.closed = False

    def close(self):
        self.closed = True


class RouterFactory:
    def __init__(self):
        self.routers = []

    def __call__(self, config):
        router = CountingRouter()
        self.routers.append(router)
        return router


class TempHomeCase(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.folder = self.home.__enter__()
        self.addCleanup(self.home.__exit__, None, None, None)

    def rows(self, sql):
        db = sqlite3.connect(self.folder / "history.sqlite")
        try:
            return db.execute(sql).fetchall()
        finally:
            db.close()


class DecideOneTests(TempHomeCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.folder / "history.sqlite")
        self.addCleanup(self.store.close)
        self.factory = RouterFactory()
        self.source = server.build_source(PAYLOAD)
        self.info = server.message_info(PAYLOAD, True)
        self.mailbox = Mailbox("me@example.com", server.MAILBOX)

    def engine(self, config=None):
        return Engine(config or default_config(), self.store, server.AddinSource(), self.factory,
                      out=lambda line: None)

    def key(self):
        return message_key(self.mailbox.account, self.info.message_id, self.info.native_id,
                           scope=self.info.identity_scope, mailbox=self.mailbox.name)

    def test_model_decision_is_recorded_and_then_known(self):
        engine = self.engine()
        self.assertTrue(self.store.needs_decision(self.key(), "new", engine.fingerprint))
        decision, origin = engine.decide_one(self.mailbox, self.info, self.source)
        self.assertEqual(("model", "keep"), (origin, decision.verdict))
        self.assertEqual([("outlook-addin", "me@example.com", "Outlook", MESSAGE_ID)],
                         self.rows("SELECT connector, account, mailbox, native_id FROM messages"))
        self.assertEqual([("model", "keep", 1)], self.rows("SELECT source, verdict, current FROM decisions"))
        self.assertFalse(self.store.needs_decision(self.key(), "new", engine.fingerprint))
        self.assertEqual(1, len(self.factory.routers))

    def test_same_message_twice_keeps_one_current_decision(self):
        engine = self.engine()
        engine.decide_one(self.mailbox, self.info, self.source)
        engine.decide_one(self.mailbox, self.info, self.source)
        self.assertEqual([(1,)], self.rows("SELECT COUNT(*) FROM messages"))
        self.assertEqual([(1,)], self.rows("SELECT COUNT(*) FROM decisions WHERE current = 1"))
        self.assertEqual(1, len(self.factory.routers))  # the router is created once and reused

    def test_teaching_records_without_calling_the_router(self):
        self.store.teach("domain", "shop.example", "personal")
        decision, origin = self.engine().decide_one(self.mailbox, self.info, self.source)
        self.assertEqual(("teach", "keep"), (origin, decision.verdict))
        self.assertEqual([("teach",)], self.rows("SELECT source FROM decisions WHERE current = 1"))
        self.assertEqual([], self.factory.routers)

    def test_rule_records_without_calling_the_router(self):
        rule = configmod.Rule(name="shop", category="personal", domain="shop.example")
        decision, origin = self.engine(default_config(rules=(rule,))).decide_one(self.mailbox, self.info, self.source)
        self.assertEqual(("rule", "keep"), (origin, decision.verdict))
        self.assertEqual([], self.factory.routers)

    def test_does_not_use_scan_and_close_releases_the_router(self):
        engine = self.engine()
        with mock.patch.object(Engine, "scan", side_effect=AssertionError("scan must not be used")):
            engine.decide_one(self.mailbox, self.info, self.source)
        router = self.factory.routers[0]
        engine.close()
        self.assertTrue(router.closed)
        engine.close()  # idempotent


class TriageTests(TempHomeCase):
    def setUp(self):
        super().setUp()
        self.factory = RouterFactory()
        self.triage = server.Triage(router_factory=self.factory)
        self.closed = False
        self.addCleanup(lambda: None if self.closed else self.triage.close())

    def decide(self, payload=PAYLOAD):
        source = server.build_source(payload)
        return self.triage.call(self.triage.decide, payload, source, True)

    def test_decide_returns_decision_fields_and_records(self):
        result = self.decide()
        self.assertEqual({"source", "verdict", "category", "junk_likelihood"}, set(result))
        self.assertEqual(("model", "keep"), (result["source"], result["verdict"]))
        self.assertEqual([("outlook-addin",)], self.rows("SELECT connector FROM messages"))

    def test_known_before_and_after_deciding(self):
        ids = [MESSAGE_ID, "", "<other-id@example.net>"]
        self.assertEqual([False, False, False], self.triage.call(self.triage.known, "me@example.com", ids))
        self.decide()
        self.assertEqual([True, False, False], self.triage.call(self.triage.known, "me@example.com", ids))
        self.assertEqual([False], self.triage.call(self.triage.known, "other@example.com", [MESSAGE_ID]))

    def test_report_returns_an_html_file_inside_the_temp_home(self):
        self.decide()
        path = self.triage.call(self.triage.report)
        self.assertEqual(".html", path.suffix)
        self.assertTrue(path.is_file())
        self.assertTrue(path.resolve().is_relative_to(self.folder.resolve()))

    def test_close_finishes_the_run_and_releases_the_router(self):
        self.decide()
        self.triage.close()
        self.closed = True
        self.assertTrue(self.factory.routers[0].closed)
        self.assertEqual(1, len(self.rows("SELECT * FROM runs WHERE finished IS NOT NULL")))
        with self.assertRaises(RuntimeError):
            self.triage.call(self.triage.known, "me@example.com", [])


class FakeTriage:
    def __init__(self):
        self.decide_calls, self.known_calls, self.report_calls = [], [], 0
        self.error = None
        self.report_path = Path("/synthetic/report.html")

    def call(self, fn, *args):
        return fn(*args)

    def decide(self, payload, source, headers_available):
        self.decide_calls.append((payload, source, headers_available))
        if self.error:
            raise self.error
        return dict(RESULT)

    def known(self, account, ids):
        self.known_calls.append((account, ids))
        return [i == MESSAGE_ID for i in ids]

    def report(self):
        self.report_calls += 1
        return self.report_path


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        patcher = mock.patch.object(server, "ORIGIN", self.origin)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake = FakeTriage()
        server.Handler.triage = self.fake
        server.Handler.verbose = False
        self.addCleanup(setattr, server.Handler, "triage", None)
        self.addCleanup(setattr, server.Handler, "verbose", False)
        printer = mock.patch("builtins.print")
        self.print = printer.start()
        self.addCleanup(printer.stop)
        opener = mock.patch.object(server, "open_file")
        self.open_file = opener.start()
        self.addCleanup(opener.stop)

    def request(self, method, path, body=b"", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response, response.read()

    def post(self, payload, path="/triage", **overrides):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        headers = {"Origin": self.origin, "X-Mail-Triage": "probe", "Content-Type": "application/json", **overrides}
        headers = {k: v for k, v in headers.items() if v is not None}
        return self.request("POST", path, body, headers)

    def console(self):
        return "\n".join(" ".join(str(a) for a in call.args) for call in self.print.call_args_list)

    def test_health(self):
        response, data = self.request("GET", "/health")
        self.assertEqual(200, response.status)
        self.assertIs(True, json.loads(data)["ok"])

    def test_static_routes(self):
        response, data = self.request("GET", "/taskpane.html")
        csp = response.getheader("Content-Security-Policy")
        self.assertEqual(200, response.status)
        self.assertIn(b"taskpane.js", data)
        for expected in ("'unsafe-eval'", "ajax.aspnetcdn.com", "report-uri"):
            self.assertIn(expected, csp)
        for path in ("/manifest.xml", "/manifest-readonly.xml", "/taskpane.js"):
            self.assertEqual(200, self.request("GET", path)[0].status, path)
        response, data = self.request("GET", "/icon-32.png")
        self.assertEqual((200, "image/png"), (response.status, response.getheader("Content-Type")))
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_unknown_and_traversal_paths_are_404(self):
        for path in ("/../server.py", "/..%2fserver.py", "/static/../server.py", "/nope", "/server.py", "/"):
            self.assertEqual(404, self.request("GET", path)[0].status, path)
        self.assertEqual(404, self.post({}, "/nope")[0].status)

    def test_triage_and_legacy_probe(self):
        for path in ("/triage", "/probe"):
            response, data = self.post(PAYLOAD, path)
            facts = json.loads(data)
            self.assertEqual(200, response.status, path)
            self.assertTrue(facts["parsed"] and facts["headers_available"] and facts["body"])
            self.assertEqual(RESULT, {k: facts[k] for k in RESULT})
        self.assertEqual(2, len(self.fake.decide_calls))
        self.assertIs(True, self.fake.decide_calls[0][2])

    def test_forbidden_without_origin_or_header(self):
        for path in ("/triage", "/known", "/report", "/diag"):
            self.assertEqual(403, self.post(PAYLOAD, path, Origin="https://evil.example")[0].status, path)
            self.assertEqual(403, self.post(PAYLOAD, path, Origin=None)[0].status, path)
            self.assertEqual(403, self.post(PAYLOAD, path, **{"X-Mail-Triage": None})[0].status, path)
        self.assertEqual((0, [], 0), (len(self.fake.decide_calls), self.fake.known_calls, self.fake.report_calls))
        self.open_file.assert_not_called()

    def test_oversize_and_malformed(self):
        with mock.patch.object(server, "MAX_BODY", 10):
            self.assertEqual(413, self.post(b"x" * 20)[0].status)
        response, data = self.post(b"{not json")
        self.assertEqual((422, {"parsed": False}), (response.status, json.loads(data)))
        self.assertEqual(422, self.post({**PAYLOAD, "headers": 5})[0].status)

    def test_malformed_content_length_gets_a_clean_4xx(self):
        headers = {"Origin": self.origin, "X-Mail-Triage": "probe", "Content-Length": "abc"}
        self.assertEqual(413, self.request("POST", "/triage", b"", headers)[0].status)
        self.assertEqual(422, self.request("POST", "/known", b"", headers)[0].status)
        self.assertEqual(204, self.request("POST", "/diag", b"", headers)[0].status)
        self.assertEqual([], self.fake.decide_calls)
        self.assertEqual(422, self.post(b"[1, 2]")[0].status)
        self.assertEqual([], self.fake.decide_calls)

    def test_known(self):
        response, data = self.post({"account": "Me@Example.com", "ids": [MESSAGE_ID, "<x@example.net>"]}, "/known")
        self.assertEqual((200, {"known": [True, False]}), (response.status, json.loads(data)))
        self.assertEqual([("me@example.com", [MESSAGE_ID, "<x@example.net>"])], self.fake.known_calls)

    def test_known_validates_input(self):
        invalid = ({"ids": "abc"}, {"ids": [1, 2]}, {"ids": ["a"] * 501}, {"ids": [None]}, {}, [], b"{bad", b"")
        for payload in invalid:
            self.assertEqual(422, self.post(payload, "/known")[0].status, payload)
        self.assertEqual([], self.fake.known_calls)
        self.assertEqual(200, self.post({"ids": ["a"] * 500}, "/known")[0].status)

    def test_report_opens_the_file(self):
        response, data = self.post({}, "/report")
        self.assertEqual((200, {"opened": True}), (response.status, json.loads(data)))
        self.open_file.assert_called_once_with(self.fake.report_path)

    def test_report_failure_and_unavailable(self):
        self.fake.report = mock.Mock(side_effect=RuntimeError("synthetic"))
        self.assertEqual(500, self.post({}, "/report")[0].status)
        server.Handler.triage = None
        self.assertEqual(503, self.post({}, "/report")[0].status)
        self.open_file.assert_not_called()

    def test_backend_error_still_answers_200_unclassified(self):
        self.fake.error = BackendError("synthetic model failure")
        response, data = self.post(PAYLOAD)
        facts = json.loads(data)
        self.assertEqual(200, response.status)
        self.assertIs(False, facts["classified"])
        self.assertNotIn("verdict", facts)
        self.assertIn("model unavailable", self.console())
        for secret in SECRETS:
            self.assertNotIn(secret, self.console())

    def test_quiet_by_default(self):
        self.assertEqual(200, self.post(PAYLOAD)[0].status)
        self.assertEqual([], self.print.call_args_list)

    def test_verbose_logs_status_but_no_content(self):
        server.Handler.verbose = True
        self.assertEqual(200, self.post({**PAYLOAD, "timing": {"headers": 5}})[0].status)
        console = self.console()
        self.assertIn("mailbox1.8=True", console)
        self.assertIn("decided source=model", console)
        self.assertIn("headers=5ms", console)
        for hidden in (*SECRETS, MESSAGE_ID, "me@example.com", "personal", "keep"):
            self.assertNotIn(hidden, console)

    def test_csp_report_logs_directive_and_origin_only(self):
        report = {"csp-report": {"blocked-uri": "https://cdn.example/path/x.js?id=secret",
                                 "violated-directive": "script-src-elem"}}
        response, _ = self.request("POST", "/csp-report", json.dumps(report).encode())
        self.assertEqual(204, response.status)
        self.assertEqual("[csp] directive=script-src-elem blocked=https://cdn.example", self.console())

    def test_diag_logs_codes_and_timings_only(self):
        response, _ = self.post({"error": f"step failed {SUBJECT}<b>", "timing": {"load": 12}}, "/diag")
        self.assertEqual(204, response.status)
        self.assertIn("timing load=12ms", self.console())
        self.assertNotIn("<", self.console())


class CertsTests(TempHomeCase):
    def run_result(self, stdout="", returncode=0):
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")

    def test_paths_live_in_a_private_folder_under_the_home(self):
        cert, key = certs.paths()
        self.assertEqual(self.folder.resolve(), cert.parent.parent.resolve())
        self.assertEqual(cert.parent, key.parent)
        self.assertEqual(0o700, stat.S_IMODE(cert.parent.stat().st_mode))

    def test_is_trusted(self):
        with mock.patch.object(certs.sys, "platform", "darwin"), \
                mock.patch.object(certs.subprocess, "run") as run:
            run.return_value = self.run_result(f"Cert 0: Common Name: {certs.SUBJECT}\n")
            self.assertIs(True, certs.is_trusted())
            self.assertEqual(["security", "dump-trust-settings"], run.call_args.args[0])
            run.return_value = self.run_result("Cert 0: Common Name: Something Else\n")
            self.assertIs(False, certs.is_trusted())
        with mock.patch.object(certs.sys, "platform", "linux"), \
                mock.patch.object(certs.subprocess, "run") as run:
            self.assertIsNone(certs.is_trusted())
            run.assert_not_called()

    def test_trust_uses_the_login_keychain(self):
        cert = self.folder / "localhost.pem"
        with mock.patch.object(certs.sys, "platform", "darwin"), mock.patch.object(Path, "home", return_value=self.folder), \
                mock.patch.object(certs.subprocess, "run", return_value=self.run_result()) as run:
            certs.trust(cert)
        keychain = str(self.folder / "Library/Keychains/login.keychain-db")
        run.assert_called_once()
        self.assertEqual(["security", "add-trusted-cert", "-r", "trustRoot", "-p", "ssl", "-k", keychain, str(cert)],
                         run.call_args.args[0])

    def test_trust_failure_and_non_macos(self):
        with mock.patch.object(certs.sys, "platform", "darwin"), \
                mock.patch.object(certs.subprocess, "run", return_value=self.run_result(returncode=1)):
            with self.assertRaises(certs.CertError):
                certs.trust(self.folder / "localhost.pem")
        with mock.patch.object(certs.sys, "platform", "linux"), mock.patch.object(certs.subprocess, "run") as run:
            with self.assertRaises(certs.CertError):
                certs.trust(self.folder / "localhost.pem")
            run.assert_not_called()

    def patched_ensure(self, renew, trusted):
        stack = contextlib.ExitStack()
        mocks = {
            "create": stack.enter_context(mock.patch.object(certs, "create")),
            "trust": stack.enter_context(mock.patch.object(certs, "trust")),
            "untrust": stack.enter_context(mock.patch.object(certs, "untrust")),
        }
        stack.enter_context(mock.patch.object(certs, "needs_renewal", return_value=renew))
        stack.enter_context(mock.patch.object(certs, "is_trusted", return_value=trusted))
        self.addCleanup(stack.close)
        return mocks

    def test_ensure_creates_and_trusts_when_needed(self):
        mocks = self.patched_ensure(renew=True, trusted=False)
        self.assertEqual(certs.paths()[0], certs.ensure(out=lambda line: None))
        mocks["create"].assert_called_once()
        mocks["trust"].assert_called_once_with(certs.paths()[0])
        mocks["untrust"].assert_not_called()  # nothing to untrust when no certificate existed

    def test_ensure_does_nothing_when_valid_and_trusted(self):
        mocks = self.patched_ensure(renew=False, trusted=True)
        certs.ensure(out=lambda line: None)
        for mocked in mocks.values():
            mocked.assert_not_called()

    def test_ensure_renews_an_existing_certificate_and_skips_trust_where_unknown(self):
        certs.paths()[0].write_text("synthetic")
        mocks = self.patched_ensure(renew=True, trusted=None)
        certs.ensure(out=lambda line: None)
        mocks["untrust"].assert_called_once()
        mocks["create"].assert_called_once()
        mocks["trust"].assert_not_called()


class ServiceTests(TempHomeCase):
    def setUp(self):
        super().setUp()
        self.user = Path(tempfile.mkdtemp(dir=self.folder))
        for patcher in (mock.patch.object(Path, "home", return_value=self.user),
                        mock.patch.object(service.sys, "platform", "darwin")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.loaded = False
        self.bootstrap_ok = True
        self.calls = []
        runner = mock.patch.object(service.subprocess, "run", side_effect=self.fake_run)
        runner.start()
        self.addCleanup(runner.stop)

    def fake_run(self, command, **kwargs):
        self.calls.append(command)
        code = 0
        if command[:2] == ["launchctl", "print"]:
            code = 0 if self.loaded else 1
        elif command[:2] == ["launchctl", "bootstrap"]:
            code = 0 if self.bootstrap_ok else 1
        return subprocess.CompletedProcess(command, code, stdout="", stderr="")

    def test_definition(self):
        plan = service.definition()
        self.assertEqual("org.mail-triage.addin", plan["Label"])
        self.assertEqual([sys.executable, "-m", "mail_triage.addin.server"], plan["ProgramArguments"])
        self.assertIs(True, plan["RunAtLoad"])
        self.assertEqual({"SuccessfulExit": False}, plan["KeepAlive"])
        logs = (self.folder / "logs").resolve()
        for key in ("StandardOutPath", "StandardErrorPath"):
            self.assertEqual(logs, Path(plan[key]).resolve().parent)
        self.assertEqual(str(service.ROOT), plan["EnvironmentVariables"]["PYTHONPATH"])
        self.assertEqual(str(self.folder), plan["EnvironmentVariables"]["MAIL_TRIAGE_HOME"])
        self.assertEqual([*plan["ProgramArguments"][:3], "--config", "/synthetic/config.toml"],
                         service.definition(Path("/synthetic/config.toml"))["ProgramArguments"])

    def test_install_writes_private_plist_and_bootstraps(self):
        service.install()
        path = service.plist_path()
        self.assertTrue(path.resolve().is_relative_to(self.user.resolve()))
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        with path.open("rb") as handle:
            self.assertEqual(service.definition(), plistlib.load(handle))
        self.assertIn(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], self.calls)
        self.assertNotIn(["launchctl", "bootout", f"gui/{os.getuid()}/{service.LABEL}"], self.calls)

    def test_install_replaces_a_loaded_service(self):
        self.loaded = True
        service.install()
        self.assertLess(self.calls.index(["launchctl", "bootout", f"gui/{os.getuid()}/{service.LABEL}"]),
                        self.calls.index(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(service.plist_path())]))

    def test_install_reports_bootstrap_failure(self):
        self.bootstrap_ok = False
        with self.assertRaises(service.ServiceError):
            service.install()

    def test_install_on_other_platforms_fails_cleanly(self):
        with mock.patch.object(service.sys, "platform", "linux"):
            with self.assertRaises(service.ServiceError):
                service.install()
        self.assertFalse(service.plist_path().exists())
        self.assertEqual([], self.calls)

    def test_start_requires_install(self):
        with self.assertRaises(service.ServiceError):
            service.start()

    def test_uninstall_removes_the_plist(self):
        service.install()
        self.loaded = True
        service.uninstall()
        self.assertFalse(service.plist_path().exists())
        self.assertIn(["launchctl", "bootout", f"gui/{os.getuid()}/{service.LABEL}"], self.calls)
        service.uninstall()  # already gone: no error

    def test_healthy_is_false_without_a_certificate(self):
        with mock.patch.object(service.urllib.request, "urlopen") as urlopen:
            self.assertFalse(service.healthy())
            urlopen.assert_not_called()


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_status(self):
        with TempHome() as home, \
                mock.patch.object(service, "plist_path", return_value=home / "none.plist"), \
                mock.patch.object(service, "loaded", return_value=False), \
                mock.patch.object(service, "healthy", return_value=False), \
                mock.patch.object(service, "log_path", return_value=home / "addin.log"), \
                mock.patch.object(certs, "is_trusted", return_value=None):
            code, out = self.run_cli("addin", "status")
        self.assertEqual(0, code)
        self.assertIn("background service: not installed", out)
        self.assertIn("not responding", out)
        self.assertNotIn("Traceback", out)

    def test_start_failure_is_a_clean_message(self):
        with mock.patch.object(service, "start", side_effect=service.ServiceError("not installed yet")):
            code, out = self.run_cli("addin", "start")
        self.assertEqual(1, code)
        self.assertIn("✗ not installed yet", out)
        self.assertNotIn("Traceback", out)


class ManifestTests(unittest.TestCase):
    def test_manifest(self):
        root = ET.parse(STATIC / "manifest-readonly.xml").getroot()
        elements = list(root.iter())
        local = lambda e: e.tag.rsplit("}", 1)[-1]  # noqa: E731
        permissions = [e for e in elements if local(e) == "Permissions"]
        self.assertEqual(["ReadItem"], [e.text.strip() for e in permissions])
        urls = [(local(e), e.get("DefaultValue")) for e in elements if (e.get("DefaultValue") or "").startswith("https://")]
        self.assertTrue(urls)
        for name, url in urls:
            if name != "SupportUrl":
                self.assertTrue(url.startswith(f"https://localhost:{server.PORT}/"), (name, url))
        version = next(e.text for e in root if local(e) == "Version")
        self.assertGreaterEqual(tuple(int(p) for p in version.split(".")), (1, 0))

    def test_batch_manifest(self):
        single = ET.parse(STATIC / "manifest-readonly.xml").getroot()
        root = ET.parse(STATIC / "manifest.xml").getroot()
        local = lambda e: e.tag.rsplit("}", 1)[-1]  # noqa: E731
        value = lambda tree, name: [e.text.strip() for e in tree.iter() if local(e) == name]  # noqa: E731
        self.assertEqual(["ReadWriteMailbox"], value(root, "Permissions"))  # Microsoft requirement for multi-select
        self.assertEqual(["true"], value(root, "SupportsMultiSelect"))
        self.assertNotEqual(value(single, "Id"), value(root, "Id"))
        for element in root.iter():
            url = element.get("DefaultValue") or ""
            if url.startswith("https://") and local(element) != "SupportUrl":
                self.assertTrue(url.startswith(f"https://localhost:{server.PORT}/"), url)

    def test_task_pane_never_writes_to_the_mailbox(self):
        script = (STATIC / "taskpane.js").read_text()
        for forbidden in (".categories", "moveAsync", "makeEwsRequestAsync", "getCallbackTokenAsync", "saveAsync",
                          "removeAttachment", "setAsync", "innerHTML", "eval("):
            self.assertFalse(forbidden in script, forbidden)
        for endpoint in ('"/triage"', '"/known"', '"/report"', '"/diag"'):
            self.assertIn(endpoint, script)
        self.assertNotIn("http://", script)


if __name__ == "__main__":
    unittest.main()
