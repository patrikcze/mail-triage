"""Sender memory: reuse a sender's consistent past decisions instead of fetching and asking again.

A sender qualifies after `min_samples` authoritative decisions (model, rule or teach — never
earlier reuses) of which at least `agreement` share one category. Every `recheck_every`-th
reuse still goes through the model, so a sender that changes behaviour is noticed. Subjects
that look like receipts, orders, contracts or account notices always get the full check.
"""

from __future__ import annotations

from .config import Config
from .mime import subject_signals
from .store import Store


def recall(config: Config, store: Store, address: str, subject: str) -> tuple[str, str] | None:
    """(category, reason) when the sender's history can decide this email, else None."""
    learning = config.learning
    if not learning["enabled"] or not address:
        return None
    signals = subject_signals(address, subject)
    if signals["transactional"] or signals["notice"] or signals["account_sender"]:
        return None
    profile = store.profile(address)
    if profile is None or profile.total < learning["min_samples"] or profile.share < learning["agreement"]:
        return None
    if profile.category not in config.names() and profile.category != "review":
        return None   # the category was removed from the config since
    uses = store.use_memory(address)
    if uses % int(learning["recheck_every"]) == 0:
        return None   # periodic re-check by the model
    return profile.category, f"learned: {profile.total} earlier emails from this sender, {profile.share:.0%} {profile.category}"
