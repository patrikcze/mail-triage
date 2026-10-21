"""Synthetic Outlook boundaries only. No test connects to a mailbox or imports real COM."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from email import policy
from email.parser import Parser
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from helpers import FakeConnector, FakeRouter, TempHome, default_config, eml

from mail_triage import cli
from mail_triage.backends.base import BackendError
from mail_triage.connectors.base import Connector, ConnectorError, Mailbox, Mover
from mail_triage.connectors.outlook import create_outlook
from mail_triage.connectors.outlook import common, macos, windows
from mail_triage.engine import Engine, ScanOptions
from mail_triage.mime import parse_message
from mail_triage.store import Store, message_key


def structured(headers="", *, subject="Big sale", html=False):
    return dict(headers=headers, sender="deals@shop.example.com", subject=subject,
                body="<p>Velka sleva! Shop now and save today.</p>" if html else
                     "Příliš žluťoučký kůň. Huge sale, shop now! BODY_PRIVATE_SENTINEL",
                html=html)


class FakeMac:
    def __init__(self, size=2, headers=""):
        self.size, self.headers = size, headers
        self.calls = []
        self.accounts = [
            dict(kind="exchangeAccounts", accountId="11", name="Work", email="work@example.org"),
            dict(kind="imapAccounts", accountId="22", name="Other", email="other@example.org"),
        ]

    def __call__(self, action, data):
        self.calls.append((action, data))
        if action == "discover":
            return self.accounts
        if action == "resolve":
            return {"folderId": data["accountId"] + "00"}
        if action == "list":
            return {"ids": list(range(1, self.size + 1)),
                    "senders": ["deals@shop.example.com"] * self.size,
                    "subjects": [f"Big sale {i}" for i in range(self.size)],
                    "dates": ["2026-10-01T12:00:00Z"] * self.size}
        if action == "fetch":
            return {ident: structured(self.headers, subject=f"Big sale {ident}") for ident in data["ids"]
                    if int(ident) <= self.size}
        if action == "probe":
            return {"count": self.size, "metadata": bool(self.size),
                    "sampled": data["readSample"] and bool(self.size),
                    "headers": bool(self.headers), "body": True, "unchanged": True}
        raise AssertionError(action)


def fake_mac(size=2, headers=""):
    with patch.object(macos, "inspect_installation", return_value={"version": "test", "build": "synthetic"}):
        connector = macos.MacOutlookConnector()
    boundary = FakeMac(size, headers)
    connector._call = Mock(side_effect=boundary)
    return connector, boundary


class NormalizationTest(unittest.TestCase):
    def test_missing_headers_stay_missing(self):
        source = common.normalize_source(**structured())
        mail = parse_message(source)
        self.assertTrue(mail["signals"]["headers_unavailable"])
        for key in ("list_unsubscribe", "bulk", "esp", "auth_fail", "automated"):
            self.assertFalse(mail["signals"][key])
        self.assertEqual(mail["message_id"], "")
        self.assertEqual(mail["date"], "")
        self.assertNotIn("authentication-results", mail["headers"])
        self.assertNotIn("list-unsubscribe", mail["headers"])

    def test_actual_headers_and_duplicate_received_are_preserved(self):
        headers = ("From: Actual <actual@example.org>\nSubject: Actual subject\n"
                   "Message-ID: <real@example.org>\nReceived: first\nReceived: second\n"
                   "List-Unsubscribe: <mailto:u@example.org>\nAuthentication-Results: mx; dkim=fail\n"
                   "Content-Type: multipart/mixed; boundary=old\nContent-Disposition: attachment\n\n")
        source = common.normalize_source(**structured(headers))
        msg = Parser(policy=policy.default).parsestr(source)
        mail = parse_message(source)
        self.assertEqual(msg.get_all("Received"), ["first", "second"])
        self.assertEqual(mail["subject"], "Actual subject")
        self.assertEqual(mail["from_address"], "actual@example.org")
        self.assertEqual(mail["message_id"], "<real@example.org>")
        self.assertTrue(mail["signals"]["auth_fail"])
        self.assertTrue(mail["signals"]["list_unsubscribe"])
        self.assertFalse(mail["signals"]["headers_unavailable"])
        self.assertEqual(list(msg.iter_attachments()), [])

    def test_unicode_plain_and_html(self):
        plain = parse_message(common.normalize_source(**structured(subject="Žluťoučký 日本語")))
        self.assertEqual(plain["subject"], "Žluťoučký 日本語")
        self.assertIn("Příliš žluťoučký", plain["body"])
        value = structured(html=True, subject="日本語")
        value["body"] = "<script>PRIVATE_SCRIPT</script><p>Český 日本語 text: výprodej dnes!</p>"
        html = parse_message(common.normalize_source(**value))
        self.assertIn("Český 日本語", html["body"])
        self.assertNotIn("PRIVATE_SCRIPT", html["body"])
        self.assertNotIn("<p>", html["body"])

    def test_properties_cannot_inject_header_evidence(self):
        value = structured(subject="Sale\nList-Unsubscribe: <mailto:u@example.org>")
        value["sender"] = "deals@example.org\r\nAuthentication-Results: dkim=fail"
        mail = parse_message(common.normalize_source(**value))
        self.assertFalse(mail["signals"]["list_unsubscribe"])
        self.assertFalse(mail["signals"]["auth_fail"])

    def test_provenance_is_local_not_an_untrusted_claim(self):
        value = structured("From: a@example.org\nX-Mail-Triage-Source-Provenance: structured-without-headers\n")
        source = common.normalize_source(**value)
        self.assertFalse(parse_message(source)["signals"]["headers_unavailable"])
        self.assertEqual(source.count("X-Mail-Triage-Source-Provenance:"), 1)

    def test_raw_mail_cannot_forge_the_missing_headers_marker(self):
        raw = ("From: deals@example.org\nSubject: Synthetic sale\n"
               "X-Mail-Triage-Source-Provenance: structured-without-headers\n\n"
               "A synthetic body that is long enough to be read.\n")
        self.assertFalse(parse_message(raw)["signals"]["headers_unavailable"])

    def test_actual_message_id_fallback_and_no_invented_date(self):
        source = common.normalize_source(**structured(), message_id="<supplied@example.org>")
        mail = parse_message(source)
        self.assertEqual(mail["message_id"], "<supplied@example.org>")
        self.assertEqual(mail["date"], "")

    def test_body_preview_is_bounded(self):
        value = structured()
        value["body"] = "A" * 100_000
        msg = Parser(policy=policy.default).parsestr(common.normalize_source(**value))
        self.assertEqual(len(msg.get_content().strip()), common.BODY_PREVIEW)

    def test_oversized_headers_fail_not_truncate_evidence(self):
        with self.assertRaises(ConnectorError):
            common.normalize_source(**structured("X-Large: " + "a" * 256_000))

    def test_malformed_native_results_do_not_leak_properties(self):
        for rows in ({"1": {"token": "TOKEN_PRIVATE"}}, {"1": dict(structured(), body=["PRIVATE"])}):
            with self.assertRaises(ConnectorError) as raised:
                common.normalize_sources(rows, ["1"])
            self.assertNotIn("PRIVATE", str(raised.exception))
        source = common.normalize_source(**structured(
            "MIME-Version: 9.9\nX-Mail-Triage-Source-Provenance: structured-with-headers\n"))
        mail = parse_message(source)
        self.assertTrue(mail["signals"]["headers_unavailable"])
        self.assertEqual(mail["headers"]["mime-version"], "1.0")


class BoundaryTest(unittest.TestCase):
    def reply(self, value):
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

    def test_fixed_script_and_parameters_as_data(self):
        connector, _ = fake_mac()
        parameter = '"; do shell script "evil"\n日本語'
        with patch.object(common.subprocess, "run", return_value=self.reply({"ok": True, "data": []})) as run:
            macos.MacOutlookConnector._call(connector, "discover", {"name": parameter})
        command = run.call_args.args[0]
        self.assertEqual(command[4], macos._SCRIPT)
        self.assertNotIn(parameter, command[4])
        self.assertEqual(json.loads(command[-1]), {"name": parameter})
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_native_stderr_is_not_disclosed(self):
        result = SimpleNamespace(returncode=1, stdout="", stderr="TOKEN_SECRET BODY_PRIVATE")
        with patch.object(common.subprocess, "run", return_value=result):
            with self.assertRaises(ConnectorError) as raised:
                common.run_json(["synthetic"])
        self.assertNotIn("TOKEN_SECRET", str(raised.exception))
        self.assertNotIn("BODY_PRIVATE", str(raised.exception))

    def test_permission_timeout_and_native_failures_are_safe(self):
        for code, expected in ((-1743, "permission denied"), (-1712, "timeout"),
                               ("changed", "without writes"), ("protected", "protected"),
                               ("native", "corporate policy"), ("pywin32", "pywin32"),
                               ("profile", "classic Outlook")):
            with self.subTest(code=code), patch.object(
                common.subprocess, "run", return_value=self.reply({"ok": False, "code": code,
                                                                  "message": "CREDENTIAL_PRIVATE"})):
                with self.assertRaises(ConnectorError) as raised:
                    common.run_json(["synthetic"])
                self.assertIn(expected, str(raised.exception))
                self.assertNotIn("CREDENTIAL_PRIVATE", str(raised.exception))
        with patch.object(common.subprocess, "run", side_effect=subprocess.TimeoutExpired("secret", 1)):
            with self.assertRaisesRegex(ConnectorError, "timed out"):
                common.run_json(["synthetic"])

    def test_invalid_json_and_envelope_fail_closed(self):
        for output in ("BODY_PRIVATE", "[]", "{}", '{"ok":true}', '{"ok":"yes","data":[]}'):
            with self.subTest(output=output), patch.object(
                common.subprocess, "run",
                return_value=SimpleNamespace(returncode=0, stdout=output, stderr="")):
                with self.assertRaises(ConnectorError) as raised:
                    common.run_json(["synthetic"])
                self.assertNotIn("BODY_PRIVATE", str(raised.exception))

    def test_only_whitelisted_operation_labels_are_diagnostic(self):
        for operation in ("headers", "CREDENTIAL_PRIVATE", {"private": "TOKEN_PRIVATE"}):
            with patch.object(common.subprocess, "run", return_value=self.reply(
                {"ok": False, "code": -1708, "operation": operation})):
                with self.assertRaises(ConnectorError) as raised:
                    common.run_json(["synthetic"])
                self.assertNotIn("PRIVATE", str(raised.exception))
                if operation == "headers":
                    self.assertIn("headers read failed (native code -1708)", str(raised.exception))

    def test_platform_dispatch_and_windows_isolation(self):
        with patch.object(sys, "platform", "linux"):
            with self.assertRaises(ConnectorError):
                create_outlook()
            with self.assertRaises(ConnectorError):
                windows.WindowsOutlookConnector()
            with self.assertRaises(ConnectorError):
                macos.inspect_installation()
        self.assertNotIn("win32com.client", sys.modules)
        with patch.object(sys, "platform", "darwin"), patch.object(macos, "MacOutlookConnector") as cls:
            self.assertIs(create_outlook(), cls.return_value)
        with patch.object(sys, "platform", "win32"), patch.object(windows, "WindowsOutlookConnector") as cls:
            self.assertIs(create_outlook(), cls.return_value)

    def test_public_dictionary_capability_detection(self):
        required = {
            "object": ["id"], "account": ["name", "email address", "inbox", "root folder"],
            "message": ["sender", "subject", "time received", "headers", "plain text content", "content",
                        "has html", "is read", "is rights protected", "is partially downloaded"]}
        xml = "<dictionary>" + "".join(
            '<class name="' + name + '">' + "".join('<property name="' + p + '"/>' for p in properties)
            + "</class>" for name, properties in required.items()) + "</dictionary>"
        info = {"CFBundleIdentifier": "com.microsoft.Outlook", "CFBundleShortVersionString": "test",
                "CFBundleVersion": "build"}
        with patch.object(sys, "platform", "darwin"), patch.object(Path, "is_dir", return_value=True), \
             patch.object(Path, "open", return_value=io.BytesIO()), \
             patch.object(macos.plistlib, "load", return_value=info), \
             patch.object(macos.subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout=xml.encode())):
            self.assertEqual(macos.inspect_installation()["version"], "test")
        with patch.object(sys, "platform", "darwin"), patch.object(Path, "is_dir", return_value=False):
            with self.assertRaisesRegex(ConnectorError, "not found"):
                macos.inspect_installation()
        with patch.object(sys, "platform", "darwin"), patch.object(Path, "is_dir", return_value=True), \
             patch.object(Path, "open", return_value=io.BytesIO()), \
             patch.object(macos.plistlib, "load", return_value=info), \
             patch.object(macos.subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout=b"<dictionary/>")):
            with self.assertRaisesRegex(ConnectorError, "lacks required"):
                macos.inspect_installation()


_JXA_STUB = r"""
var scenario = 'stable';
function FakeApplication(identifier) {
  if (identifier !== 'com.microsoft.Outlook') throw new Error('unexpected application');
  function message(id) {
    var reads = 0;
    return {
      id: function() { if (id === 99) throw {errorNumber: -1728}; return id; },
      isRead: function() { reads++; return scenario === 'changed' && reads > 1; },
      isRightsProtected: function() { return scenario === 'protected'; },
      isPartiallyDownloaded: function() { return false; },
      headers: function() {
        if (scenario === 'headers-error') throw {errorNumber: -1708};
        return '';
      },
      plainTextContent: function() { return 'Synthetic body, never a real mailbox'; },
      sender: function() { return {address: 'sender@example.org'}; },
      subject: function() { return 'Synthetic subject'; },
      timeReceived: function() { return new Date('2026-10-01T12:00:00Z'); }
    };
  }
  const messages = {
    id: function() { return [1, 2]; },
    isRead: function() { return [false, false]; },
    sender: function() { return [{address:'sender@example.org'}, {address:'other@example.org'}]; },
    subject: function() { return ['Synthetic 1', 'Synthetic 2']; },
    timeReceived: function() { return [new Date('2026-10-01T12:00:00Z'), null]; },
    byId: message
  };
  const folder = {id: function() { return 100; }, messages: messages};
  const account = {id: function() { return 11; }, name: function() { return 'Synthetic'; },
                   emailAddress: function() { return 'synthetic@example.org'; },
                   inbox: function() { return folder; }};
  function accounts() {
    if (scenario === 'denied') throw {errorNumber: -1743};
    return [account];
  }
  accounts.byId = function(id) { return account; };
  return {exchangeAccounts: accounts, imapAccounts: function() { return []; },
          popAccounts: function() { return []; }};
}
"""


@unittest.skipUnless(sys.platform == "darwin", "synthetic JXA execution requires macOS")
class SyntheticJxaTest(unittest.TestCase):
    def call(self, action, *, ids=None, scenario="stable", read_sample=False):
        # A lexical Application argument shadows the JXA bridge. No test contacts Outlook.
        script = _JXA_STUB + "\nvar scriptedRead = (function(Application) {\n" + macos._SCRIPT
        script += "\nreturn run; })(FakeApplication);\n"
        script += "function run(argv) { scenario = argv[2]; return scriptedRead(argv); }\n"
        data = {"kind": "exchangeAccounts", "accountId": "11", "path": "INBOX", "folderId": "100",
                "ids": ids or [], "readSample": read_sample}
        return common.run_json(["/usr/bin/osascript", "-l", "JavaScript", "-e", script,
                                action, json.dumps(data), scenario])

    def test_fixed_discover_resolve_and_metadata_scripts(self):
        self.assertEqual(len(self.call("discover")), 1)
        self.assertEqual(self.call("resolve"), {"folderId": "100"})
        metadata = self.call("list")
        self.assertEqual(metadata["ids"], [1, 2])
        self.assertEqual(metadata["dates"], ["2026-10-01T12:00:00.000Z", ""])
        self.assertNotIn("body", metadata)

    def test_selected_fetch_missing_message_and_read_probe(self):
        sources = self.call("fetch", ids=["2", "99"])
        self.assertEqual(set(sources), {"2"})
        self.assertIn("Synthetic body", sources["2"]["body"])
        self.assertFalse(self.call("probe")["sampled"])
        self.assertTrue(self.call("probe", read_sample=True)["unchanged"])

    def test_read_state_changes_protection_and_permissions_fail(self):
        for scenario in ("changed", "protected", "denied"):
            with self.subTest(scenario=scenario), self.assertRaises(ConnectorError):
                self.call("discover" if scenario == "denied" else "fetch", ids=["1"], scenario=scenario)
        with self.assertRaisesRegex(ConnectorError, "headers read failed"):
            self.call("fetch", ids=["1"], scenario="headers-error")


class MacConnectorTest(unittest.TestCase):
    def test_contract_and_no_write_interface(self):
        connector, _ = fake_mac()
        self.assertIsInstance(connector, Connector)
        self.assertNotIsInstance(connector, Mover)

    def test_multiple_accounts_default_to_own_inboxes(self):
        connector, boundary = fake_mac()
        boxes = connector.mailboxes()
        self.assertEqual(len(boxes), 2)
        self.assertTrue(all(box.name == "INBOX" for box in boxes))
        scopes = [connector.list(box)[0].identity_scope for box in boxes]
        self.assertNotEqual(*scopes)
        self.assertTrue(all(data["path"] == "INBOX" for action, data in boundary.calls if action == "resolve"))

    def test_account_name_email_and_explicit_folder(self):
        connector, boundary = fake_mac()
        self.assertEqual(connector.mailboxes(["Work"])[0].account, "work@example.org")
        box = connector.mailboxes(["work@example.org"], "Projects/Team")[0]
        self.assertEqual(box.name, "Projects/Team")
        self.assertEqual(boundary.calls[-1][1]["path"], "Projects/Team")

    def test_invalid_partial_account_and_ambiguous_account_fail(self):
        connector, boundary = fake_mac()
        for accounts in (["absent@example.org"], ["Work", "absent@example.org"]):
            with self.assertRaisesRegex(ConnectorError, "identifiers hidden"):
                connector.mailboxes(accounts)
        boundary.accounts.append(dict(boundary.accounts[0], accountId="99"))
        with self.assertRaisesRegex(ConnectorError, "ambiguous"):
            connector.mailboxes()
        boundary.accounts = [dict(boundary.accounts[0], email="first@example.org"),
                             dict(boundary.accounts[0], email="second@example.org", accountId="33")]
        with self.assertRaisesRegex(ConnectorError, "ambiguous"):
            connector.mailboxes(["Work"])

    def test_no_accounts_is_blocked_not_empty_success(self):
        connector, boundary = fake_mac()
        boundary.accounts = []
        with self.assertRaisesRegex(ConnectorError, "no script-visible"):
            connector.mailboxes()

    def test_empty_inbox_is_valid(self):
        connector, _ = fake_mac(size=0)
        box = connector.mailboxes(["Work"])[0]
        self.assertEqual(connector.list(box), [])
        self.assertEqual(connector.fetch_sources(box, []), {})
        ready, lines = connector.diagnose(accounts=["Work"], read_sample=True)
        self.assertFalse(ready)
        self.assertIn("UNVERIFIED", "\n".join(lines))

    def test_folder_selection_requires_single_account_and_safe_path(self):
        connector, _ = fake_mac()
        for accounts, path in (([], "Projects"), (["Work", "Other"], "Projects"),
                               (["Work"], "../Inbox"), (["Work"], "A//B"), (["Work"], "")):
            with self.assertRaises(ConnectorError):
                connector.mailboxes(accounts, path)

    def test_large_metadata_only_listing_is_one_boundary_call(self):
        connector, boundary = fake_mac(size=10_001)
        box = connector.mailboxes(["Work"])[0]
        boundary.calls.clear()
        infos = connector.list(box)
        self.assertEqual(len(infos), 10_001)
        self.assertEqual([action for action, _ in boundary.calls], ["list"])
        self.assertTrue(all(info.message_id == "" and info.headers_available is None for info in infos))

    def test_duplicate_native_ids_and_incomplete_columns_fail(self):
        connector, _ = fake_mac()
        box = connector.mailboxes(["Work"])[0]
        for data in ({"ids": [1, 1], "senders": ["", ""], "subjects": ["", ""], "dates": ["", ""]},
                     {"ids": [1, 2], "senders": [""], "subjects": ["", ""], "dates": ["", ""]}):
            connector._call = Mock(return_value=data)
            with self.assertRaises(ConnectorError):
                connector.list(box)

    def test_fetch_is_selective_and_missing_message_is_absent(self):
        connector, boundary = fake_mac()
        box = connector.mailboxes(["Work"])[0]
        result = connector.fetch_sources(box, ["2", "99"])
        self.assertEqual(set(result), {"2"})
        self.assertIn("BODY_PRIVATE_SENTINEL", parse_message(result["2"])["body"])
        self.assertEqual(boundary.calls[-1][1]["ids"], ["2", "99"])

    def test_fetch_batches_and_rejects_unrequested_content(self):
        connector, boundary = fake_mac(size=25)
        box = connector.mailboxes(["Work"])[0]
        result = connector.fetch_sources(box, list(map(str, range(1, 26))))
        self.assertEqual(len(result), 25)
        self.assertEqual([len(data["ids"]) for action, data in boundary.calls if action == "fetch"], [10, 10, 5])
        connector._call = Mock(return_value={"999": structured()})
        with self.assertRaisesRegex(ConnectorError, "unrequested"):
            connector.fetch_sources(box, ["1"])

    def test_unknown_mailbox_and_invalid_native_id(self):
        connector, _ = fake_mac()
        with self.assertRaises(ConnectorError):
            connector.list(Mailbox("unknown", "INBOX"))
        box = connector.mailboxes(["Work"])[0]
        for ident in ('"; malicious()', "NaN", "9007199254740992", "١٢"):
            with self.assertRaises(ConnectorError):
                connector.fetch_sources(box, [ident])
        with self.assertRaises(ConnectorError):
            connector.mailboxes(["absent@example.org"])
        with self.assertRaises(ConnectorError):
            connector.list(box)

    def test_doctor_does_not_claim_body_access_without_opt_in(self):
        connector, boundary = fake_mac()
        ready, lines = connector.diagnose(accounts=["Work"])
        self.assertFalse(ready)
        self.assertIn("UNVERIFIED", "\n".join(lines))
        self.assertNotIn("work@example.org", "\n".join(lines))
        self.assertFalse(next(data for action, data in boundary.calls if action == "probe")["readSample"])
        ready, lines = connector.diagnose(accounts=["Work"], read_sample=True)
        self.assertTrue(ready)
        self.assertIn("headers=missing", "\n".join(lines))
        self.assertNotIn("BODY_PRIVATE_SENTINEL", "\n".join(lines))

    def test_failures_are_not_silently_converted_to_empty_inboxes(self):
        connector, _ = fake_mac()
        box = connector.mailboxes(["Work"])[0]
        connector._call = Mock(side_effect=ConnectorError("Outlook permission denied"))
        with self.assertRaisesRegex(ConnectorError, "permission denied"):
            connector.list(box)
        with self.assertRaises(ConnectorError):
            connector.fetch_sources(box, ["1"])


class IdentityAndEngineTest(unittest.TestCase):
    def test_legacy_history_key_is_unchanged(self):
        expected = hashlib.sha256(b"acct\0same@example.org").hexdigest()
        self.assertEqual(message_key("acct", "<same@example.org>", "1"), expected)
        self.assertEqual(message_key("acct", "", "1"), hashlib.sha256(b"acct\0native:1").hexdigest())

    def test_scoped_keys_isolate_native_ids_accounts_folders_and_adapters(self):
        key = message_key("acct", "<same@example.org>", "1", scope="outlook-macos:11", mailbox="INBOX")
        self.assertEqual(key, message_key("acct", "", "1", scope="outlook-macos:11", mailbox="INBOX"))
        for account, native, scope, box in (("other", "1", "outlook-macos:11", "INBOX"),
                                          ("acct", "2", "outlook-macos:11", "INBOX"),
                                          ("acct", "1", "outlook-macos:22", "INBOX"),
                                          ("acct", "1", "outlook-windows:11", "INBOX"),
                                          ("acct", "1", "outlook-macos:11", "Other")):
            self.assertNotEqual(key, message_key(account, "<same@example.org>", native, scope=scope, mailbox=box))
        self.assertNotEqual(key, message_key("acct", "<same@example.org>", "1"))

    def test_outlook_flows_through_engine_preserves_history_and_never_stores_body(self):
        connector, boundary = fake_mac()
        with TempHome() as home:
            store = Store(home / "history.sqlite")
            output = []
            config = default_config(privacy={**default_config().privacy, "console": "counts"})
            engine = Engine(config, store, connector, FakeRouter, out=output.append)
            first = engine.scan(ScanOptions(accounts=("Work",), limit=1))
            self.assertEqual((first["review"], first["junk"], first["by_model"]), (1, 0, 1))
            second = engine.scan(ScanOptions(accounts=("Work",), limit=1))
            self.assertEqual((second["skipped"], second["by_model"]), (1, 1))
            third = engine.scan(ScanOptions(accounts=("Work",)))
            self.assertEqual((third["skipped"], third["by_model"]), (2, 0))
            self.assertNotIn("work@example.org", "\n".join(output))
            dump = "\n".join(store.db.iterdump())
            self.assertNotIn("BODY_PRIVATE_SENTINEL", dump)
            self.assertNotIn("Příliš", dump)
            self.assertEqual([len(data["ids"]) for action, data in boundary.calls if action == "fetch"], [1, 1])
            store.close()

    def test_missing_transport_headers_cap_even_commercial_marker(self):
        connector, _ = fake_mac()
        with TempHome() as home:
            store = Store(home / "history.sqlite")
            engine = Engine(default_config(), store, connector, FakeRouter, out=lambda line: None)
            source = common.normalize_source(**structured(subject="Big sale (OS)"))
            decision, _, _ = engine._classify(source)
            self.assertEqual(decision.verdict, "review")
            self.assertIn("capped: transport headers unavailable", decision.reasons)
            engine.router.close()
            store.close()

    def test_genuine_bulk_headers_still_work(self):
        connector, _ = fake_mac(headers="List-Unsubscribe: <mailto:u@shop.example.com>\n")
        with TempHome() as home:
            store = Store(home / "history.sqlite")
            counts = Engine(default_config(), store, connector, FakeRouter,
                            out=lambda line: None).scan(ScanOptions(accounts=("Work",)))
            self.assertEqual(counts["junk"], 2)
            store.close()

    def test_junk_memory_cannot_fill_missing_header_evidence(self):
        sources = [eml("deals@shop.example.com", f"Big sale old {i}",
                       List_Unsubscribe="<mailto:u@shop.example.com>") for i in range(5)]
        with TempHome() as home:
            store = Store(home / "history.sqlite")
            Engine(default_config(), store, FakeConnector(sources), FakeRouter,
                   out=lambda line: None).scan(ScanOptions())
            connector, boundary = fake_mac(size=1)
            counts = Engine(default_config(), store, connector, FakeRouter,
                            out=lambda line: None).scan(ScanOptions(accounts=("Work",)))
            self.assertEqual((counts["by_memory"], counts["by_model"], counts["junk"]), (0, 1, 0))
            self.assertTrue(any(action == "fetch" for action, _ in boundary.calls))
            store.close()


class WindowsTest(unittest.TestCase):
    def connector(self):
        with patch.object(sys, "platform", "win32"):
            return windows.WindowsOutlookConnector()

    def test_classic_contract_metadata_and_selective_fetch(self):
        connector = self.connector()
        calls = []

        def call(action, data):
            calls.append((action, data))
            if action == "discover":
                return {"version": "test", "withoutInbox": 1, "stores": [
                    {"storeId": "STORE1", "name": "Work", "email": "work@example.org"},
                    {"storeId": "STORE2", "name": "Shared", "email": ""}]}
            if action == "resolve":
                return {"folderId": "FOLDER"}
            if action == "list":
                return [dict(id="ENTRY1", mid="<duplicate@example.org>", sender="a@example.org",
                             subject="Hello", date=""),
                        dict(id="ENTRY2", mid="<duplicate@example.org>", sender="b@example.org",
                             subject="Other", date="")]
            if action == "fetch":
                return {"ENTRY2": dict(structured(), message_id="<duplicate@example.org>")}
            raise AssertionError(action)

        connector._call = Mock(side_effect=call)
        self.assertIsInstance(connector, Connector)
        self.assertNotIsInstance(connector, Mover)
        boxes = connector.mailboxes()
        self.assertEqual(len(boxes), 2)
        infos = connector.list(boxes[0])
        keys = [message_key(boxes[0].account, i.message_id, i.native_id,
                            scope=i.identity_scope, mailbox=boxes[0].name) for i in infos]
        self.assertNotEqual(*keys)
        sources = connector.fetch_sources(boxes[0], ["ENTRY1", "ENTRY2"])
        self.assertEqual(set(sources), {"ENTRY2"})
        self.assertEqual(calls[-1][1]["storeId"], "STORE1")

    def test_table_is_batched_metadata_only(self):
        rows = [("ENTRY" + str(i), "IPM.Note", "Hello", datetime(2026, 10, 1, tzinfo=timezone.utc),
                 "sender@example.org", "legacyDN", "<same@example.org>") for i in range(601)]

        class Table:
            Columns = Mock()

            def __init__(self):
                self.offset = 0
                self.requests = []

            @property
            def EndOfTable(self):
                return self.offset == len(rows)

            def GetArray(self, size):
                self.requests.append(size)
                batch = rows[self.offset:self.offset + size]
                self.offset += len(batch)
                return batch

        table = Table()
        folder = SimpleNamespace(GetTable=lambda: table)
        metadata = windows._table_rows(folder)
        self.assertEqual(len(metadata), 601)
        self.assertEqual(table.requests, [256, 256, 256])
        self.assertEqual(metadata[0]["sender"], "sender@example.org")
        self.assertNotIn("Body", [c.args[0] for c in table.Columns.Add.call_args_list])

    def test_missing_property_falls_back_only_for_not_found(self):
        class ComError(Exception):
            def __init__(self, hresult):
                self.hresult = hresult

        accessor = Mock()
        accessor.GetProperty.side_effect = [ComError(windows._NOT_FOUND), "ANSI header"]
        self.assertEqual(windows._property(SimpleNamespace(PropertyAccessor=accessor),
                                          windows._HEADERS, ComError), "ANSI header")
        accessor.GetProperty.side_effect = ComError(0x80070005)
        with self.assertRaises(ComError):
            windows._property(SimpleNamespace(PropertyAccessor=accessor), windows._HEADERS, ComError)

    def test_read_item_never_sets_properties_and_detects_read_state_changes(self):
        class ReadOnlyItem:
            Class, DownloadState, Permission, BodyFormat = 43, 1, 0, 1
            Body, Subject, SenderEmailType = "Synthetic plain body for testing", "Hello", "SMTP"
            SenderEmailAddress = "sender@example.org"
            PropertyAccessor = SimpleNamespace(GetProperty=lambda name: "")
            UnRead = True

            def __setattr__(self, name, value):
                raise AssertionError("mail mutation: " + name)

        self.assertIn("Synthetic", windows._read_item(ReadOnlyItem(), RuntimeError)["body"])

        class ChangedItem(ReadOnlyItem):
            reads = iter((True, False))

            @property
            def UnRead(self):
                return next(self.reads)

        with self.assertRaises(windows._NativeFailure) as raised:
            windows._read_item(ChangedItem(), RuntimeError)
        self.assertEqual(raised.exception.code, "changed")

    def test_worker_invocation_uses_json_stdin_and_timeout(self):
        connector = self.connector()
        with patch.object(windows, "run_json", return_value=[]) as run:
            connector._call("discover", {"account": "synthetic@example.org"})
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["action"], "discover")
        self.assertNotIn("synthetic@example.org", " ".join(run.call_args.args[0]))

    def test_com_worker_attaches_current_profile_and_omits_only_missing_selected_items(self):
        class ComError(Exception):
            def __init__(self, hresult):
                self.hresult = hresult

        folder = SimpleNamespace(EntryID="FOLDER1", StoreID="STORE1", Items=SimpleNamespace(Count=1))
        store = SimpleNamespace(StoreID="STORE1", DisplayName="Work", GetDefaultFolder=Mock(return_value=folder))
        item = SimpleNamespace(Parent=folder)

        def get_item(ident, store_id):
            self.assertEqual(store_id, "STORE1")
            if ident == "GONE":
                raise ComError(windows._NOT_FOUND)
            if ident == "DENIED":
                raise ComError(0x80070005)
            return item

        session = SimpleNamespace(Accounts=[SimpleNamespace(DeliveryStore=store, SmtpAddress="work@example.org")],
                                  Stores=[store], GetStoreFromID=Mock(return_value=store),
                                  GetItemFromID=Mock(side_effect=get_item))
        app = SimpleNamespace(Version="synthetic", GetNamespace=Mock(return_value=session))
        pythoncom, pywintypes, win32com, client = (ModuleType(name) for name in
                                                   ("pythoncom", "pywintypes", "win32com", "win32com.client"))
        pythoncom.CoInitialize, pythoncom.CoUninitialize = Mock(), Mock()
        pywintypes.com_error = ComError
        client.GetActiveObject = Mock(return_value=app)
        win32com.client = client
        modules = {"pythoncom": pythoncom, "pywintypes": pywintypes,
                   "win32com": win32com, "win32com.client": client}
        ref = {"storeId": "STORE1", "folderId": "FOLDER1", "path": "INBOX"}
        with patch.dict(sys.modules, modules), patch.object(windows, "_read_item", return_value=structured()) as read, \
             patch.object(windows, "_table_rows", return_value=[{"id": "ENTRY1"}]):
            discovered = windows._execute("discover", {})
            self.assertTrue(discovered["ok"])
            self.assertEqual(discovered["data"]["version"], "synthetic")
            fetched = windows._execute("fetch", {**ref, "ids": ["ENTRY1", "GONE"]})
            self.assertEqual(set(fetched["data"]), {"ENTRY1"})
            self.assertEqual(read.call_count, 1)
            read.reset_mock()
            self.assertFalse(windows._execute("probe", {**ref, "readSample": False})["data"]["sampled"])
            read.assert_not_called()
            self.assertTrue(windows._execute("probe", {**ref, "readSample": True})["data"]["sampled"])
            denied = windows._execute("fetch", {**ref, "ids": ["DENIED"]})
            self.assertFalse(denied["ok"])
            self.assertEqual(denied["code"], 0x80070005)
            self.assertEqual(pythoncom.CoInitialize.call_count, pythoncom.CoUninitialize.call_count)
        client.GetActiveObject.assert_called_with("Outlook.Application")
        app.GetNamespace.assert_called_with("MAPI")

    def test_table_long_subject_is_resolved_without_body_reads(self):
        table = SimpleNamespace(EndOfTable=False, Columns=Mock(), GetArray=Mock(return_value=[
            ("ENTRY1", "IPM.Note", "x" * 255, None, "sender@example.org", "", "<" + "m" * 300 + ">")]))
        resolve = Mock(return_value="Complete subject " + "x" * 300)
        rows = windows._table_rows(SimpleNamespace(GetTable=lambda: table), maximum=1, full_subject=resolve)
        resolve.assert_called_once_with("ENTRY1")
        self.assertEqual(rows[0]["subject"], resolve.return_value)
        self.assertEqual(rows[0]["mid"], "")


class CliAndPrivacyTest(unittest.TestCase):
    def test_source_selection_is_explicit_and_exports_unchanged(self):
        self.assertEqual(cli.parser().parse_args(["scan"]).source, "applemail")
        args = cli.parser().parse_args(["scan", "--source", "outlook", "--limit", "20"])
        with patch("mail_triage.connectors.outlook.create_outlook") as create:
            self.assertIs(cli._connector(args), create.return_value)
        args = cli.parser().parse_args(["scan", "--folder", "tests/fixtures/demo"])
        self.assertEqual(cli._connector(args).name, "emlfolder")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.parser().parse_args(["scan", "--source", "outlook", "--folder", "tests/fixtures/demo"])

    def test_applemail_default_factory_is_preserved(self):
        with patch("mail_triage.connectors.applemail.AppleMailConnector") as apple:
            self.assertIs(cli._connector(cli.parser().parse_args(["scan"])), apple.return_value)

    def test_counts_only_scan_and_doctor_do_not_leak_mail(self):
        connector, _ = fake_mac()
        with TempHome(), patch.object(cli, "_connector", return_value=connector), \
             patch.object(cli.privacy, "install_network_guard"), patch.object(cli, "create", side_effect=FakeRouter):
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(["scan", "--source", "outlook", "--account", "Work", "--limit", "1",
                                 "--console", "counts", "--no-report"])
            self.assertEqual(code, 0)
            self.assertIn("1 to review", output.getvalue())
            for private in ("work@example.org", "Big sale", "BODY_PRIVATE_SENTINEL"):
                self.assertNotIn(private, output.getvalue())
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(["doctor", "--source", "outlook"])
            self.assertEqual(code, 1)
            self.assertIn("provider: outlook", output.getvalue())
            self.assertIn("UNVERIFIED", output.getvalue())
            self.assertNotIn("work@example.org", output.getvalue())

    def test_scan_report_uses_resolved_account_not_selection_alias(self):
        connector, _ = fake_mac()
        with TempHome(), patch.object(cli, "_connector", return_value=connector), \
             patch.object(cli.privacy, "install_network_guard"), patch.object(cli, "create", side_effect=FakeRouter), \
             patch.object(cli.report, "write", return_value=(Path("synthetic.html"), Path("synthetic.csv"))) as write:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["scan", "--source", "outlook", "--account", "Work",
                                           "--limit", "1", "--console", "counts"]), 0)
            self.assertEqual(write.call_args.args[-1], ["work@example.org"])

    def test_empty_export_selection_keeps_existing_report_account_filter(self):
        with TempHome(), patch.object(cli.privacy, "install_network_guard"), \
             patch.object(cli, "create", side_effect=FakeRouter), \
             patch.object(cli.report, "write", return_value=(Path("synthetic.html"), Path("synthetic.csv"))) as write:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["scan", "--folder", "tests/fixtures/demo",
                                           "--account", "not-selected", "--console", "counts"]), 0)
            self.assertEqual(write.call_args.args[-1], ["not-selected"])

    def test_doctor_still_reports_connector_when_model_is_unavailable(self):
        connector, _ = fake_mac()
        with TempHome(), patch.object(cli, "_connector", return_value=connector), \
             patch.object(cli.privacy, "install_network_guard"), \
             patch.object(cli, "create", side_effect=BackendError("model not installed")):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(["doctor", "--source", "outlook"]), 1)
            self.assertIn("account/inbox discovery: 2", output.getvalue())

    def test_read_probe_requires_explicit_outlook_source(self):
        with redirect_stdout(io.StringIO()), patch.object(cli, "create") as create:
            self.assertEqual(cli.main(["doctor", "--probe-read"]), 1)
        create.assert_not_called()

    def test_outlook_scripts_have_no_mutations_credentials_or_attachment_access(self):
        script = macos._SCRIPT
        for forbidden in (".source(", ".attachments", ".open(", ".activate(", ".move(", ".save(",
                          ".password", ".smtpPassword", ".properties(", "doShellScript", ".selection"):
            self.assertNotIn(forbidden, script)
        self.assertNotRegex(script, r"\.isRead\s*=")
        self.assertIn("before !== msg.isRead()", script)
        self.assertIn("data.ids", script)
        windows_source = Path(windows.__file__).read_text()
        for forbidden in (".Save(", ".Move(", ".Display(", ".Attachments", ".Logon(", "Dispatch("):
            self.assertNotIn(forbidden, windows_source)
        self.assertNotRegex(windows_source, r"\.UnRead\s*=")
        self.assertIn("install_network_guard()", windows_source)
