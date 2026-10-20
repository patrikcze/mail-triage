import tempfile
import unittest
from pathlib import Path

from helpers import default_config

from mail_triage import config as configmod
from mail_triage.config import ConfigError


def load_text(text: str) -> configmod.Config:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "config.toml"
        path.write_text(text, encoding="utf-8")
        return configmod.load(path)


class ConfigTest(unittest.TestCase):
    def test_defaults_are_valid(self):
        c = default_config()
        self.assertEqual(c.names("junk"), ["newsletter", "promotion", "spam"])
        self.assertEqual(len(c.fingerprint()), 12)

    def test_user_values_override_defaults(self):
        c = load_text('[model]\ntemperature = 1.5\n[policy]\njunk_threshold = 0.95\n')
        self.assertEqual(c.model["temperature"], 1.5)
        self.assertEqual(c.policy["junk_threshold"], 0.95)
        self.assertEqual(c.policy["review_threshold"], 0.70)        # untouched default kept

    def test_fingerprint_changes_with_instructions_not_privacy(self):
        base = default_config()
        self.assertNotEqual(base.fingerprint(), load_text('[instructions]\nkind = "Which type?"\n').fingerprint())
        self.assertEqual(base.fingerprint(), load_text('[privacy]\nconsole = "counts"\n').fingerprint())

    def test_remote_model_server_is_refused(self):
        for url in ("http://192.168.1.10:8080", "https://api.example.com", "http://10.0.0.1"):
            with self.assertRaises(ConfigError, msg=url):
                load_text(f'[model.systemone]\nurl = "{url}"\n')
        self.assertTrue(configmod.is_loopback_url("http://127.0.0.1:8080"))
        self.assertTrue(configmod.is_loopback_url("http://localhost:8080"))
        self.assertTrue(configmod.is_loopback_url("http://[::1]:8080"))

    def test_invalid_values_are_explained(self):
        bad = [
            '[model]\nbackend = "cloud"\n',
            '[model]\ntemperature = 0\n',
            '[policy]\nreview_threshold = 0.95\njunk_threshold = 0.9\n',
            '[privacy]\nconsole = "loud"\n',
            '[[categories]]\nname = "only"\ndescription = "x"\naction = "keep"\n',
            '[[categories]]\nname = "bad name"\ndescription = "x"\naction = "junk"\n',
            '[[rules]]\ncategory = "promotion"\n',
            '[[rules]]\nsender = "a@b.example"\ncategory = "nope"\n',
            '[[rules]]\nsubject_regex = "("\ncategory = "promotion"\n',
            'this is = = not toml',
        ]
        for text in bad:
            with self.assertRaises(ConfigError, msg=text):
                load_text(text)

    def test_rules_and_categories_from_file(self):
        c = load_text('[[rules]]\nname = "shop"\ndomain = "shop.example.cz"\ncategory = "promotion"\n')
        self.assertEqual(c.rules[0].domain, "shop.example.cz")


if __name__ == "__main__":
    unittest.main()
