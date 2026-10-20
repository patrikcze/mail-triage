"""Shared test helpers. All data here is synthetic (example.* domains only)."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mail_triage import config as configmod  # noqa: E402
from mail_triage.connectors.base import Mailbox, MessageInfo  # noqa: E402


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "demo"
NO_SIGNALS = dict(list_unsubscribe=False, bulk=False, esp=False, noreply=False, automated=False, is_reply=False,
                  auth_fail=False, transactional=False, notice=False, commercial_marker=False, account_sender=False)


def default_config(**overrides) -> configmod.Config:
    config = configmod.load(configmod.DEFAULT_FILE)
    return configmod.Config(**{**config.__dict__, **overrides})


def answers(kind: dict, marketing=0.1, spam=0.1, phishing=0.0) -> dict:
    """Model answer in system-one format over the default categories."""
    names = default_config().names()
    total = sum(kind.values())
    return {
        "kind": {"type": "choice", "probabilities": {n: kind.get(n, 0.0) / total for n in names}},
        "is_marketing": {"type": "noul", "noul": marketing},
        "is_spam": {"type": "noul", "noul": spam},
        "is_phishing": {"type": "noul", "noul": phishing},
    }


def eml(sender: str, subject: str, body: str = "Hello there, this is a test message body.", **headers) -> str:
    lines = [f"From: {sender}", "To: demo@example.org", f"Subject: {subject}",
             f"Message-ID: <{abs(hash((sender, subject, body)))}@test.example>"]
    lines += [f"{k.replace('_', '-')}: {v}" for k, v in headers.items()]
    return "\n".join(lines) + "\nContent-Type: text/plain; charset=utf-8\n\n" + body + "\n"


class TempHome:
    """Point MAIL_TRIAGE_HOME at a temporary folder for the duration of a test."""

    def __enter__(self) -> Path:
        self.dir = tempfile.TemporaryDirectory()
        self.old = os.environ.get("MAIL_TRIAGE_HOME")
        os.environ["MAIL_TRIAGE_HOME"] = self.dir.name
        return Path(self.dir.name)

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop("MAIL_TRIAGE_HOME", None)
        else:
            os.environ["MAIL_TRIAGE_HOME"] = self.old
        self.dir.cleanup()


class FakeConnector:
    """In-memory mail source; counts fetches so tests can prove shortcuts skip them."""
    name = "fake"

    def __init__(self, sources: list[str]):
        from mail_triage.mime import parse_message
        self.box = Mailbox("acct", "INBOX")
        self.sources = {str(i): s for i, s in enumerate(sources)}
        self.infos = []
        for native_id, source in self.sources.items():
            mail = parse_message(source)
            self.infos.append(MessageInfo(native_id, mail["message_id"], mail["from_address"], mail["subject"], ""))
        self.fetched = 0

    def mailboxes(self, accounts=(), mailbox=None):
        return [self.box]

    def list(self, mailbox):
        return list(self.infos)

    def fetch_sources(self, mailbox, native_ids):
        self.fetched += len(native_ids)
        return {i: self.sources[i] for i in native_ids if i in self.sources}


class FakeRouter:
    """Deterministic model: promotion when the subject says 'sale', else personal."""
    label = "fake"

    def __init__(self, config=None):
        self.calls = 0

    def classify(self, state):
        self.calls += 1
        if "sale" in state["subject"].lower():
            return answers({"promotion": 0.9, "personal": 0.1}, marketing=0.97, spam=0.6), True, self.label
        return answers({"personal": 0.9, "promotion": 0.1}, marketing=0.05, spam=0.02), True, self.label

    def close(self):
        pass
