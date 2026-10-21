"""Experimental classic Outlook COM adapter; pywin32 is imported only inside a Windows worker.

No new Outlook support. The worker attaches to the running profile, never logs in, starts
a client, changes security settings, opens items or saves anything.
"""

from __future__ import annotations

from email.utils import parseaddr
import json
import sys
from typing import Iterable

from ..base import ConnectorError, Mailbox, MessageInfo
from .common import BODY_PREVIEW, FETCH_BATCH, normalize_sources, run_json, validate_infos


_MAPI = "http://schemas.microsoft.com/mapi/proptag/"
_HEADERS = _MAPI + "0x007D001F"
_MID = _MAPI + "0x1035001F"
_SMTP = _MAPI + "0x5D01001F"
TABLE_BATCH = 256
_NOT_FOUND = 0x8004010F


class _NativeFailure(Exception):
    def __init__(self, code):
        self.code = code


def _error_code(exc) -> int:
    details = getattr(exc, "excepinfo", None)
    code = details[5] if details and details[5] else getattr(exc, "hresult", 0)
    return code & 0xFFFFFFFF


def _property(item, name: str, com_error) -> str:
    try:
        return str(item.PropertyAccessor.GetProperty(name) or "")
    except com_error as exc:
        if _error_code(exc) != _NOT_FOUND:
            raise
    # Only a genuinely absent property may fall back, never permission or service failures.
    if name.endswith("001F"):
        try:
            return str(item.PropertyAccessor.GetProperty(name[:-4] + "001E") or "")
        except com_error as exc:
            if _error_code(exc) != _NOT_FOUND:
                raise
    return ""


def _table_rows(folder, maximum: int | None = None, full_subject=None) -> list[dict]:
    table = folder.GetTable()
    names = ["EntryID", "MessageClass", "Subject", "ReceivedTime", _SMTP, "SenderEmailAddress", _MID]
    table.Columns.RemoveAll()
    for name in names:
        table.Columns.Add(name)
    result = []
    while not table.EndOfTable:
        rows = table.GetArray(min(TABLE_BATCH, maximum) if maximum is not None else TABLE_BATCH)
        if not rows:
            raise _NativeFailure("changed")
        for row in rows:
            if len(row) != len(names):
                raise _NativeFailure("changed")
            ident, kind, subject, received, smtp, address, mid = row
            if not str(kind).startswith("IPM.Note"):
                raise _NativeFailure("unsupported")
            # Outlook tables truncate ordinary string columns to 255 characters.
            if isinstance(subject, str) and len(subject.encode("utf-8")) >= 255:
                if full_subject is None:
                    raise _NativeFailure("unsupported")
                subject = full_subject(str(ident))
            sender = smtp if isinstance(smtp, str) and "@" in smtp else address
            result.append({"id": str(ident or ""),
                           "mid": mid if isinstance(mid, str) and len(mid.encode("utf-8")) < 255 else "",
                           "sender": sender if isinstance(sender, str) and "@" in sender
                           and len(sender.encode("utf-8")) < 255 else "",
                           "subject": str(subject or ""),
                           "date": received.isoformat() if received else ""})
        if maximum is not None:
            break
    return result


def _read_item(item, com_error) -> dict:
    before = item.UnRead
    if item.Class != 43:
        raise _NativeFailure("unsupported")
    if item.DownloadState != 1 or item.Permission != 0:
        raise _NativeFailure("protected")
    headers = _property(item, _HEADERS, com_error)
    body, html = item.Body or "", False
    if not body and item.BodyFormat == 2:
        body, html = item.HTMLBody or "", True
    sender = _property(item, _SMTP, com_error)
    if not sender and item.SenderEmailType == "SMTP":
        sender = item.SenderEmailAddress or ""
    result = {"headers": headers, "sender": sender, "subject": item.Subject or "",
              "body": body[:BODY_PREVIEW], "html": html, "message_id": _property(item, _MID, com_error)}
    if before != item.UnRead:
        raise _NativeFailure("changed")
    return result


def _execute(action: str, data: dict) -> dict:
    try:
        import pythoncom
        import pywintypes
        import win32com.client
    except ImportError:
        return {"ok": False, "code": "pywin32"}
    pythoncom.CoInitialize()
    try:
        try:
            app = win32com.client.GetActiveObject("Outlook.Application")
        except pywintypes.com_error:
            return {"ok": False, "code": "profile"}
        session = app.GetNamespace("MAPI")
        if action == "discover":
            emails = {account.DeliveryStore.StoreID: account.SmtpAddress or ""
                      for account in session.Accounts if account.DeliveryStore is not None}
            stores, without_inbox = [], 0
            for store in session.Stores:
                try:
                    inbox = store.GetDefaultFolder(6)
                    inbox_id = inbox.EntryID
                except pywintypes.com_error as exc:
                    if _error_code(exc) != _NOT_FOUND:
                        raise
                    without_inbox += 1
                    continue
                stores.append({"storeId": store.StoreID, "name": store.DisplayName,
                               "email": emails.get(store.StoreID, ""), "folderId": inbox_id})
            result = {"version": app.Version, "stores": stores, "withoutInbox": without_inbox}
        else:
            store = session.GetStoreFromID(data["storeId"])
            if data["path"] == "INBOX":
                folder = store.GetDefaultFolder(6)
            else:
                folder = store.GetRootFolder()
                for part in data["path"].split("/"):
                    folder = folder.Folders.Item(part)
            if data.get("folderId") and folder.EntryID != data["folderId"]:
                raise _NativeFailure("changed")

            def full_subject(ident):
                item = session.GetItemFromID(ident, data["storeId"])
                before = item.UnRead
                subject = item.Subject or ""
                if before != item.UnRead:
                    raise _NativeFailure("changed")
                return subject

            if action == "resolve":
                result = {"folderId": folder.EntryID}
            elif action == "list":
                result = _table_rows(folder, full_subject=full_subject)
            elif action == "fetch":
                result = {}
                for ident in data["ids"]:
                    try:
                        item = session.GetItemFromID(ident, data["storeId"])
                    except pywintypes.com_error as exc:
                        if _error_code(exc) != _NOT_FOUND:
                            raise
                        continue
                    if item.Parent.EntryID != folder.EntryID or item.Parent.StoreID != data["storeId"]:
                        continue
                    result[ident] = _read_item(item, pywintypes.com_error)
            elif action == "probe":
                rows = _table_rows(folder, maximum=1, full_subject=full_subject)
                result = {"count": folder.Items.Count, "metadata": bool(rows), "sampled": False}
                if rows and data["readSample"]:
                    item = session.GetItemFromID(rows[0]["id"], data["storeId"])
                    value = _read_item(item, pywintypes.com_error)
                    result.update(sampled=True, headers=bool(value["headers"].strip()), body=bool(value["body"]))
            else:
                raise _NativeFailure("unsupported")
        return {"ok": True, "data": result}
    except _NativeFailure as exc:
        return {"ok": False, "code": exc.code}
    except pywintypes.com_error as exc:
        return {"ok": False, "code": _error_code(exc), "operation": action}
    finally:
        pythoncom.CoUninitialize()


class WindowsOutlookConnector:
    name = "outlook-windows"

    def __init__(self):
        if sys.platform != "win32":
            raise ConnectorError("Outlook COM is available only on Windows with classic Outlook")
        self._boxes: dict[Mailbox, dict] = {}
        self.version = "unknown"
        self.without_inbox = 0

    def _call(self, action: str, data: dict):
        return run_json([sys.executable, "-m", "mail_triage.connectors.outlook.windows"],
                        input=json.dumps({"action": action, "data": data}))

    def mailboxes(self, accounts: Iterable[str] = (), mailbox: str | None = None) -> list[Mailbox]:
        self._boxes = {}
        requested = set(accounts)
        if mailbox is not None and len(requested) != 1:
            raise ConnectorError("--mailbox needs exactly one --account")
        path = mailbox if mailbox is not None else "INBOX"
        if not path or any(part in ("", ".", "..") for part in path.split("/")):
            raise ConnectorError("Outlook mailbox must be INBOX or a folder path beneath the store root")
        discovered = self._call("discover", {})
        self.version, self.without_inbox = discovered["version"], discovered["withoutInbox"]
        if not discovered["stores"]:
            raise ConnectorError("No accessible inbox stores in the current classic Outlook profile")
        boxes, matched = {}, set()
        for row in discovered["stores"]:
            aliases = {row["email"], row["name"]} - {""}
            if requested and not requested.intersection(aliases):
                continue
            if matched.intersection(aliases):
                raise ConnectorError("Outlook account selector is ambiguous (identifiers hidden)")
            matched.update(requested.intersection(aliases))
            box = Mailbox(row["email"] or row["name"], path)
            if box in boxes:
                raise ConnectorError("Outlook account selection is ambiguous; duplicate store labels")
            ref = {"storeId": row["storeId"], "path": path}
            ref.update(self._call("resolve", ref))
            boxes[box] = ref
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
        rows = self._call("list", ref)
        scope = "outlook-windows:" + ref["storeId"]
        infos = [MessageInfo(row["id"], row["mid"], parseaddr(row["sender"])[1].lower(),
                             row["subject"], row["date"], scope, None) for row in rows]
        validate_infos(infos)
        return infos

    def fetch_sources(self, mailbox: Mailbox, native_ids: list[str]) -> dict[str, str]:
        ref = self._reference(mailbox)
        sources = {}
        for start in range(0, len(native_ids), FETCH_BATCH):
            ids = native_ids[start:start + FETCH_BATCH]
            rows = self._call("fetch", {**ref, "ids": ids})
            sources.update(normalize_sources(rows, ids))
        return sources

    def diagnose(self, *, accounts=(), read_sample: bool = False) -> tuple[bool, list[str]]:
        lines = ["adapter: Windows classic COM (experimental); new Outlook is unsupported",
                 "permission: current signed-in profile and IT/Object Model Guard approval; no security bypass",
                 "unsupported: writes, attachments, original MIME export, new Outlook"]
        try:
            boxes = self.mailboxes(accounts)
            lines.append(f"Outlook version: {self.version}; reachable inbox stores: {len(boxes)}; "
                         f"stores without inbox: {self.without_inbox} (identifiers hidden)")
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
            lines.append("sample read succeeded; Windows live compatibility requires independent validation")
            return True, lines
        except ConnectorError as exc:
            lines.append(f"read capability: BLOCKED: {exc}")
            return False, lines


def _worker_main() -> None:
    from ...privacy import install_network_guard
    install_network_guard()
    if sys.platform != "win32":
        reply = {"ok": False, "code": "profile"}
    else:
        try:
            request = json.load(sys.stdin)
            reply = _execute(request["action"], request["data"])
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            reply = {"ok": False, "code": "native"}
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(reply, ensure_ascii=True))


if __name__ == "__main__":
    _worker_main()
