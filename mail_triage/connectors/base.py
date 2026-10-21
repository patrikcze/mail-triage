"""What a mail source must provide. Add Outlook (COM), Microsoft Graph, IMAP … by implementing this.

Read side (all connectors):
    mailboxes()                 the mailboxes to triage, normally each account's inbox
    list(mailbox)               cheap metadata of every message (no body)
    fetch_sources(mailbox, ids) raw RFC 822 source of a few messages

Write side (optional): a connector may also implement Mover: ensure_folder() creates a missing
destination folder, move() moves messages. There is deliberately no delete, trash, expunge or
flag method anywhere, and validate_destination() refuses trash/junk folders and anything
outside the configured root. actions.py is the only caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol, runtime_checkable


@dataclass(frozen=True)
class Mailbox:
    account: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.account} / {self.name}"


@dataclass(frozen=True)
class MessageInfo:
    """Light metadata, fetched for every message on every scan; keep it cheap."""
    native_id: str        # the mail program's own id (used to fetch/move)
    message_id: str       # RFC 822 Message-ID header, "" if unknown
    sender: str           # address only, e.g. "news@shop.example.cz"
    subject: str
    received: str         # ISO date or ""
    identity_scope: str = ""  # opt-in native identity namespace; legacy sources leave empty
    headers_available: bool | None = True  # None: fetch before junk memory; default preserves legacy shortcuts


class ConnectorError(Exception):
    pass


@runtime_checkable
class Connector(Protocol):
    name: str

    def mailboxes(self, accounts: Iterable[str] = (), mailbox: str | None = None) -> list[Mailbox]: ...

    def list(self, mailbox: Mailbox) -> list[MessageInfo]: ...

    def fetch_sources(self, mailbox: Mailbox, native_ids: list[str]) -> dict[str, str]:
        """native id → raw source. Missing ids (deleted meanwhile) are simply absent."""
        ...


@runtime_checkable
class Mover(Protocol):
    """Write side. Moving (and creating its destination folder) is the only mutation mail-triage performs."""

    def ensure_folder(self, account: str, folder: str, flat_separator: str = "") -> tuple[bool, str]:
        """Make sure `folder` exists in `account`, creating it (and its parents) if missing.

        Returns (created, folder actually used). Where the account cannot have nested folders
        created by script and `flat_separator` is set, the folder used may be the flattened
        top-level name ("Laya-Newsletters"). Never renames or deletes folders."""
        ...

    def move(self, mailbox: Mailbox, items: list[tuple[str, str]], folder: str) -> list[str]:
        """Move (native id, Message-ID) pairs from `mailbox` to `folder` in the same account.

        A message is moved only if its Message-ID still matches, so a reused or stale native id
        can never move a different message. Returns the native ids that were moved."""
        ...


def validate_destination(folder: str, actions: dict) -> str:
    """Return the cleaned destination or raise: only under allowed_root (or listed), never trash/junk."""
    folder = folder.strip().strip("/")
    if not folder:
        raise ValueError("empty destination")
    if any(part in ("", ".", "..") for part in folder.split("/")):
        raise ValueError(f"invalid folder path {folder!r}")
    top = folder.split("/")[0].lower()
    if top in {name.lower() for name in actions.get("forbidden", [])}:
        raise ValueError(f"moving to {folder!r} is not allowed")
    root = str(actions.get("allowed_root", "")).strip("/")
    allowed = [f.strip("/") for f in actions.get("also_allowed", [])]
    if not (root and (folder == root or folder.startswith(root + "/")) or folder in allowed):
        raise ValueError(f"{folder!r} is outside {root!r}/ and not listed in actions.also_allowed")
    return folder
