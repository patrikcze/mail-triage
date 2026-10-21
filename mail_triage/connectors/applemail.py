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
# Finds a mailbox by path inside an account. The full path works for IMAP and iCloud
# ("Laya/Newsletters"); Exchange accounts only resolve it one level at a time.
_RESOLVE = """
function resolve(account, path) {
  try { const mb = account.mailboxes.byName(path); mb.name(); return mb; } catch (e) { }
  const parts = path.split('/');
  let mb = account.mailboxes.byName(parts[0]);
  mb.name();
  for (const part of parts.slice(1)) {
    const found = mb.mailboxes.whose({name: part})();
    if (!found.length) throw new Error('no such mailbox');
    mb = found[0];
  }
  return mb;
}"""
_EXISTS = _RESOLVE + """
function run(argv) {
  resolve(Application('Mail').accounts.byName(argv[0]), argv[1]).name();
  return JSON.stringify([argv[0], argv[1]]);
}"""
_LIST = _RESOLVE + """
function run(argv) {
  const m = resolve(Application('Mail').accounts.byName(argv[0]), argv[1]).messages;
  return JSON.stringify({id: m.id(), mid: m.messageId(), sender: m.sender(), subject: m.subject(),
                         date: m.dateReceived().map(d => d ? d.toISOString() : '')});
}"""
_FETCH = _RESOLVE + """
function run(argv) {
  const mb = resolve(Application('Mail').accounts.byName(argv[0]), argv[1]);
  const out = {};
  for (const id of argv.slice(2)) {
    try { out[id] = mb.messages.byId(Number(id)).source(); } catch (e) { }
  }
  return JSON.stringify(out);
}"""
# Creates each missing level of argv[1] ("Laya/Newsletters" → "Laya", then "Newsletters" in it)
# inside the account, so it is created on the mail server, not "On My Mac". An existing folder is
# always reused, so running this again creates nothing. Mail creates folders in the background
# (slowly while it is busy moving mail), so wait until the new one is visible.
# Exchange accounts (type "unknown" in Mail) cannot get nested folders by script: a full path
# becomes one folder literally named "Laya/Newsletters", and creating inside a parent fails
# (-10000). There a nested folder is only used if it exists; otherwise, with a separator in
# argv[3], a top-level folder "Laya-Newsletters" is used instead.
_ENSURE = _RESOLVE + """
function run(argv) {
  const Mail = Application('Mail');
  const account = Mail.accounts.byName(argv[0]);
  account.name();
  const exchange = String(account.accountType()) === 'unknown';
  function exists(path) {
    try { resolve(account, path).name(); return true; } catch (e) { return false; }
  }
  function create(path) {
    try { account.mailboxes.push(Mail.Mailbox({name: path})); } catch (e) { return Number(e.errorNumber) || -1; }
    for (let wait = 0; wait < Number(argv[2]) && !exists(path); wait++) delay(0.5);
    return exists(path) ? 0 : -2;
  }
  const parts = argv[1].split('/');
  if (exists(argv[1])) return JSON.stringify({ok: true, created: false, folder: argv[1]});
  if (exchange && parts.length > 1) {
    if (!argv[3]) return JSON.stringify({ok: false, failed: argv[1], code: 'nested', exchange: true});
    const flat = parts.join(argv[3]);
    if (exists(flat)) return JSON.stringify({ok: true, created: false, folder: flat});
    const code = create(flat);
    return JSON.stringify(code ? {ok: false, failed: flat, code: code, exchange: true}
                               : {ok: true, created: true, folder: flat});
  }
  let created = false;
  for (let i = 1; i <= parts.length; i++) {
    const path = parts.slice(0, i).join('/');
    if (exists(path)) continue;
    const code = create(path);
    if (code) return JSON.stringify({ok: false, failed: path, code: code, exchange: exchange});
    created = true;
  }
  return JSON.stringify({ok: true, created: created, folder: argv[1]});
}"""
FOLDER_WAIT_SECONDS = 30
# argv: account, source mailbox, destination mailbox, then pairs of (message id, Message-ID).
# A message is moved only when its Message-ID still matches the one it was decided under.
_MOVE = _RESOLVE + """
function run(argv) {
  const Mail = Application('Mail');
  const account = Mail.accounts.byName(argv[0]);
  const source = resolve(account, argv[1]);
  const target = resolve(account, argv[2]);
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

    def ensure_folder(self, account: str, folder: str, flat_separator: str = "") -> tuple[bool, str]:
        result = _run(_ENSURE, account, folder, str(FOLDER_WAIT_SECONDS * 2), flat_separator,
                      timeout=FOLDER_WAIT_SECONDS + 90)
        if result["ok"]:
            return bool(result["created"]), str(result["folder"])
        failed, code = str(result.get("failed") or folder), result.get("code")
        if code == "nested":
            raise ConnectorError(f"Mail cannot create subfolders on Exchange accounts. Create {folder} once in "
                                 "Outlook on the web, or set  exchange_flat_separator = \"-\"  under [actions] to "
                                 f"use a top-level folder {folder.replace('/', '-')} there")
        if code == -2:
            raise ConnectorError(f"Mail did not show the new folder {failed} within {FOLDER_WAIT_SECONDS} s; "
                                 "it may still be busy moving mail")
        raise ConnectorError(f"Mail refused to create {failed} (error {code})")

    def move(self, mailbox: Mailbox, items: list[tuple[str, str]], folder: str) -> list[str]:
        moved = []
        for start in range(0, len(items), MOVE_BATCH):
            pairs = [value for pair in items[start:start + MOVE_BATCH] for value in pair]
            moved += _run(_MOVE, mailbox.account, mailbox.name, folder, *pairs)
        return moved
