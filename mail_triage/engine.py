"""The core pipeline. Knows nothing about Apple Mail, Outlook, MLX or llama.cpp: it talks to a
Connector (mail source) and a Router (model backends) and records everything in the Store.

For every message:
  1. skip it if it already has a decision (unless rescanning)
  2. teachings, then rules on sender/subject   → decided without fetching the message
  3. sender memory                             → decided without fetching the message
  4. otherwise fetch the source, apply header rules, ask the model, apply the policy
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Callable

from . import learning, rules
from .backends.base import BackendError
from .config import Config
from .connectors.base import Connector, Mailbox, MessageInfo
from .mime import model_state, parse_message
from .policy import Decision, decide, decide_category
from .privacy import printable, redact_sender, redact_subject
from .store import Store, message_key


FETCH_BATCH = 10


@dataclass
class ScanOptions:
    accounts: tuple[str, ...] = ()
    mailbox: str | None = None
    limit: int | None = None
    mode: str = "new"            # new | changed | all


class Engine:
    def __init__(self, config: Config, store: Store, connector: Connector,
                 router_factory: Callable, out: Callable[[str], None] = print):
        self.config = config
        self.store = store
        self.connector = connector
        self.router_factory = router_factory
        self.router = None
        self.out = out
        self.fingerprint = config.fingerprint()
        self.console = config.privacy["console"]
        self.counts = Counter()

    # -- output ------------------------------------------------------------------------------

    def _show(self, info: MessageInfo, decision: Decision, source: str) -> None:
        self.counts[decision.verdict] += 1
        total = self.counts["keep"] + self.counts["review"] + self.counts["junk"]
        if self.console == "counts":
            if total % 50 == 0:
                self.out(f"  … {total} decided")
            return
        mark = {"junk": "✗ junk  ", "review": "? review", "keep": "✓ keep  "}[decision.verdict]
        if self.console == "full":
            who, what = printable(info.sender), printable(info.subject)
        else:
            who, what = redact_sender(printable(info.sender)), redact_subject(printable(info.subject))
        self.out(f"  {mark} {decision.category[:13]:13} {source:6} {who[:32]:32} {what[:60]}")

    # -- pipeline ----------------------------------------------------------------------------

    def _record(self, mailbox: Mailbox, info: MessageInfo, key: str, decision: Decision,
                source: str, model: str = "") -> None:
        self.store.record(key=key, connector=self.connector.name, account=mailbox.account, mailbox=mailbox.name,
                          native_id=info.native_id, sender=info.sender, subject=info.subject,
                          received=info.received, decision=decision, source=source, model=model,
                          fingerprint=self.fingerprint, store_subject=self.config.privacy["store_subjects"])
        self.counts[f"by_{source}"] += 1
        self._show(info, decision, source)

    def _shortcut(self, info: MessageInfo) -> tuple[Decision, str] | None:
        taught = self.store.taught(info.sender) if info.sender else None
        if taught is not None and (taught["category"] in self.config.names() or taught["category"] == "review"):
            return decide_category(self.config, taught["category"], f"taught: {taught['kind']} {taught['value']}"), "teach"
        rule = rules.first_match(self.config, info.sender, info.subject)
        if rule is not None:
            return decide_category(self.config, rule.category, f"rule: {rule.name}"), "rule"
        recalled = learning.recall(self.config, self.store, info.sender, info.subject)
        if recalled is not None:
            category, reason = recalled
            return decide_category(self.config, category, reason, "learned"), "memory"
        return None

    def _classify(self, source: str) -> tuple[Decision, str, str]:
        mail = parse_message(source)
        rule = rules.first_match(self.config, mail["from_address"], mail["subject"], mail["headers"])
        if rule is not None:
            return decide_category(self.config, rule.category, f"rule: {rule.name}"), "rule", ""
        if self.router is None:
            self.router = self.router_factory(self.config)
        answers, fluent, label = self.router.classify(model_state(mail, int(self.config.model["body_chars"])))
        return decide(self.config, answers, mail["signals"], fluent=fluent), "model", label

    def scan(self, options: ScanOptions) -> Counter:
        self.counts = Counter()
        purged = self.store.purge_older(int(self.config.privacy["retention_days"]))
        if purged:
            self.out(f"Removed {purged} entries older than {self.config.privacy['retention_days']} days from history.")
        run = self.store.start_run(self.connector.name)
        remaining = options.limit
        try:
            for number, mailbox in enumerate(self.connector.mailboxes(options.accounts, options.mailbox), 1):
                if remaining is not None and remaining <= 0:
                    break
                infos = self.connector.list(mailbox)
                todo = []
                for info in infos:
                    key = message_key(mailbox.account, info.message_id, info.native_id)
                    if self.store.needs_decision(key, options.mode, self.fingerprint):
                        todo.append((info, key))
                self.counts["seen"] += len(infos)
                self.counts["skipped"] += len(infos) - len(todo)
                if remaining is not None:
                    todo, remaining = todo[:remaining], remaining - min(remaining, len(todo))
                # Account names can be email addresses: hidden unless console = full.
                label = mailbox.label if self.console == "full" else f"mailbox {number}"
                self.out(f"{label}: {len(infos)} messages, {len(todo)} to decide")

                full = []
                for info, key in todo:
                    shortcut = self._shortcut(info)
                    if shortcut is None:
                        full.append((info, key))
                    else:
                        self._record(mailbox, info, key, *shortcut)
                self.store.commit()

                for start in range(0, len(full), FETCH_BATCH):
                    batch = full[start:start + FETCH_BATCH]
                    sources = self.connector.fetch_sources(mailbox, [info.native_id for info, _ in batch])
                    for info, key in batch:
                        if info.native_id not in sources:
                            self.counts["gone"] += 1
                            continue
                        try:
                            decision, source, label = self._classify(sources[info.native_id])
                        except BackendError:
                            raise              # the model is gone: stop, keep what is decided
                        except Exception as exc:   # one malformed message must not stop the run
                            self.counts["errors"] += 1
                            # Only the error type: messages can contain mail text.
                            self.out(f"  ! could not classify one message ({type(exc).__name__})")
                            continue
                        self._record(mailbox, info, key, decision, source, label)
                    self.store.commit()
        finally:
            self.store.commit()
            self.store.finish_run(run, dict(self.counts))
            if self.router is not None:
                self.router.close()
                self.router = None
        return self.counts
