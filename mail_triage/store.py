"""Local history in SQLite: which emails were seen, every decision, sender memory, teachings.

Never stores email bodies. Subjects only when privacy.store_subjects is true. The file is
readable by its owner only. All SQL is parameterised.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .privacy import private_dir, private_file


SCHEMA_VERSION = 1
# Decisions that count as evidence about a sender. Memory reuse is excluded so it cannot
# reinforce itself.
AUTHORITATIVE = ("model", "rule", "teach")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(
    key TEXT PRIMARY KEY,
    connector TEXT NOT NULL, account TEXT NOT NULL, mailbox TEXT NOT NULL, native_id TEXT NOT NULL,
    sender TEXT NOT NULL, domain TEXT NOT NULL, subject TEXT NOT NULL, received TEXT NOT NULL,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions(
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL REFERENCES messages(key) ON DELETE CASCADE,
    decided_at TEXT NOT NULL, verdict TEXT NOT NULL, category TEXT NOT NULL, folder TEXT NOT NULL,
    junk_probability REAL NOT NULL, likelihood TEXT NOT NULL, source TEXT NOT NULL,
    reasons TEXT NOT NULL, model TEXT NOT NULL, config_fp TEXT NOT NULL, current INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS decisions_current ON decisions(key, current);
CREATE TABLE IF NOT EXISTS sender_stats(
    address TEXT NOT NULL, category TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY(address, category));
CREATE TABLE IF NOT EXISTS sender_memory(address TEXT PRIMARY KEY, uses INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS teachings(
    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('sender', 'domain')), value TEXT NOT NULL, category TEXT NOT NULL,
    UNIQUE(kind, value));
-- Journal for the future move feature: every planned/applied move, so it can be undone.
CREATE TABLE IF NOT EXISTS actions(
    id INTEGER PRIMARY KEY, key TEXT NOT NULL, created_at TEXT NOT NULL,
    from_mailbox TEXT NOT NULL, to_folder TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned', 'applied', 'undone', 'failed')), detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
    id INTEGER PRIMARY KEY, started TEXT NOT NULL, finished TEXT, connector TEXT NOT NULL, counts TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def message_key(account: str, message_id: str, native_id: str) -> str:
    """Stable identity: account + Message-ID header (or the mail program's own id)."""
    ident = message_id.strip().strip("<>").strip() or f"native:{native_id}"
    return hashlib.sha256(f"{account}\0{ident}".encode()).hexdigest()


@dataclass(frozen=True)
class Profile:
    total: int
    category: str
    share: float


class Store:
    def __init__(self, path: Path):
        private_dir(path.parent)
        private_file(path)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA secure_delete = ON")    # erased rows are overwritten, not just unlinked
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(_SCHEMA)
        version = self.db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        if version is None:
            self.db.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        elif int(version[0]) > SCHEMA_VERSION:
            raise RuntimeError(f"{path} was written by a newer mail-triage (schema {version[0]})")
        self.db.commit()
        for suffix in ("-wal", "-shm"):
            side = path.with_name(path.name + suffix)
            if side.exists():
                private_file(side)

    def close(self) -> None:
        self.db.close()

    def commit(self) -> None:
        self.db.commit()

    # -- decisions ---------------------------------------------------------------------------

    def current(self, key: str):
        return self.db.execute(
            "SELECT * FROM decisions WHERE key = ? AND current = 1", (key,)).fetchone()

    def needs_decision(self, key: str, mode: str, fingerprint: str) -> bool:
        """mode: new = only never-seen mail; changed = also mail decided under older settings; all."""
        if mode == "all":
            return True
        row = self.current(key)
        if row is None or row["config_fp"] == "":     # never seen, or invalidated by a teaching
            return True
        return mode == "changed" and row["config_fp"] != fingerprint

    def record(self, *, key: str, connector: str, account: str, mailbox: str, native_id: str,
               sender: str, subject: str, received: str, decision, source: str, model: str,
               fingerprint: str, store_subject: bool) -> None:
        sender = sender.lower()
        stamp = now()
        self.db.execute(
            """INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET mailbox = excluded.mailbox, native_id = excluded.native_id,
               sender = excluded.sender, domain = excluded.domain, subject = excluded.subject,
               received = excluded.received, last_seen = excluded.last_seen""",
            (key, connector, account, mailbox, str(native_id), sender, sender.rpartition("@")[2],
             subject if store_subject else "", received, stamp, stamp))
        old = self.current(key)
        if old is not None:
            self.db.execute("UPDATE decisions SET current = 0 WHERE key = ? AND current = 1", (key,))
            if old["source"] in AUTHORITATIVE:
                self.db.execute("UPDATE sender_stats SET n = n - 1 WHERE address = ? AND category = ?",
                                (sender, old["category"]))
        self.db.execute(
            "INSERT INTO decisions(key, decided_at, verdict, category, folder, junk_probability, likelihood,"
            " source, reasons, model, config_fp, current) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (key, stamp, decision.verdict, decision.category, decision.folder, decision.junk_probability,
             decision.likelihood, source, json.dumps(list(decision.reasons), ensure_ascii=False), model,
             fingerprint))
        if source in AUTHORITATIVE and sender:
            self.db.execute(
                "INSERT INTO sender_stats VALUES (?, ?, 1) ON CONFLICT(address, category) DO UPDATE SET n = n + 1",
                (sender, decision.category))

    # -- learning ----------------------------------------------------------------------------

    def profile(self, address: str) -> Profile | None:
        rows = self.db.execute("SELECT category, n FROM sender_stats WHERE address = ? AND n > 0",
                               (address.lower(),)).fetchall()
        total = sum(row["n"] for row in rows)
        if not total:
            return None
        best = max(rows, key=lambda row: row["n"])
        return Profile(total, best["category"], best["n"] / total)

    def use_memory(self, address: str) -> int:
        """Count reuses of a sender's memory; returns the new count."""
        self.db.execute("INSERT INTO sender_memory VALUES (?, 1) ON CONFLICT(address) DO UPDATE SET uses = uses + 1",
                        (address.lower(),))
        return self.db.execute("SELECT uses FROM sender_memory WHERE address = ?", (address.lower(),)).fetchone()[0]

    # -- teachings (user corrections) --------------------------------------------------------

    def teach(self, kind: str, value: str, category: str) -> int:
        """Store a correction and mark that sender's/domain's decisions for re-deciding. Returns how many."""
        value = value.lower().lstrip("@")
        self.db.execute(
            "INSERT INTO teachings(created_at, kind, value, category) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(kind, value) DO UPDATE SET category = excluded.category, created_at = excluded.created_at",
            (now(), kind, value, category))
        if kind == "sender":
            match, args = "m.sender = ?", (value,)
        else:
            match, args = "(m.domain = ? OR m.domain LIKE ?)", (value, "%." + value)
        redo = self.db.execute(
            f"UPDATE decisions SET config_fp = '' WHERE current = 1 AND key IN"
            f" (SELECT m.key FROM messages m WHERE {match})", args).rowcount
        self.db.commit()
        return redo

    def teachings(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM teachings ORDER BY kind, value").fetchall()

    def unteach(self, teaching_id: int) -> bool:
        removed = self.db.execute("DELETE FROM teachings WHERE id = ?", (teaching_id,)).rowcount
        self.db.commit()
        return bool(removed)

    def taught(self, address: str):
        """Teaching for an address: exact sender first, then its domain and parent domains."""
        address = address.lower()
        row = self.db.execute("SELECT * FROM teachings WHERE kind = 'sender' AND value = ?", (address,)).fetchone()
        if row:
            return row
        parts = address.rpartition("@")[2].split(".")
        for i in range(len(parts) - 1):
            row = self.db.execute("SELECT * FROM teachings WHERE kind = 'domain' AND value = ?",
                                  (".".join(parts[i:]),)).fetchone()
            if row:
                return row
        return None

    # -- reporting ---------------------------------------------------------------------------

    def rows(self, accounts: list[str] | None = None, verdict: str | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT m.*, d.verdict, d.category, d.folder, d.junk_probability, d.likelihood, d.source,"
               " d.reasons, d.model, d.decided_at FROM messages m JOIN decisions d ON d.key = m.key AND d.current = 1")
        where, args = [], []
        if accounts:
            where.append(f"m.account IN ({','.join('?' * len(accounts))})")
            args += accounts
        if verdict:
            where.append("d.verdict = ?")
            args.append(verdict)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self.db.execute(sql + " ORDER BY m.received DESC", args).fetchall()

    def counts(self) -> dict:
        rows = self.db.execute("SELECT verdict, COUNT(*) n FROM decisions WHERE current = 1 GROUP BY verdict")
        result = {row["verdict"]: row["n"] for row in rows}
        result["senders"] = self.db.execute("SELECT COUNT(DISTINCT address) FROM sender_stats").fetchone()[0]
        result["teachings"] = self.db.execute("SELECT COUNT(*) FROM teachings").fetchone()[0]
        return result

    def start_run(self, connector: str) -> int:
        cursor = self.db.execute("INSERT INTO runs(started, connector) VALUES (?, ?)", (now(), connector))
        self.db.commit()
        return cursor.lastrowid

    def finish_run(self, run_id: int, counts: dict) -> None:
        self.db.execute("UPDATE runs SET finished = ?, counts = ? WHERE id = ?", (now(), json.dumps(counts), run_id))
        self.db.commit()

    # -- erasure (GDPR) ----------------------------------------------------------------------

    def forget_sender(self, address: str) -> int:
        address = address.lower()
        keys = [row[0] for row in self.db.execute("SELECT key FROM messages WHERE sender = ?", (address,))]
        self.db.executemany("DELETE FROM actions WHERE key = ?", [(k,) for k in keys])
        self.db.execute("DELETE FROM messages WHERE sender = ?", (address,))
        self.db.execute("DELETE FROM sender_stats WHERE address = ?", (address,))
        self.db.execute("DELETE FROM sender_memory WHERE address = ?", (address,))
        self.db.execute("DELETE FROM teachings WHERE kind = 'sender' AND value = ?", (address,))
        self.db.commit()
        self.vacuum()
        return len(keys)

    def purge_older(self, days: int) -> int:
        """Delete history of messages not seen for `days` days (0 = keep forever)."""
        if days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        keys = [row[0] for row in self.db.execute("SELECT key FROM messages WHERE last_seen < ?", (cutoff,))]
        self.db.executemany("DELETE FROM actions WHERE key = ?", [(k,) for k in keys])
        self.db.execute("DELETE FROM messages WHERE last_seen < ?", (cutoff,))
        self.db.commit()
        return len(keys)

    def vacuum(self) -> None:
        """Rewrite the file so deleted rows do not linger on disk."""
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.execute("VACUUM")
