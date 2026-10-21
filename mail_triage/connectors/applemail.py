"""Apple Mail (macOS) through JavaScript for Automation (osascript -l JavaScript).

Reads mail, and for `./triage apply` only: creates destination mailboxes and moves messages
(never deletes, flags or marks as read). The scripts below are constants; account names,
mailbox names and message ids are passed as arguments, never pasted into code, so no mail data
can inject script. Needs the Automation permission for the terminal (System Settings › Privacy
& Security › Automation › Mail).
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
# Creates each missing level of argv[1] ("Laya/Newsletters" → "Laya", then "Laya/Newsletters")
# inside the account, so it is created on the mail server, not "On My Mac". Mail creates it in
# the background (slowly while it is busy moving mail), so wait until it is visible.
_ENSURE = """
function run(argv) {
  const Mail = Application('Mail');
  const account = Mail.accounts.byName(argv[0]);
  account.name();
  function exists(path) {
    try { account.mailboxes.byName(path).name(); return true; } catch (e) { return false; }
  }
  const parts = argv[1].split('/');
  let created = false;
  for (let i = 1; i <= parts.length; i++) {
    const path = parts.slice(0, i).join('/');
    if (exists(path)) continue;
    account.mailboxes.push(Mail.Mailbox({name: path}));
    created = true;
    for (let wait = 0; wait < Number(argv[2]) && !exists(path); wait++) delay(0.5);
    if (!exists(path)) return JSON.stringify({ok: false, created: created});
  }
  return JSON.stringify({ok: true, created: created});
}"""
FOLDER_WAIT_SECONDS = 30
# argv: account, source mailbox, destination mailbox, then pairs of (message id, Message-ID).
# A message is moved only when its Message-ID still matches the one it was decided under.
_MOVE = """
function run(argv) {
  const Mail = Application('Mail');
  const account = Mail.accounts.byName(argv[0]);
  const source = account.mailboxes.byName(argv[1]);
  const target = account.mailboxes.byName(argv[2]);
  source.name();
  target.name();
  const moved = [];
  for (let i = 3; i + 1 < argv.length; i += 2) {
    try {
      const msg = source.messages.byId(Number(argv[i]));
      if (msg.messageId() !== argv[i + 1]) continue;
      Mail.move(msg, {to: target});
      moved.push(argv[i]);
    } catch (e) { }
  }
  return JSON.stringify(moved);
}"""
MOVE_BATCH = 25


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

    # -- write side (./triage apply only) -----------------------------------------------------

    def ensure_folder(self, account: str, folder: str) -> bool:
        result = _run(_ENSURE, account, folder, str(FOLDER_WAIT_SECONDS * 2), timeout=FOLDER_WAIT_SECONDS + 90)
        if not result["ok"]:
            raise ConnectorError(f"Mail did not show the new folder {folder} within {FOLDER_WAIT_SECONDS} s "
                                 "(it may still be busy moving mail, or the server refused the name)")
        return bool(result["created"])

    def move(self, mailbox: Mailbox, items: list[tuple[str, str]], folder: str) -> list[str]:
        moved = []
        for start in range(0, len(items), MOVE_BATCH):
            pairs = [value for pair in items[start:start + MOVE_BATCH] for value in pair]
            moved += _run(_MOVE, mailbox.account, mailbox.name, folder, *pairs)
        return moved
