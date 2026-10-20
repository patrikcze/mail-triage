import unittest

from helpers import eml

from mail_triage.mime import clean_body, html_to_text, model_state, parse_message, subject_signals


PROMO = """\
From: MegaShop <news@megashop.example.com>
Subject: -50% on everything this weekend only!
Message-ID: <promo-1@megashop.example.com>
List-Unsubscribe: <mailto:unsub@megashop.example.com>
Precedence: bulk
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="b"

--b
Content-Type: text/html; charset=utf-8

<html><head><style>p{color:red}</style></head><body><p>Huge&nbsp;sale!</p><p>Shop now and save.</p></body></html>
--b--
"""


def signal(name, subject="Hi", sender="a@b.example"):
    return parse_message(eml(sender, subject))["signals"][name]


class ParseTest(unittest.TestCase):
    def test_html_body_and_bulk_headers(self):
        mail = parse_message(PROMO)
        self.assertEqual(mail["from_address"], "news@megashop.example.com")
        self.assertIn("Huge sale!", mail["body"])
        self.assertNotIn("color", mail["body"])
        self.assertTrue(mail["signals"]["list_unsubscribe"])
        self.assertTrue(mail["signals"]["bulk"])
        self.assertTrue(mail["signals"]["noreply"])
        self.assertEqual(mail["headers"]["precedence"], "bulk")

    def test_8bit_utf8_body_and_encoded_subject(self):
        source = ("From: Obchod <info@obchod.example.cz>\nSubject: =?utf-8?q?Z=C3=ADtra?=\n"
                  "Content-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: 8bit\n\n"
                  "Objednejte ještě dnes a získejte dopravu zdarma.\n")
        mail = parse_message(source)
        self.assertEqual(mail["subject"], "Zítra")
        self.assertIn("ještě dnes a získejte", mail["body"])

    def test_reply_detection(self):
        mail = parse_message(eml("Jan <jan@company.example>", "Re: PR review", In_Reply_To="<x@company.example>"))
        self.assertTrue(mail["signals"]["is_reply"])

    def test_garbage_does_not_raise(self):
        self.assertEqual(parse_message("not really an email")["subject"], "")

    def test_oversized_source_is_capped(self):
        mail = parse_message(eml("a@b.example", "Big", "x" * 3_000_000))
        self.assertLess(len(mail["body"]), 10_000)

    def test_html_to_text_skips_scripts(self):
        self.assertEqual(html_to_text("<script>x()</script><b>Hi</b>").strip(), "Hi")

    def test_model_state_is_minimal(self):
        mail = parse_message(eml("Jan <jan@company.example>", "Hello", "Body text\n\nOn Mon, X wrote:\n> quoted secret"))
        state = model_state(mail)
        self.assertEqual(set(state), {"subject", "from", "body"})
        self.assertNotIn("quoted secret", state["body"])

    def test_clean_body_limit(self):
        self.assertEqual(len(clean_body("a" * 5000, 300)), 300)


class GuardTest(unittest.TestCase):
    def test_transactional_subjects(self):
        for subject in ("NOVA OBJEDNAVKA c. 7526001681", "Potvrzeni prijeti platby", "Your order #123",
                        "Vas zustatek na uctu", "Vase stvrzenka z bankomatu", "New login to your account",
                        "Ihre Rechnung", "Receipt from the store", "You shared some Account data with an app",
                        "Your temporary login code", "Your multi-factor authentication settings have been updated",
                        "Security alert", "Bezpečnostní upozornění", "Dispatched: 4 items"):
            self.assertTrue(signal("transactional", subject), subject)
        for subject in ("Order now and save 50%", "Free delivery this weekend", "Slevovy kod na podzim",
                        "Objednejte jeste dnes", "Podzimni vyprodej je tu"):
            self.assertFalse(signal("transactional", subject), subject)

    def test_notice_subjects(self):
        for subject in ("Příloha k Vaší smlouvě č. 0000000001", "Změna správce na pojistné smlouvě",
                        "Aktualizované podmínky členství", "Váš dočasný přístupový kód",
                        "Your free license key", "Your subscription access has been paused",
                        "Action Required: Important security update", "Secure your Cyber account by Oct 1",
                        "Provádíme změny smluvních podmínek", "Updates to our Terms of Service"):
            self.assertTrue(signal("notice", subject), subject)
        for subject in ("Podzimní výprodej je tu!", "Kupte poslední zlevněné vstupenky",
                        "Peníze už za pár minut na účtu", "Huge sale on security cameras"):
            self.assertFalse(signal("notice", subject), subject)

    def test_commercial_marker(self):
        self.assertTrue(signal("commercial_marker", "4,99 % ročně jen do konce dubna (OS)"))
        self.assertTrue(signal("commercial_marker", "Akce (obchodní sdělení)"))
        self.assertFalse(signal("commercial_marker", "Faktura OS-2026"))

    def test_account_sender(self):
        for sender in ("noreply-accounts@id.example.com", "invoice+statements@mail.example.com",
                       "payments@pay.example.cz", "security@bank.example.cz"):
            self.assertTrue(signal("account_sender", sender=sender), sender)
        for sender in ("news@shop.example.com", "jan.novak@company.example", "promotions@shop.example.net"):
            self.assertFalse(signal("account_sender", sender=sender), sender)

    def test_subject_signals_without_fetch(self):
        self.assertTrue(subject_signals("a@b.example", "Your receipt #1")["transactional"])


if __name__ == "__main__":
    unittest.main()
