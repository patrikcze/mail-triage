"""`./triage apply` and `./triage undo`: move decided mail into its category folder. Never deletes.

What may move, and where, is decided here; connectors only carry it out (connectors.base.Mover).
A message is moved only when all of this holds:
  - it is in the mailbox right now, in the mailbox it was decided in (a message you moved
    yourself is left alone), and it has a Message-ID, which the mail program re-checks just
    before moving
  - its current decision was made with the current settings (otherwise rescan first)
  - its category has a folder and is not listed in actions.keep_in_place (personal and work mail
    stay where they are by default)
  - the folder passes validate_destination(): under actions.allowed_root, never Trash/Junk
  - it was not moved before, unless it has been re-decided since (an undone move is respected)
Every move is journaled before it happens, so undo() can move it back.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable

from .config import Config
from .connectors.base import ConnectorError, Mailbox, MessageInfo, Mover, validate_destination
from .privacy import printable, redact_sender, redact_subject
from .store import Store, message_key


NOT_DECIDED = "not decided yet (run ./triage scan first)"
STALE = "decided with older settings or re-taught (run ./triage scan --rescan-changed)"
ELSEWHERE = "decided in another mailbox (you moved it; left alone)"
NO_ID = "no Message-ID, so it cannot be verified before moving"
MOVED_BEFORE = "moved before and not re-decided since (left where it is)"
NOT_ALLOWED = "folder not allowed by [actions]"


@dataclass(frozen=True)
class PlannedMove:
    key: str
    info: MessageInfo
    category: str
    folder: str


@dataclass
class Plan:
    mailbox: Mailbox
    seen: int = 0
    moves: list[PlannedMove] = field(default_factory=list)
    stay: Counter = field(default_factory=Counter)       # category → messages that stay
    skipped: Counter = field(default_factory=Counter)    # reason → messages not considered
    held: int = 0                                         # ready to move, left for a later apply by --limit

    def by_folder(self) -> dict[str, list[PlannedMove]]:
        groups: dict[str, list[PlannedMove]] = defaultdict(list)
        for move in self.moves:
            groups[move.folder].append(move)
        return dict(sorted(groups.items()))


def destinations(config: Config) -> dict[str, str]:
    """Category (and "review") → folder it moves to; "" = stays where it is."""
    keep = set(config.actions.get("keep_in_place", []))
    result = {c.name: "" if c.name in keep else c.folder for c in config.categories}
    result["review"] = "" if "review" in keep else config.policy["review_folder"]
    return result


def plan(config: Config, store: Store, connector, mailbox: Mailbox) -> Plan:
    result = Plan(mailbox)
    fingerprint = config.fingerprint()
    keep = set(config.actions.get("keep_in_place", []))
    infos = connector.list(mailbox)
    result.seen = len(infos)
    for info in infos:
        key = message_key(mailbox.account, info.message_id, info.native_id,
                          scope=info.identity_scope, mailbox=mailbox.name)
        row = store.current(key)
        if row is None:
            result.skipped[NOT_DECIDED] += 1
            continue
        if row["config_fp"] != fingerprint:
            result.skipped[STALE] += 1
            continue
        if row["category"] in keep or not row["folder"]:
            result.stay[row["category"]] += 1
            continue
        try:
            folder = validate_destination(row["folder"], config.actions)
        except ValueError:
            result.skipped[NOT_ALLOWED] += 1
            continue
        if folder == mailbox.name:
            result.stay[row["category"]] += 1
            continue
        placed = store.message(key)
        if placed is None or placed["mailbox"] != mailbox.name:
            result.skipped[ELSEWHERE] += 1
            continue
        if not info.message_id:
            result.skipped[NO_ID] += 1
            continue
        last = store.last_move(key)
        if last is not None and last["created_at"] >= row["decided_at"]:
            result.skipped[MOVED_BEFORE] += 1
            continue
        result.moves.append(PlannedMove(key, info, row["category"], folder))
    return result


def limit(plans: list[Plan], n: int) -> None:
    """Keep only the first n moves across all plans (the rest stay for a later apply)."""
    for item in plans:
        keep = max(n, 0)
        item.held = max(len(item.moves) - keep, 0)
        item.moves, n = item.moves[:keep], max(n - len(item.moves), 0)


def show(plans: list[Plan], console: str, out: Callable[[str], None] = print) -> None:
    for number, item in enumerate(plans, 1):
        label = item.mailbox.label if console == "full" else f"mailbox {number}"
        held = f" ({item.held} more ready, left for later by --limit)" if item.held else ""
        out(f"{label}: {item.seen} messages, {len(item.moves)} to move{held}")
        for folder, moves in item.by_folder().items():
            out(f"  → {folder:28} {len(moves):5}")
            if console == "counts":
                continue
            for move in moves:
                who, what = printable(move.info.sender), printable(move.info.subject)
                if console != "full":
                    who, what = redact_sender(who), redact_subject(what)
                out(f"      {move.category[:13]:13} {who[:32]:32} {what[:50]}")
        if item.stay:
            out("  stays: " + ", ".join(f"{name} {n}" for name, n in sorted(item.stay.items())))
        for reason, n in sorted(item.skipped.items()):
            out(f"  not moved, {reason}: {n}")


def apply(store: Store, mover: Mover, plans: list[Plan], run_id: int,
          out: Callable[[str], None] = print, counts: Counter | None = None) -> Counter:
    """Carry out the plans, counting into `counts` (so a caller keeps the tally if this raises).

    A folder that cannot be created is skipped (nothing is journaled or moved for it) and the
    rest continues. A failed move stops the run; what was done stays journaled."""
    counts = Counter() if counts is None else counts
    for number, item in enumerate(plans, 1):
        for folder, moves in item.by_folder().items():
            try:
                created = mover.ensure_folder(item.mailbox.account, folder)
            except ConnectorError as exc:
                counts["folder_failed"] += len(moves)
                out(f"  ! mailbox {number}: {exc}; its {len(moves)} emails for {folder} were not moved")
                continue
            if created:
                counts["folders_created"] += 1
                out(f"  created folder {folder} (mailbox {number})")
            journal = {move.info.native_id: store.journal(move.key, item.mailbox.name, folder, run_id)
                       for move in moves}
            store.commit()
            try:
                moved = set(mover.move(item.mailbox, [(m.info.native_id, m.info.message_id) for m in moves], folder))
            except ConnectorError as exc:
                for action_id in journal.values():
                    store.finish_action(action_id, "failed", type(exc).__name__)
                store.commit()
                raise
            for move in moves:
                if move.info.native_id in moved:
                    store.finish_action(journal[move.info.native_id], "applied")
                    store.set_location(move.key, folder)
                    counts["moved"] += 1
                else:
                    # Gone or changed since listing (the Message-ID no longer matched): not moved.
                    store.finish_action(journal[move.info.native_id], "failed", "changed or gone")
                    counts["not_moved"] += 1
            store.commit()
    return counts


def undo(store: Store, connector, accounts: list[str] | None = None, all_runs: bool = False,
         folder: str | None = None) -> Counter:
    """Move messages journaled by apply back to the mailbox they came from (newest apply run by
    default; only those moved into `folder` when given)."""
    counts = Counter()
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for row in store.moves_to_undo(connector.name, accounts, all_runs, folder):
        groups[(row["account"], row["to_folder"])].append(row)
    for (account, folder), rows in groups.items():
        here = Mailbox(account, folder)
        try:
            infos = connector.list(here)
        except ConnectorError:
            counts["not_found"] += len(rows)
            continue
        found = {message_key(account, info.message_id, info.native_id,
                             scope=info.identity_scope, mailbox=folder): info for info in infos}
        back: dict[str, list] = defaultdict(list)
        for row in rows:
            info = found.get(row["key"])
            if info is None or not info.message_id:
                if row["status"] == "planned":       # interrupted before the move happened
                    store.finish_action(row["id"], "failed", "interrupted")
                else:
                    counts["not_found"] += 1           # moved on since; left where it is
                continue
            back[row["from_mailbox"]].append((row, info))
        for origin, pairs in back.items():
            moved = set(connector.move(here, [(info.native_id, info.message_id) for _, info in pairs], origin))
            for row, info in pairs:
                if info.native_id in moved:
                    store.finish_action(row["id"], "undone")
                    store.set_location(row["key"], origin)
                    counts["moved_back"] += 1
                else:
                    counts["not_found"] += 1
        store.commit()
    return counts
