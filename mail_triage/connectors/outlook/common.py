"""In-memory structured-message normalization and privacy-safe automation boundaries."""

from __future__ import annotations

from email import policy
from email.errors import HeaderParseError
from email.parser import HeaderParser
import json
import subprocess

from ..base import ConnectorError


FETCH_BATCH = 10
BODY_PREVIEW = 12_000
from ...mime import PROVENANCE, provenance_value  # noqa: F401  (PROVENANCE is re-exported)


def normalize_source(*, headers: str, sender: str, subject: str, body: str,
                     html: bool = False, message_id: str = "") -> str:
    """Not original MIME: only transport headers are evidence; content headers are rebuilt."""
    if not all(isinstance(value, str) for value in (headers, sender, subject, body, message_id)) \
            or not isinstance(html, bool):
        raise ConnectorError("Outlook returned invalid structured message properties")
    if len(headers) > 256_000:
        raise ConnectorError("Outlook transport headers exceed the safe normalization limit")
    try:
        msg = HeaderParser(policy=policy.default).parsestr(headers)
        available = any(key.lower() != PROVENANCE.lower() and key.lower() != "mime-version"
                        and not key.lower().startswith("content-") for key in msg.keys())
        msg.clear_content()
        if "MIME-Version" in msg:
            del msg["MIME-Version"]
        if PROVENANCE in msg:
            del msg[PROVENANCE]
        msg[PROVENANCE] = provenance_value("structured-with-headers" if available else "structured-without-headers")
        for name, value in (("From", sender), ("Subject", subject), ("Message-ID", message_id)):
            if name not in msg and value:
                msg[name] = " ".join(value.splitlines())
        msg.set_content(body[:BODY_PREVIEW], subtype="html" if html else "plain", charset="utf-8")
        return msg.as_string()
    except (ValueError, UnicodeError, HeaderParseError):
        raise ConnectorError("Outlook message could not be safely normalized; no content was logged") from None


def run_json(command: list[str], *, input: str | None = None, timeout: float = 120):
    try:
        result = subprocess.run(command, input=input, capture_output=True, text=True,
                                encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ConnectorError("Outlook automation timed out; close dialogs or retry a smaller scan") from None
    except (OSError, UnicodeError):
        raise ConnectorError("Outlook automation could not start or returned invalid text") from None
    # Native errors can quote account names, headers, credentials or message text.
    if result.returncode:
        if "-1743" in result.stderr:
            raise ConnectorError("Outlook Automation permission denied. Enable your terminal under "
                                 "System Settings > Privacy & Security > Automation > Microsoft Outlook.")
        raise ConnectorError("Outlook automation failed; no native error text was logged")
    try:
        reply = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise ConnectorError("Outlook automation returned invalid JSON; no native output was logged") from None
    if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
        raise ConnectorError("Outlook automation returned an invalid response")
    if not reply["ok"]:
        code = reply.get("code")
        if code == -1743:
            raise ConnectorError("Outlook Automation permission denied. Enable your terminal under "
                                 "System Settings > Privacy & Security > Automation > Microsoft Outlook.")
        if code == -1712:
            raise ConnectorError("Outlook did not answer before the Apple Event timeout")
        if code == "changed":
            raise ConnectorError("Outlook mailbox or read state changed during reading; scan stopped without writes")
        if code == "protected":
            raise ConnectorError("Outlook item is protected or not fully downloaded; no content was classified")
        if code == "pywin32":
            raise ConnectorError("Windows Outlook requires the optional outlook-windows dependency (pywin32)")
        if code == "profile":
            raise ConnectorError("Start signed-in classic Outlook; new Outlook for Windows has no supported COM adapter")
        if code == "folder":
            raise ConnectorError("Outlook folder is unavailable in the selected account/store")
        if code == "unsupported":
            raise ConnectorError("Outlook returned an unsupported item type; no items were silently omitted")
        operations = {"discover", "resolve", "list", "fetch", "probe", "headers", "body", "metadata", "read-state"}
        operation = reply.get("operation")
        if isinstance(code, int) and isinstance(operation, str) and operation in operations:
            raise ConnectorError(f"Outlook {operation} read failed (native code {code}); "
                                 "the client may not expose this item/property or corporate policy may block it")
        raise ConnectorError("Outlook scripting/read access failed; check client capabilities and corporate policy")
    if "data" not in reply:
        raise ConnectorError("Outlook automation response has no data")
    return reply["data"]


def validate_infos(infos) -> None:
    if any(not isinstance(value, str) for info in infos
           for value in (info.native_id, info.message_id, info.sender, info.subject, info.received)):
        raise ConnectorError("Outlook returned invalid metadata properties")
    ids = [info.native_id for info in infos]
    if any(not ident for ident in ids) or len(set(ids)) != len(ids):
        raise ConnectorError("Outlook returned missing or duplicate native IDs; scan stopped")


def normalize_sources(rows, native_ids: list[str]) -> dict[str, str]:
    if not isinstance(rows, dict) or set(rows) - set(native_ids):
        raise ConnectorError("Outlook returned unrequested message content")
    required = {"headers", "sender", "subject", "body", "html"}
    sources = {}
    for ident, row in rows.items():
        if not isinstance(row, dict) or not required <= row.keys() or row.keys() - required - {"message_id"}:
            raise ConnectorError("Outlook returned invalid structured message properties")
        sources[ident] = normalize_source(**row)
    return sources
