"""Apple Mail (macOS) through JavaScript for Automation (osascript -l JavaScript). Read-only.

The scripts below are constants; account names, mailbox names and message ids are passed as
arguments, never pasted into code, so no mail data can inject script. Needs the Automation
permission for the terminal (System Settings › Privacy & Security › Automation › Mail).
"""

from __future__ import annotations

from email.utils import parseaddr
import json
import subprocess
import sys
from typing import Iterable

from .base import ConnectorError, Mailbox, MessageInfo


FETCH_BATCH = 10
_INBOXES = """
function run(argv) {
  const Mail = Application('Mail');
  return JSON.stringify(Mail.inbox.mailboxes().map(mb => [mb.account().name(), mb.name()]));
}"""
_EXISTS = """
function run(argv) {
  const mb = Application('Mail').accounts.byName(argv[0]).mailboxes.byName(argv[1]);
  mb.name();
  return JSON.stringify([argv[0], argv[1]]);
}"""
_LIST = """
function run(argv) {
  const m = Application('Mail').accounts.byName(argv[0]).mailboxes.byName(argv[1]).messages;
  return JSON.stringify({id: m.id(), mid: m.messageId(), sender: m.sender(), subject: m.subject(),
                         date: m.dateReceived().map(d => d ? d.toISOString() : '')});
}"""
_FETCH = """
function run(argv) {
  const mb = Application('Mail').accounts.byName(argv[0]).mailboxes.byName(argv[1]);
  const out = {};
  for (const id of argv.slice(2)) {
    try { out[id] = mb.messages.byId(Number(id)).source(); } catch (e) { }
  }
  return JSON.stringify(out);
}"""


def _run(script: str, *args: str, timeout: float = 600):
    try:
        result = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", script, *args],
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ConnectorError("Mail did not answer in time; is it busy downloading?") from exc
    if result.returncode != 0:
        if "-1743" in result.stderr or "not allowed" in result.stderr.lower():
            raise ConnectorError("Mail access not allowed. Enable your terminal under System Settings › "
                                 "Privacy & Security › Automation › Mail, then run again.")
        # stderr can quote mail data; report only the error number.
        code = next((w.strip("().") for w in result.stderr.split() if w.strip("().").lstrip("-").isdigit()), "?")
        raise ConnectorError(f"Mail returned error {code}")
    return json.loads(result.stdout or "null")


class AppleMailConnector:
    name = "applemail"

    def __init__(self):
        if sys.platform != "darwin":
            raise ConnectorError("Apple Mail is only available on macOS")

    def mailboxes(self, accounts: Iterable[str] = (), mailbox: str | None = None) -> list[Mailbox]:
        accounts = list(accounts)
        if mailbox is not None:
            if len(accounts) != 1:
                raise ConnectorError("--mailbox needs exactly one --account")
            return [Mailbox(*_run(_EXISTS, accounts[0], mailbox))]
        boxes = [Mailbox(account, name) for account, name in _run(_INBOXES)]
        chosen = [b for b in boxes if not accounts or b.account in accounts]
        if accounts and not chosen:
            raise ConnectorError(f"no inbox for {', '.join(accounts)}; accounts: {', '.join(sorted({b.account for b in boxes}))}")
        return chosen

    def list(self, mailbox: Mailbox) -> list[MessageInfo]:
        data = _run(_LIST, mailbox.account, mailbox.name)
        return [MessageInfo(str(i), mid or "", parseaddr(sender or "")[1].lower(), subject or "", date or "")
                for i, mid, sender, subject, date in zip(data["id"], data["mid"], data["sender"],
                                                         data["subject"], data["date"])]

    def fetch_sources(self, mailbox: Mailbox, native_ids: list[str]) -> dict[str, str]:
        sources = {}
        for start in range(0, len(native_ids), FETCH_BATCH):
            sources.update(_run(_FETCH, mailbox.account, mailbox.name, *native_ids[start:start + FETCH_BATCH]))
        return sources
