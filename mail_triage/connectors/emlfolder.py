"""Read .eml files and .mbox archives from a folder (read-only).

Useful for trying mail-triage safely on exported mail, for Outlook/Thunderbird exports, and
for tests. Each sub-folder is a mailbox; the account is the folder's name.
"""

from __future__ import annotations

import email
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
import mailbox as mboxlib
from pathlib import Path
from typing import Iterable

from .base import ConnectorError, Mailbox, MessageInfo


def _info(native_id: str, raw: bytes) -> MessageInfo:
    msg = email.message_from_bytes(raw, policy=policy.default)
    try:
        received = parsedate_to_datetime(str(msg.get("Date", ""))).isoformat()
    except (TypeError, ValueError, IndexError):
        received = ""
    return MessageInfo(native_id, str(msg.get("Message-ID", "") or "").strip(),
                       parseaddr(str(msg.get("From", "")))[1].lower(), str(msg.get("Subject", "") or ""), received)


class EmlFolderConnector:
    name = "emlfolder"

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ConnectorError(f"{self.root} is not a folder")

    def _files(self, mailbox: Mailbox) -> list[Path]:
        folder = self.root if mailbox.name == "." else self.root / mailbox.name
        return sorted(p for p in folder.iterdir() if p.suffix.lower() in (".eml", ".mbox") and p.is_file())

    def mailboxes(self, accounts: Iterable[str] = (), mailbox: str | None = None) -> list[Mailbox]:
        accounts = list(accounts)
        if accounts and self.root.name not in accounts:
            return []
        names = sorted({str(p.parent.relative_to(self.root)) for p in self.root.rglob("*")
                        if p.suffix.lower() in (".eml", ".mbox") and p.is_file()})
        if mailbox is not None:
            names = [n for n in names if n == mailbox]
        return [Mailbox(self.root.name, n) for n in names]

    def _raw(self, mailbox: Mailbox):
        for path in self._files(mailbox):
            if path.suffix.lower() == ".eml":
                yield path.name, path.read_bytes()
            else:
                for index, message in enumerate(mboxlib.mbox(path, create=False)):
                    yield f"{path.name}#{index}", message.as_bytes()

    def list(self, mailbox: Mailbox) -> list[MessageInfo]:
        return [_info(native_id, raw) for native_id, raw in self._raw(mailbox)]

    def fetch_sources(self, mailbox: Mailbox, native_ids: list[str]) -> dict[str, str]:
        wanted = set(native_ids)
        return {native_id: raw.decode("utf-8", errors="surrogateescape")
                for native_id, raw in self._raw(mailbox) if native_id in wanted}
