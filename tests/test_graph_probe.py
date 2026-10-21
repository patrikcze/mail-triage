"""Tests for tools/graph_probe.py: synthetic data only, no msal, network or mailbox needed."""

import contextlib
import importlib.util
import io
from pathlib import Path
import socket
import subprocess
import sys
import types
import unittest
from unittest import mock
import urllib.parse

TOOLS = Path(__file__).resolve().parent.parent / "tools"
spec = importlib.util.spec_from_file_location("graph_probe", TOOLS / "graph_probe.py")
gp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gp)

TOKEN = "SYNTHETIC-TOKEN-xyz"
HINT = "alice@example.invalid"
SUBJECT = "Synthetic subject 42"
SENDER = "sender@example.com"
BODY = "Synthetic body text 7."
RAW = (f"Received: from mx.example.com by mx.example.invalid; Mon, 1 Jan 2024 00:00:00 +0000\r\n"
       f"Authentication-Results: mx.example.invalid; spf=pass\r\nMessage-ID: <m1@example.com>\r\n"
       f"From: {SENDER}\r\nTo: {HINT}\r\nSubject: {SUBJECT}\r\nContent-Type: text/plain\r\n\r\n{BODY}\r\n").encode()


def capture(func, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = func(*args, **kwargs)
    return result, out.getvalue()


def resp(status=200, body=b"{}", headers=None):
    return gp.GraphResponse(status, body, headers or {})


class RecordingTransport:
    """Fake GET-only transport; replies come from a list, the last one repeating."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, url, headers, timeout=30):
        self.calls.append((url, headers))
        return self.replies[min(len(self.calls), len(self.replies)) - 1]


class AuthErrorTest(unittest.TestCase):
    CODES = {700016: "app_registration_unavailable", 50011: "redirect_uri_mismatch", 65001: "consent_required",
             90094: "admin_approval_required", 53003: "conditional_access_blocked",
             53000: "conditional_access_device", 50076: "mfa_challenge", 50105: "user_not_assigned",
             7000218: "public_client_flows_disabled"}

    def test_error_codes_list_and_description_text(self):
        """Both the error_codes list and AADSTS text map to the same category."""
        for code, category in self.CODES.items():
            with self.subTest(code=code):
                self.assertEqual(gp.categorize_auth_error({"error_codes": [code]})[0], category)
                text = f"AADSTS{code}: synthetic failure"
                self.assertEqual(gp.categorize_auth_error({"error_description": text})[0], category)

    def test_text_fallbacks(self):
        """Broker, cancellation and unknown errors fall back to text matching."""
        broker = {"error_description": "SSO extension is inactive or is not available on this device."}
        self.assertEqual(gp.categorize_auth_error(broker)[0], "broker_unavailable")
        self.assertEqual(gp.categorize_auth_error({"error": "access_denied"})[0], "user_cancelled")
        self.assertEqual(gp.categorize_auth_error({"error_description": "User cancelled"})[0], "user_cancelled")
        self.assertEqual(gp.categorize_auth_error({"error": "weird"})[0], "unknown_auth_error")


class GraphErrorTest(unittest.TestCase):
    def test_categories(self):
        """HTTP status plus Graph error code select the category; malformed bodies are tolerated."""
        forbidden = b'{"error":{"code":"ErrorAccessDenied"}}'
        mailbox = b'{"error":{"code":"MailboxNotEnabledForRESTAPI"}}'
        self.assertEqual(gp.categorize_graph_error(401)[0], "graph_unauthorized")
        category, hint = gp.categorize_graph_error(403, forbidden)
        self.assertEqual(category, "graph_forbidden")
        self.assertIn("ErrorAccessDenied", hint)
        self.assertEqual(gp.categorize_graph_error(404, mailbox)[0], "mailbox_incompatible")
        self.assertEqual(gp.categorize_graph_error(404)[0], "not_found")
        self.assertEqual(gp.categorize_graph_error(429)[0], "throttled")
        for body in (b"not json", b"[1, 2]", "", b'{"error": "text"}'):
            with self.subTest(body=body):
                self.assertEqual(gp.categorize_graph_error(403, body)[0], "graph_forbidden")


class GraphClientTest(unittest.TestCase):
    def make(self, *replies):
        self.delays = []
        self.transport = RecordingTransport(*replies)
        return gp.Graph(TOKEN, self.transport, self.delays.append)

    def test_headers_and_url(self):
        """Requests carry the bearer token, ImmutableId preference and a Graph v1.0 URL."""
        graph = self.make(resp())
        graph.get("/me/messages", {"$top": "1", "$select": "id,isRead"})
        url, headers = self.transport.calls[0]
        self.assertTrue(url.startswith("https://graph.microsoft.com/v1.0"))
        self.assertEqual(headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(headers["Prefer"], 'IdType="ImmutableId"')

    def test_retries_throttling_then_succeeds(self):
        """A 429 is retried and the final response is returned."""
        graph = self.make(resp(429, headers={"Retry-After": "3"}), resp(200))
        self.assertEqual(graph.get("/me").status, 200)
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(self.delays, [3.0])

    def test_retry_after_is_capped(self):
        """An excessive Retry-After never sleeps longer than MAX_RETRY_AFTER."""
        graph = self.make(resp(429, headers={"Retry-After": "9999"}), resp(200))
        graph.get("/me")
        self.assertEqual(self.delays, [gp.MAX_RETRY_AFTER])

    def test_gives_up_after_max_retries(self):
        """Persistent throttling returns the last 429 after MAX_RETRIES attempts."""
        graph = self.make(resp(429, headers={"Retry-After": "1"}))
        self.assertEqual(graph.get("/me").status, 429)
        self.assertEqual(len(self.transport.calls), gp.MAX_RETRIES)
        self.assertEqual(len(self.delays), gp.MAX_RETRIES - 1)

    def test_repr_hides_token(self):
        """repr() never exposes the access token."""
        self.assertNotIn(TOKEN, repr(self.make(resp())))


class UrllibGetTest(unittest.TestCase):
    def test_refuses_non_graph_targets(self):
        """Other hosts and plain http:// raise ProbeBlocked before any network use."""
        for url in ("https://example.com/v1.0/me", "http://graph.microsoft.com/v1.0/me",
                    "https://graph.microsoft.com.example.invalid/v1.0/me"):
            with self.subTest(url=url), mock.patch.object(gp.urllib.request, "urlopen") as urlopen:
                with self.assertRaises(gp.ProbeBlocked):
                    gp.urllib_get(url, {})
                urlopen.assert_not_called()


class ScopesTest(unittest.TestCase):
    def test_requested_scopes_are_read_only(self):
        """Stage selection picks Mail.ReadBasic or Mail.Read and never a write scope."""
        self.assertEqual(gp.requested_scopes(0), ["Mail.ReadBasic"])
        self.assertEqual(gp.requested_scopes(1), ["Mail.Read"])
        for level in (0, 1, 2):
            joined = " ".join(gp.requested_scopes(level)).lower()
            self.assertFalse(any(marker in joined for marker in gp.WRITE_SCOPE_MARKERS))


class FakeApp:
    """Stand-in for msal.PublicClientApplication with scripted results."""
    CONSOLE_WINDOW_HANDLE = 0
    accounts = []
    silent = None
    interactive = {}
    instances = []

    def __init__(self, client_id, authority=None, **kwargs):
        self.silent_calls, self.interactive_calls = [], []
        type(self).instances.append(self)

    def get_accounts(self, username=None):
        return list(self.accounts)

    def acquire_token_silent(self, scopes, account=None):
        self.silent_calls.append(scopes)
        return self.silent

    def acquire_token_interactive(self, scopes, **kwargs):
        self.interactive_calls.append(kwargs)
        return dict(self.interactive)


def run_stage_b(**attrs):
    app_class = type("App", (FakeApp,), {"instances": [], **attrs})
    module = types.SimpleNamespace(PublicClientApplication=app_class)
    result, out = capture(gp.stage_b, "00000000-0000-0000-0000-000000000000", "example.invalid", HINT,
                          ["Mail.ReadBasic"], msal_module=module)
    return result, out, app_class.instances[0]


class StageBTest(unittest.TestCase):
    def test_browser_when_broker_disabled(self):
        """Without a broker the flow is reported as system browser."""
        _, out, _ = run_stage_b(interactive={"access_token": TOKEN})
        self.assertIn("system browser", out)

    def test_broker_flag_reported(self):
        """An app with the broker enabled is reported as broker."""
        _, out, _ = run_stage_b(_enable_broker=True, interactive={"access_token": TOKEN})
        self.assertIn("auth_method: broker", out)

    def test_existing_account_uses_silent_flow(self):
        """A cached account succeeds silently and skips the interactive prompt."""
        result, out, app = run_stage_b(accounts=[{"home_account_id": "x"}], silent={"access_token": TOKEN})
        self.assertEqual(result["access_token"], TOKEN)
        self.assertEqual(app.interactive_calls, [])
        self.assertIn("silent: success", out)

    def test_interactive_failure_is_categorized_and_redacted(self):
        """Failures print the category and a truncated correlation id only."""
        failure = {"error": "invalid_grant", "error_codes": [90094], "correlation_id": "abcdef0123456789"}
        result, out, _ = run_stage_b(interactive=failure)
        self.assertIsNone(result)
        self.assertIn("admin_approval_required", out)
        self.assertIn("abcdef01…", out)
        self.assertNotIn("abcdef0123456789", out)

    def test_success_never_prints_secrets(self):
        """Neither the token nor the login hint reaches stdout."""
        result, out, _ = run_stage_b(interactive={"access_token": TOKEN, "scope": "Mail.ReadBasic"})
        self.assertEqual(result["access_token"], TOKEN)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(HINT, out)


class StageCTest(unittest.TestCase):
    def test_granted_scope_satisfies_request(self):
        """Full-URL scope names are matched case-insensitively by their last segment."""
        ok, out = capture(gp.stage_c, {"scope": "https://graph.microsoft.com/Mail.Read openid profile"}, ["Mail.Read"])
        self.assertTrue(ok)
        self.assertIn("authorization: granted", out)

    def test_missing_scope(self):
        """A missing scope fails and is named."""
        ok, out = capture(gp.stage_c, {"scope": "openid profile"}, ["Mail.Read"])
        self.assertFalse(ok)
        self.assertIn("missing", out)

    def test_write_scope_warns(self):
        """A token with Mail.ReadWrite triggers a warning line."""
        _, out = capture(gp.stage_c, {"scope": "Mail.ReadWrite Mail.Read"}, ["Mail.Read"])
        self.assertIn("[C] warning:", out)


class StageDTest(unittest.TestCase):
    def graph(self, reply):
        self.transport = RecordingTransport(reply)
        return gp.Graph(TOKEN, self.transport, lambda _: None)

    def test_lists_ids_without_printing_them(self):
        """Two items plus nextLink are returned, counted, and not echoed; the query is minimal."""
        body = (b'{"value":[{"id":"SYNTH-ID-1","isRead":true},{"id":"SYNTH-ID-2","isRead":false}],'
                b'"@odata.nextLink":"https://graph.microsoft.com/v1.0/next"}')
        items, out = capture(gp.stage_d, self.graph(resp(200, body)), 2)
        self.assertEqual([i["id"] for i in items], ["SYNTH-ID-1", "SYNTH-ID-2"])
        self.assertIn("messages_retrieved: 2", out)
        self.assertIn("has_next_page: True", out)
        self.assertNotIn("SYNTH-ID", out)
        query = urllib.parse.urlsplit(self.transport.calls[0][0])
        self.assertTrue(query.path.endswith("/me/mailFolders/inbox/messages"))
        self.assertEqual(urllib.parse.parse_qs(query.query), {"$top": ["2"], "$select": ["id,isRead"]})

    def test_empty_inbox(self):
        """An empty inbox returns an empty list."""
        items, out = capture(gp.stage_d, self.graph(resp(200, b'{"value":[]}')), 1)
        self.assertEqual(items, [])
        self.assertIn("messages_retrieved: 0", out)

    def test_forbidden(self):
        """A 403 returns None and prints the category."""
        items, out = capture(gp.stage_d, self.graph(resp(403, b'{"error":{"code":"ErrorAccessDenied"}}')), 1)
        self.assertIsNone(items)
        self.assertIn("graph_forbidden", out)


class InspectMimeTest(unittest.TestCase):
    def test_full_message(self):
        """A message with transport headers and a text body reports every fact."""
        facts = gp.inspect_mime(RAW)
        self.assertTrue(all(facts.values()), facts)

    def test_missing_transport_headers(self):
        """Messages without Received/Authentication-Results report False for them."""
        raw = f"From: {SENDER}\r\nSubject: {SUBJECT}\r\n\r\n{BODY}\r\n".encode()
        facts = gp.inspect_mime(raw)
        self.assertFalse(facts["has_received"])
        self.assertFalse(facts["has_authentication_results"])
        self.assertFalse(facts["has_message_id"])

    def test_only_booleans_returned(self):
        """No header value or body text can leak through the result."""
        for raw in (RAW, b"", b"garbage"):
            self.assertTrue(all(type(v) is bool for v in gp.inspect_mime(raw).values()))


class StageETest(unittest.TestCase):
    def run_stage(self, is_read_sequence):
        reads = iter(is_read_sequence)
        calls = []

        def transport(url, headers, timeout=30):
            calls.append(url)
            if url.endswith("/$value"):
                return resp(200, RAW)
            return resp(200, ('{"isRead": %s}' % str(next(reads)).lower()).encode())

        graph = gp.Graph(TOKEN, transport, lambda _: None)
        ok, out = capture(gp.stage_e, graph, [{"id": "SYNTH-ID-1"}])
        return ok, out, calls

    def assert_quiet_and_read_only(self, out, calls):
        for secret in (SUBJECT, SENDER, BODY, "SYNTH-ID-1", HINT):
            self.assertNotIn(secret, out)
        for url in calls:
            self.assertTrue(url.startswith("https://graph.microsoft.com/v1.0/me/messages/"))
            for verb in ("/move", "/send", "/copy"):
                self.assertNotIn(verb, url)

    def test_read_state_unchanged(self):
        """Reading MIME with a stable isRead flag succeeds and prints only status values."""
        ok, out, calls = self.run_stage([False, False])
        self.assertTrue(ok)
        self.assertIn("read_state_unchanged: True", out)
        self.assertEqual(len(calls), 3)
        self.assert_quiet_and_read_only(out, calls)

    def test_read_state_flip_is_detected(self):
        """A False→True flip between the two lookups fails the stage."""
        ok, out, calls = self.run_stage([False, True])
        self.assertFalse(ok)
        self.assertIn("read_state_unchanged: False", out)
        self.assert_quiet_and_read_only(out, calls)


class ConfirmReadTest(unittest.TestCase):
    def confirm(self, prompt):
        return capture(gp.confirm_read, 1, prompt)[0]

    def test_only_exact_read_confirms(self):
        """Only the literal READ (ignoring surrounding whitespace) confirms."""
        self.assertTrue(self.confirm(lambda _: "READ"))
        self.assertTrue(self.confirm(lambda _: " READ \n"))
        for answer in ("yes", "read", "READ it", ""):
            self.assertFalse(self.confirm(lambda _, a=answer: a))

    def test_eof_declines(self):
        """No terminal input counts as a refusal."""
        def prompt(_):
            raise EOFError
        self.assertFalse(self.confirm(prompt))


GUARD_CODE = """
import importlib.util, socket, sys
spec = importlib.util.spec_from_file_location("graph_probe", sys.argv[1])
gp = importlib.util.module_from_spec(spec); spec.loader.exec_module(gp)
gp.install_allowlist_guard()
try:
    socket.getaddrinfo("example.com", 443); print("DNS_LEAK")
except gp.ProbeBlocked: print("DNS_BLOCKED")
s = socket.socket()
try:
    s.connect(("1.1.1.1", 443)); print("CONNECT_LEAK")
except gp.ProbeBlocked: print("CONNECT_BLOCKED")
except OSError: print("CONNECT_LEAK")
finally: s.close()
s = socket.socket()
try:
    s.connect(("127.0.0.1", int(sys.argv[2]))); print("LOOPBACK_OPEN")
except gp.ProbeBlocked: print("LOOPBACK_BLOCKED")
except OSError: print("LOOPBACK_ALLOWED")
finally: s.close()
"""


class AllowlistGuardTest(unittest.TestCase):
    def test_guard_in_fresh_process(self):
        """DNS and connects to other hosts are blocked; loopback is not."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        run = subprocess.run([sys.executable, "-c", GUARD_CODE, str(TOOLS / "graph_probe.py"), str(port)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        markers = run.stdout.split()
        self.assertIn("DNS_BLOCKED", markers)
        self.assertIn("CONNECT_BLOCKED", markers)
        self.assertNotIn("LOOPBACK_BLOCKED", markers)
        self.assertNotIn("DNS_LEAK", markers)
        self.assertNotIn("CONNECT_LEAK", markers)


class MainTest(unittest.TestCase):
    def test_sign_in_without_prerequisites_stops_before_stage_b(self):
        """Missing msal/client id means no authentication attempt and exit code 2."""
        facts = {"msal": None, "client_id": "", "tenant": "", "broker": False}
        with mock.patch.object(gp, "stage_a", return_value=facts), \
                mock.patch.object(gp, "install_allowlist_guard"), \
                mock.patch.object(gp, "stage_b") as stage_b:
            code, out = capture(gp.main, ["--sign-in"])
        self.assertEqual(code, 2)
        self.assertIn("not attempted", out)
        stage_b.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class OAuthErrorFieldsTest(unittest.TestCase):
    def test_reports_codes_without_description_text(self):
        line = gp.oauth_error_fields({"error": "access_denied", "error_subcode": "cancel",
                                      "error_description": "AADSTS65004: user someone@example.com declined",
                                      "error_codes": [65004]})
        self.assertEqual(line, "error=access_denied subcode=cancel aadsts=65004")
        self.assertNotIn("example.com", line)
