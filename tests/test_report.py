"""The interactive HTML report: self-contained, script pinned by hash, mail data never becomes markup."""

import base64
import hashlib
import json
import re
import unittest

from helpers import TempHome, default_config

from mail_triage import report
from mail_triage.policy import Decision
from mail_triage.store import Store


HOSTILE = '</script><script>alert(1)</script><img src=x onerror=alert(2)> & "quotes"'


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        folder = self.home.__enter__()
        self.store = Store(folder / "history.sqlite")
        rows = [("junk", "promotion", "Laya/Promotions", "model"), ("keep", "personal", "", "memory"),
                ("review", "review", "Laya/Review", "rule")]
        for i, (verdict, category, target, source) in enumerate(rows):
            self.store.record(key=f"k{i}", connector="applemail", account="acct", mailbox="INBOX", native_id=str(i),
                              sender=f"s{i}@shop.example.com", subject=HOSTILE if i == 0 else f"Subject {i}",
                              received="2026-10-21T08:00:00", decision=Decision(verdict, category, target, 0.5,
                                                                                "likely", ("reason <b>",)),
                              source=source, model="", fingerprint="x", store_subject=True)
        self.store.commit()
        self.html = report.write(self.store, default_config(), folder / "reports")[0].read_text()

    def tearDown(self):
        self.store.close()
        self.home.__exit__(None, None, None)

    def test_only_the_hashed_inline_script_may_run(self):
        csp = re.search(r'Content-Security-Policy content="([^"]+)"', self.html).group(1)
        self.assertIn("default-src 'none'", csp)
        scripts = re.findall(r"<script>(.*?)</script>", self.html, re.S)
        self.assertEqual(len(scripts), 1)
        digest = base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
        self.assertIn(f"script-src 'sha256-{digest}'", csp)
        self.assertNotIn("unsafe-inline' 'sha", csp)

    def test_mail_data_cannot_become_markup(self):
        self.assertNotIn("<script>alert", self.html)
        self.assertNotIn("<img", self.html)
        self.assertNotIn("reason <b>", self.html)
        data = re.search(r'<script type=application/json id=mt-data>(.*?)</script>', self.html, re.S).group(1)
        rows = json.loads(data)["rows"]
        self.assertEqual({r["s"] for r in rows} & {HOSTILE}, {HOSTILE})    # intact, only escaped
        self.assertNotIn("innerHTML", report._JS)
        self.assertNotIn("insertAdjacentHTML", report._JS)
        self.assertNotIn("eval(", report._JS)

    def test_loads_nothing_from_the_network(self):
        self.assertIsNone(re.search(r"(src|href)=['\"]?https?:", self.html))
        self.assertNotIn("fetch(", report._JS)
        self.assertNotIn("XMLHttpRequest", report._JS)

    def test_overview_filters_and_table_are_present(self):
        for marker in ("id=verdict-bar", "id=source-bar", "id=q ", "id=f-c", "id=f-l", "id=f-b", "id=f-v",
                       "data-key=d", "data-key=f", "data-value=-model", "<noscript>"):
            self.assertIn(marker, self.html)


if __name__ == "__main__":
    unittest.main()
