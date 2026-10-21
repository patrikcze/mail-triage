"""Public Outlook JXA dictionary adapter. Experimental until validated on the user's client.

No UI operations, source/attachment reads, setters or credential properties. All parameters
are JSON data in argv, never inserted into the fixed script.
"""

from __future__ import annotations

from email.utils import parseaddr
import json
from pathlib import Path
import plistlib
import re
import subprocess
import sys
from typing import Iterable
import xml.etree.ElementTree as ET

from ..base import ConnectorError, Mailbox, MessageInfo
from .common import FETCH_BATCH, normalize_sources, run_json, validate_infos


_SCRIPT = r"""
function run(argv) {
  function fail(code) { throw {safeCode: code}; }
  function text(value) { return value === null || value === undefined ? '' : String(value); }
  function date(value) { return value ? value.toISOString() : ''; }
  const app = Application('com.microsoft.Outlook');
  let operation = 'discover';
  try {
    const action = argv[0], data = JSON.parse(argv[1]);
    operation = action;
    function folder() {
      const account = app[data.kind].byId(Number(data.accountId));
      let mb;
      if (data.path === 'INBOX') {
        mb = account.inbox();
      } else {
        mb = account.rootFolder();
        for (const part of data.path.split('/')) mb = mb.mailFolders.byName(part);
      }
      if (data.folderId && String(mb.id()) !== data.folderId) fail('changed');
      return mb;
    }
    function read(msg) {
      operation = 'read-state';
      const before = msg.isRead();
      if (msg.isRightsProtected() || msg.isPartiallyDownloaded()) fail('protected');
      operation = 'headers';
      const headers = text(msg.headers());
      operation = 'body';
      let body = text(msg.plainTextContent()), html = false;
      if (!body) {
        html = Boolean(msg.hasHtml());
        body = text(msg.content());
      }
      body = body.slice(0, 12000);
      operation = 'metadata';
      const value = {headers: headers, sender: text(msg.sender().address), subject: text(msg.subject()),
                     body: body, html: html};
      operation = 'read-state';
      if (before !== msg.isRead()) fail('changed');
      return value;
    }
    let result;
    if (action === 'discover') {
      result = [];
      for (const kind of ['exchangeAccounts', 'imapAccounts', 'popAccounts']) {
        for (const account of app[kind]()) {
          result.push({kind: kind, accountId: String(account.id()), name: text(account.name()),
                       email: text(account.emailAddress())});
        }
      }
    } else if (action === 'resolve') {
      result = {folderId: String(folder().id())};
    } else if (action === 'list') {
      const messages = folder().messages;
      const ids = messages.id(), before = messages.isRead();
      const senders = messages.sender(), subjects = messages.subject(), dates = messages.timeReceived();
      if (JSON.stringify(ids) !== JSON.stringify(messages.id()) ||
          JSON.stringify(before) !== JSON.stringify(messages.isRead())) fail('changed');
      result = {ids: ids, senders: senders.map(s => text(s.address)),
                subjects: subjects.map(text), dates: dates.map(date)};
    } else if (action === 'fetch') {
      const messages = folder().messages;
      result = {};
      for (const id of data.ids) {
        const msg = messages.byId(Number(id));
        try { msg.id(); } catch (e) {
          if (Number(e.errorNumber || e.number) === -1728) continue;
          throw e;
        }
        result[id] = read(msg);
      }
    } else if (action === 'probe') {
      const messages = folder().messages, ids = messages.id();
      result = {count: ids.length, metadata: false, sampled: false};
      if (ids.length) {
        const msg = messages.byId(ids[0]), before = msg.isRead();
        msg.sender(); msg.subject(); msg.timeReceived(); msg.id();
        if (before !== msg.isRead()) fail('changed');
        result.metadata = true;
        if (data.readSample) {
          const value = read(msg);
          result.sampled = true;
          result.headers = Boolean(value.headers.trim());
          result.body = Boolean(value.body);
          result.unchanged = before === msg.isRead();
          if (!result.unchanged) fail('changed');
        }
      }
    } else {
      fail('unsupported');
    }
    return JSON.stringify({ok: true, data: result});
  } catch (e) {
    return JSON.stringify({ok: false, code: e.safeCode || Number(e.errorNumber || e.number) || 'native',
                           operation: operation});
  }
}
"""


def inspect_installation() -> dict:
    """Inspect only the public application bundle, never Outlook's profile or databases."""
    if sys.platform != "darwin":
        raise ConnectorError("Outlook JXA is only available on macOS")
    paths = (Path("/Applications/Microsoft Outlook.app"), Path.home() / "Applications/Microsoft Outlook.app")
    app = next((path for path in paths if path.is_dir()), None)
    if app is None:
        raise ConnectorError("Microsoft Outlook.app not found in /Applications or ~/Applications")
    try:
        with (app / "Contents/Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
        version = str(info.get("CFBundleShortVersionString", "unknown"))
        build = str(info.get("CFBundleVersion", "unknown"))
        result = subprocess.run(["/usr/bin/sdef", str(app)], capture_output=True, timeout=30)
        if result.returncode:
            raise ConnectorError(f"Outlook {version} build {build} has no accessible public scripting dictionary")
        root = ET.fromstring(result.stdout)
    except (OSError, ValueError, ET.ParseError, subprocess.TimeoutExpired):
        raise ConnectorError("Could not inspect Outlook's public version/scripting dictionary") from None
    required = {
        "account": {"name", "email address", "inbox", "root folder"},
        "object": {"id"},
        "message": {"sender", "subject", "time received", "headers", "plain text content",
                    "content", "has html", "is read", "is rights protected", "is partially downloaded"},
    }
    classes = {node.get("name"): node for node in root.iter("class")}
    for name, properties in required.items():
        declared = {node.get("name") for node in classes.get(name, ()) if node.tag == "property"}
        if not properties <= declared:
            raise ConnectorError(f"Outlook {version} build {build} public dictionary lacks required "
                                 "read capabilities; adapter unavailable")
    if info.get("CFBundleIdentifier") != "com.microsoft.Outlook":
        raise ConnectorError("Unexpected Outlook application identifier")
    return {"version": version, "build": build}


class MacOutlookConnector:
    name = "outlook-macos"

    def __init__(self):
        self.installation = inspect_installation()
        self._boxes: dict[Mailbox, dict] = {}

    def _call(self, action: str, data: dict):
        return run_json(["/usr/bin/osascript", "-l", "JavaScript", "-e", _SCRIPT,
                         action, json.dumps(data)])

    def mailboxes(self, accounts: Iterable[str] = (), mailbox: str | None = None) -> list[Mailbox]:
        self._boxes = {}
        requested = set(accounts)
        if mailbox is not None and len(requested) != 1:
            raise ConnectorError("--mailbox needs exactly one --account")
        path = mailbox if mailbox is not None else "INBOX"
        if not path or any(part in ("", ".", "..") for part in path.split("/")):
            raise ConnectorError("Outlook mailbox must be INBOX or a folder path beneath the account root")
        rows = self._call("discover", {})
        if not rows:
            raise ConnectorError("Outlook exposes no script-visible accounts. Client mode is unknown; "
                                 "a modern client may lack this interface. Ask IT; no fallback was attempted.")
        boxes, matched = {}, set()
        for row in rows:
            aliases = {row["name"], row["email"]} - {""}
            if requested and not requested.intersection(aliases):
                continue
            if matched.intersection(aliases):
                raise ConnectorError("Outlook account selector is ambiguous (identifiers hidden)")
            matched.update(requested.intersection(aliases))
            box = Mailbox(row["email"] or row["name"] or row["accountId"], path)
            if box in boxes:
                raise ConnectorError("Outlook account selection is ambiguous; duplicate account labels")
            reference = {"kind": row["kind"], "accountId": row["accountId"], "path": path}
            reference.update(self._call("resolve", reference))
            boxes[box] = reference
        if requested - matched:
            raise ConnectorError("One or more selected Outlook accounts were not found (identifiers hidden)")
        self._boxes = boxes
        return list(boxes)

    def _reference(self, mailbox: Mailbox) -> dict:
        if mailbox not in self._boxes:
            raise ConnectorError("Discover the Outlook mailbox before reading it")
        return self._boxes[mailbox]

    def list(self, mailbox: Mailbox) -> list[MessageInfo]:
        ref = self._reference(mailbox)
        data = self._call("list", ref)
        columns = [data.get(name) for name in ("ids", "senders", "subjects", "dates")]
        if any(not isinstance(column, list) for column in columns) or len({len(c) for c in columns}) != 1:
            raise ConnectorError("Outlook metadata columns are incomplete; no messages were silently omitted")
        if any(not self._valid_id(str(ident)) for ident in columns[0]):
            raise ConnectorError("Outlook returned invalid native message IDs")
        if any(not isinstance(value, str) for column in columns[1:] for value in column):
            raise ConnectorError("Outlook returned invalid metadata properties")
        scope = f"outlook-macos:{ref['kind']}:{ref['accountId']}"
        infos = [MessageInfo(str(i), "", parseaddr(sender)[1].lower(), subject, date, scope, None)
                 for i, sender, subject, date in zip(*columns)]
        validate_infos(infos)
        return infos

    @staticmethod
    def _valid_id(value: str) -> bool:
        return isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,16}", value) is not None \
            and abs(int(value)) <= 9_007_199_254_740_991

    def fetch_sources(self, mailbox: Mailbox, native_ids: list[str]) -> dict[str, str]:
        ref = self._reference(mailbox)
        if any(not self._valid_id(ident) for ident in native_ids):
            raise ConnectorError("Invalid Outlook native message ID")
        sources = {}
        for start in range(0, len(native_ids), FETCH_BATCH):
            ids = native_ids[start:start + FETCH_BATCH]
            rows = self._call("fetch", {**ref, "ids": ids})
            sources.update(normalize_sources(rows, ids))
        return sources

    def diagnose(self, *, accounts=(), read_sample: bool = False) -> tuple[bool, list[str]]:
        lines = [f"adapter: macOS public JXA (experimental); Outlook {self.installation['version']} "
                 f"build {self.installation['build']}; classic/modern mode: unknown",
                 "permission: terminal Automation > Microsoft Outlook; corporate approval required",
                 "unsupported: writes, attachments, original MIME export, unexposed shared accounts"]
        try:
            boxes = self.mailboxes(accounts)
            lines.append(f"account/inbox discovery: {len(boxes)} reachable (identifiers hidden)")
            sampled = False
            for number, box in enumerate(boxes, 1):
                result = self._call("probe", {**self._reference(box), "readSample": read_sample})
                lines.append(f"inbox {number}: {result['count']} messages; metadata read: "
                             f"{'yes' if result['metadata'] else 'untested (empty)'}")
                if result["sampled"]:
                    sampled = True
                    lines.append(f"inbox {number} sample: headers={'yes' if result['headers'] else 'missing'}; "
                                 f"body={'yes' if result['body'] else 'empty'}; read state unchanged")
            if not read_sample or not sampled:
                lines.append("body/header read access: UNVERIFIED; opt in with --probe-read on a test inbox")
                return False, lines
            lines.append("sample read succeeded; version-wide compatibility and asynchronous read effects unverified")
            return True, lines
        except ConnectorError as exc:
            lines.append(f"read capability: BLOCKED: {exc}")
            return False, lines
