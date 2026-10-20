import unittest

from helpers import NO_SIGNALS, answers, default_config

from mail_triage import config as configmod
from mail_triage.policy import (COARSE_LABELS, apply_temperature, decide, decide_category, likelihood,
                                questions)


C = default_config()


def run(kind, signals=None, fluent=True, config=C, **noul):
    return decide(config, answers(kind, **noul), {**NO_SIGNALS, **(signals or {})}, fluent=fluent)


class BandsTest(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(likelihood(0.95), "highly likely")
        self.assertEqual(likelihood(0.75), "likely")
        self.assertEqual(likelihood(0.5), "uncertain")
        self.assertEqual(likelihood(0.05), "highly unlikely")


class DecideTest(unittest.TestCase):
    def test_promotion_with_bulk_headers_is_junk_with_folder(self):
        d = run({"promotion": 0.7, "newsletter": 0.2, "personal": 0.1}, {"list_unsubscribe": True, "noreply": True},
                marketing=0.95)
        self.assertEqual((d.verdict, d.category, d.folder, d.likelihood),
                         ("junk", "promotion", "Laya/Promotions", "highly likely"))

    def test_personal_reply_is_kept(self):
        d = run({"work": 0.7, "personal": 0.2, "promotion": 0.1}, {"is_reply": True}, marketing=0.05)
        self.assertEqual((d.verdict, d.category, d.folder), ("keep", "work", ""))

    def test_model_alone_never_junks(self):
        d = run({"promotion": 1.0}, marketing=1.0, spam=1.0)
        self.assertEqual((d.likelihood, d.verdict, d.folder), ("highly likely", "review", "Laya/Review"))

    def test_transactional_subject_is_kept(self):
        d = run({"promotion": 0.5, "transactional": 0.5}, {"transactional": True}, marketing=1.0, spam=1.0)
        self.assertEqual(d.verdict, "keep")

    def test_notice_with_bulk_headers_is_never_junk(self):
        d = run({"promotion": 1.0}, {"list_unsubscribe": True, "esp": True, "notice": True}, marketing=1.0, spam=1.0)
        self.assertEqual(d.verdict, "review")
        self.assertIn("capped: contract or account notice", d.reasons)

    def test_account_sender_is_never_junk(self):
        d = run({"spam": 1.0}, {"list_unsubscribe": True, "account_sender": True}, marketing=1.0, spam=1.0)
        self.assertNotEqual(d.verdict, "junk")

    def test_commercial_marker_overrides_guards(self):
        d = run({"promotion": 0.9, "personal": 0.1}, {"commercial_marker": True, "notice": True, "transactional": True},
                marketing=0.95)
        self.assertEqual(d.verdict, "junk")
        d = run({"transactional": 0.6, "promotion": 0.4}, {"commercial_marker": True, "list_unsubscribe": True},
                marketing=1.0, spam=1.0)
        self.assertEqual(d.verdict, "junk")

    def test_unsupported_language_is_weighted_down(self):
        d = run({"spam": 0.99, "personal": 0.01}, fluent=False, marketing=0.99, spam=0.99)
        self.assertNotEqual(d.verdict, "junk")
        self.assertIn("language not supported by model: weight reduced", d.reasons)

    def test_signed_bulk_spam_is_relabelled_marketing(self):
        d = run({"spam": 0.6, "newsletter": 0.3, "promotion": 0.1}, {"list_unsubscribe": True}, spam=1.0, phishing=0.99)
        self.assertEqual((d.verdict, d.category, d.folder), ("junk", "newsletter", "Laya/Newsletters"))
        d = run({"spam": 0.6, "newsletter": 0.3, "promotion": 0.1}, {"list_unsubscribe": True, "auth_fail": True},
                spam=1.0, phishing=0.99)
        self.assertEqual(d.category, "spam")
        self.assertTrue(any("phishing" in r for r in d.reasons))

    def test_thresholds_come_from_config(self):
        strict = default_config(policy={**C.policy, "junk_threshold": 0.999})
        d = run({"promotion": 0.7, "personal": 0.3}, {"list_unsubscribe": True}, config=strict, marketing=0.95)
        self.assertEqual(d.verdict, "review")

    def test_custom_category_and_folder(self):
        cats = C.categories + (configmod.Category("invoices", "invoice I have to pay", "keep", "Finance/Invoices", ""),)
        custom = default_config(categories=cats)
        a = answers({"personal": 0.1})
        a["kind"]["probabilities"] = {**{n: 0.0 for n in custom.names()}, "invoices": 0.9, "personal": 0.1}
        d = decide(custom, a, NO_SIGNALS)
        self.assertEqual((d.verdict, d.category, d.folder), ("keep", "invoices", "Finance/Invoices"))
        self.assertIn("invoices", questions(custom)["kind"]["criteria"])


class MultilingualTest(unittest.TestCase):
    def test_coarse_questions_and_mapping(self):
        q = questions(C, multilingual=True)["kind"]["criteria"]
        self.assertEqual(q, list(COARSE_LABELS.values()))
        a = answers({"personal": 1.0})
        a["kind"]["probabilities"] = dict(zip(q, (0.05, 0.05, 0.9)))
        a.update(is_marketing={"type": "noul", "noul": 1.0}, is_spam={"type": "noul", "noul": 1.0})
        d = decide(C, a, {**NO_SIGNALS, "list_unsubscribe": True})
        self.assertEqual((d.verdict, d.category), ("junk", "promotion"))
        a["kind"]["probabilities"] = dict(zip(q, (0.0, 0.99, 0.01)))
        self.assertEqual(decide(C, a, {**NO_SIGNALS, "list_unsubscribe": True}).verdict, "review")


class TemperatureTest(unittest.TestCase):
    def test_temperature_softens_and_sharpens(self):
        a = answers({"promotion": 0.8, "personal": 0.2}, marketing=0.9)
        soft = apply_temperature(a, 2.0)
        sharp = apply_temperature(a, 0.5)
        self.assertLess(soft["kind"]["probabilities"]["promotion"], 0.8)
        self.assertGreater(sharp["kind"]["probabilities"]["promotion"], 0.8)
        self.assertLess(soft["is_marketing"]["noul"], 0.9)
        self.assertAlmostEqual(sum(soft["kind"]["probabilities"].values()), 1.0)
        self.assertIs(apply_temperature(a, 1.0), a)


class ShortcutDecisionTest(unittest.TestCase):
    def test_category_decisions(self):
        self.assertEqual(decide_category(C, "promotion", "rule: x").verdict, "junk")
        self.assertEqual(decide_category(C, "personal", "rule: x").folder, "")
        self.assertEqual(decide_category(C, "review", "rule: x").folder, "Laya/Review")


if __name__ == "__main__":
    unittest.main()
