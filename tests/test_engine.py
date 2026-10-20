"""End-to-end through the core engine with a fake connector and a deterministic fake model."""

import unittest

from helpers import FakeConnector, FakeRouter, TempHome, default_config, eml

from mail_triage import config as configmod
from mail_triage.engine import Engine, ScanOptions
from mail_triage.store import Store, message_key


SALE = [eml("Deals <deals@gadgets.example.com>", f"Big sale #{i}", "Huge discount today, shop now!",
            List_Unsubscribe="<mailto:u@gadgets.example.com>") for i in range(8)]
PERSONAL = eml("Mom <mom@family.example.org>", "Sunday lunch", "Are you coming on Sunday?")


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.folder = self.home.__enter__()
        self.store = Store(self.folder / "history.sqlite")
        self.routers = []

    def tearDown(self):
        self.store.close()
        self.home.__exit__(None, None, None)

    def router(self, config):
        router = FakeRouter()
        self.routers.append(router)
        return router

    def scan(self, sources, config=None, mode="new", limit=None):
        connector = FakeConnector(sources)
        engine = Engine(config or default_config(), self.store, connector, self.router, out=lambda line: None)
        counts = engine.scan(ScanOptions(limit=limit, mode=mode))
        return counts, connector

    def test_decides_and_never_repeats(self):
        counts, _ = self.scan(SALE[:3] + [PERSONAL])
        self.assertEqual((counts["junk"], counts["keep"], counts["by_model"]), (3, 1, 4))
        counts, connector = self.scan(SALE[:3] + [PERSONAL])
        self.assertEqual((counts["skipped"], counts["by_model"], connector.fetched), (4, 0, 0))

    def test_sender_memory_skips_fetch_and_model(self):
        self.scan(SALE[:5])
        counts, connector = self.scan(SALE[5:8])
        self.assertEqual(counts["by_memory"], 3)
        self.assertEqual(connector.fetched, 0)

    def test_memory_rechecks_periodically(self):
        config = default_config(learning={**default_config().learning, "recheck_every": 2, "min_samples": 2})
        self.scan(SALE[:2], config)
        counts, _ = self.scan(SALE[2:6], config)
        self.assertEqual((counts["by_memory"], counts["by_model"]), (2, 2))

    def test_receipt_from_known_sender_still_gets_model(self):
        self.scan(SALE[:5])
        receipt = eml("Deals <deals@gadgets.example.com>", "Your order #77 has shipped", "Tracking inside.")
        counts, connector = self.scan([receipt])
        self.assertEqual((counts["by_memory"], counts["by_model"], connector.fetched), (0, 1, 1))

    def test_rule_decides_without_fetch(self):
        rule = configmod.Rule(name="family", category="personal", domain="family.example.org")
        counts, connector = self.scan([PERSONAL], default_config(rules=(rule,)))
        self.assertEqual((counts["by_rule"], connector.fetched), (1, 0))

    def test_teaching_overrides_and_redecides(self):
        self.scan(SALE[:1])
        self.assertEqual(self.store.teach("domain", "gadgets.example.com", "personal"), 1)
        counts, _ = self.scan(SALE[:1])
        self.assertEqual((counts["by_teach"], counts["keep"]), (1, 1))

    def test_changed_settings_rescan_only_when_asked(self):
        self.scan([PERSONAL])
        changed = default_config(instructions={**default_config().instructions, "kind": "Which type?"})
        self.assertEqual(self.scan([PERSONAL], changed)[0]["by_model"], 0)
        self.assertEqual(self.scan([PERSONAL], changed, mode="changed")[0]["by_model"], 1)

    def test_limit(self):
        counts, _ = self.scan(SALE, limit=2)
        self.assertEqual(counts["junk"] + counts["keep"] + counts["review"], 2)

    def test_history_keeps_versions_and_stats_do_not_double_count(self):
        self.scan([PERSONAL])
        self.scan([PERSONAL], mode="all")
        key = message_key("acct", "", "0")
        rows = self.store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        self.assertEqual(rows, 2)
        self.assertEqual(self.store.profile("mom@family.example.org").total, 1)
        self.assertIsNotNone(key)

    def test_subjects_not_stored_when_disabled(self):
        config = default_config(privacy={**default_config().privacy, "store_subjects": False})
        self.scan([PERSONAL], config)
        self.assertEqual(self.store.db.execute("SELECT subject FROM messages").fetchone()[0], "")

    def test_forget_sender_erases_everything(self):
        self.scan(SALE[:3])
        self.assertEqual(self.store.forget_sender("deals@gadgets.example.com"), 3)
        self.assertIsNone(self.store.profile("deals@gadgets.example.com"))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
