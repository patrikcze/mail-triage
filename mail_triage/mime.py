"""Turn raw RFC 822 source into classifier input and header evidence. Standard library only."""

import email
from email import policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
import re
import secrets


BODY_CHARS = 3000
MAX_SOURCE = 2_000_000          # bytes parsed per message; attachments beyond this are ignored
_QUOTE_START = re.compile(r"^\s*(On .{0,300}wrote:|-{2,}\s*(Original|Forwarded) Message|Dne .{0,300}napsal|"
                          r"Am .{0,300}schrieb|From:\s.+|Od:\s.+|_{8,})\s*$", re.I)
_SIGNATURE = re.compile(r"^\s*(--|sent from my \w+|odesláno z \w+)\s*$", re.I)
_NOREPLY = re.compile(
    r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|newsletter|^news@|^info@|marketing|promo|"
    r"^offers?@|^deals?@|^hello@|mailer|^campaign|^updates?@)", re.I)
# Headers set by bulk/ESP senders (Mailchimp, SendGrid, Mailgun, Salesforce, Marketo, ...).
_ESP_HEADERS = ("x-campaign", "x-campaign-id", "x-campaignid", "x-mailgun-tag", "x-mailgun-sid",
                "x-sg-eid", "x-mc-user", "x-mailchimp-campaign", "x-sfmc-stack", "x-marketo-id",
                "x-mailjet-campaign", "x-ems-tracking", "x-csa-complaints", "x-newsletter",
                "x-mkt-id", "x-sendinblue-campaign", "x-brevo-campaign")
_ESP_MAILERS = re.compile(r"(mailchimp|sendgrid|mailgun|mailjet|sendinblue|brevo|klaviyo|"
                          r"hubspot|marketo|salesforce|exacttarget|campaign|newsletter|ecomail|smartemailing)", re.I)
_REPLY_SUBJECT = re.compile(r"^\s*(re|aw|odp|sv|vs|antw|r)\s*:", re.I)
# Receipts, orders, payments, account notices (en/cs/sk/de). Kept narrow on purpose:
# promos say "order now", "free delivery" or "discount code", which must not match.
_TRANSACTIONAL = re.compile(
    r"(your order|order (confirmation|#|no\.? ?\d|number|\d)|receipt|invoice|payment|refund|"
    r"has shipped|dispatched|tracking number|out for delivery|delivered|booking confirmation|reservation confirm|"
    r"statement|balance|verification code|security code|one-time (code|password)|password reset|"
    r"sign-in|new login|log-?in code|sign-in code|multi-factor|two-factor|2fa|passkey|security key|"
    r"security alert|account data|account (has|was) been|account (is|was) (set up|created|enabled)|"
    r"objedn[áa]vk|faktur|[úu][čc]tenk|stvrzenk|platb|zaplacen|vr[áa]cen[íi] pen[ěe]z|"
    r"z[áa]silk|doru[čc]en|rezervac|v[ýy]pis|z[ůu]statek|ov[ěe][řr]ovac[íi] k[óo]d|heslo|p[řr]ihl[áa][šs]en|"
    r"bezpe[čc]nostn[íi] upozorn[ěe]n|"
    r"bestellung|rechnung|zahlung|lieferung|versandbest[äa]tigung|buchungsbest[äa]tigung|kontoauszug)",
    re.I)
# Contracts, legal/terms notices and account actions (en/cs/de). Promos say "bez smlouvy" or
# "výhodné podmínky" too, but those carry the (OS) marker, which overrides this guard.
_NOTICE = re.compile(
    r"(action (required|needed)|security (setup|update)|"
    r"access (has been|was) (paused|suspended|revoked)|"
    r"subscription (access )?(has been |was |is )?(paused|cancell?ed|suspended|expir)|"
    r"account settings|secure your [\w ]{0,40}account|licen[cs]e key|terms of (service|use)|privacy policy|your contract|"
    r"smlouv|aktualizovan[ée] podm[íi]nk|zm[ěe]n[ay]? (obchodn[íi]ch |smluvn[íi]ch )?podm[íi]n|"
    r"obchodn[íi] podm[íi]nk|smluvn[íi]ch podm[íi]n|ochran[ay] osobn[íi]ch [úu]daj|"
    r"spr[áa]vce|doru[čc]ovac[íi] adres|kontaktn[íi] [úu]daj|p[řr][íi]stupov[ýy] k[óo]d|"
    r"licen[čc]n[íi] kl[íi][čc]|d[ůu]le[žz]it[ée] upozorn[ěe]n|j[íi]zdenk|"
    r"vertrag|k[üu]ndigung|datenschutz|nutzungsbedingungen)",
    re.I)
# Czech law requires commercial messages to be marked "obchodní sdělení", usually "(OS)".
_COMMERCIAL_MARKER = re.compile(r"\((OS|obchodn[íi] sd[ěe]len[íi])\)", re.I)
# Sender roles that send account, billing and order mail (noreply-accounts@, invoice+x@, payments@).
_ACCOUNT_SENDER = re.compile(
    r"(^|[.\-_+])(accounts?|security|billing|invoices?|receipts?|payments?|orders?|bank(ing)?)([.\-_+]|@)", re.I)
_AUTH_FAIL = re.compile(r"\b(spf|dkim|dmarc)=(fail|softfail|permerror)\b", re.I)
_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "section", "td"}


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head", "title"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head", "title"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return "".join(parser.parts)


def _normalise(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[ \t​‌͏]+", " ", text)
    text = re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", text)
    return text.strip()


def _part_text(part) -> str:
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def extract_body(msg) -> str:
    """Prefer the plain-text part; fall back to visible text of the HTML part."""
    for kind in ("plain", "html"):
        try:
            part = msg.get_body(preferencelist=(kind,))
        except Exception:
            part = None
        if part is None:
            continue
        text = _part_text(part)
        if kind == "html":
            text = html_to_text(text)
        text = _normalise(text)
        if len(text) >= 20 or kind == "html":
            return text[:BODY_CHARS * 2]
    return ""


def _header(msg, name: str) -> str:
    try:
        return str(msg.get(name, "") or "")
    except Exception:
        return ""


PROVENANCE = "X-Mail-Triage-Source-Provenance"
# Random per process: a received email cannot forge the marker that Mail Triage's own
# structured-source normalizer adds, so raw mail (Apple Mail, .eml) is never affected by it.
PROVENANCE_TOKEN = secrets.token_hex(16)


def provenance_value(kind: str) -> str:
    return f"{kind}; token={PROVENANCE_TOKEN}"


def header_signals(msg, from_address: str, subject: str) -> dict:
    keys = {key.lower() for key in msg.keys()}
    precedence = _header(msg, "Precedence").strip().lower()
    auto = _header(msg, "Auto-Submitted").strip().lower()
    mailer = _header(msg, "X-Mailer")
    return {
        "headers_unavailable": _header(msg, PROVENANCE) == provenance_value("structured-without-headers"),
        "list_unsubscribe": "list-unsubscribe" in keys,
        "bulk": precedence in ("bulk", "list", "junk") or "list-id" in keys,
        "esp": any(name in keys for name in _ESP_HEADERS) or bool(_ESP_MAILERS.search(mailer)),
        "noreply": bool(_NOREPLY.search(from_address)),
        "automated": bool(auto) and auto != "no",
        "is_reply": bool(_header(msg, "In-Reply-To") or _REPLY_SUBJECT.match(subject)),
        "auth_fail": bool(_AUTH_FAIL.search(_header(msg, "Authentication-Results"))),
        **subject_signals(from_address, subject),
    }


def subject_signals(from_address: str, subject: str) -> dict:
    """Guards that need only sender and subject: usable before the message is fetched."""
    return {
        "transactional": bool(_TRANSACTIONAL.search(subject)),
        "notice": bool(_NOTICE.search(subject)),
        "commercial_marker": bool(_COMMERCIAL_MARKER.search(subject)),
        "account_sender": bool(_ACCOUNT_SENDER.search(from_address)),
    }


def clean_body(text: str, limit: int = BODY_CHARS) -> str:
    """Drop quoted replies and signatures so the model reads what this email itself says."""
    lines = []
    for line in text.split("\n"):
        if lines and _QUOTE_START.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        if _SIGNATURE.match(line) and len(lines) > 3:
            break
        lines.append(line)
    return _normalise("\n".join(lines))[:limit]


def parse_message(source: str) -> dict:
    """Parse raw source into fields the classifier and the report need. Nothing here is stored."""
    # Mail.app hands over source as text. Parsing it as a string mangles 8bit (unencoded UTF-8)
    # bodies, so go back to bytes and let each part's charset decode it.
    raw = (source or "").encode("utf-8", errors="surrogateescape")[:MAX_SOURCE]
    msg = email.message_from_bytes(raw, policy=policy.default)
    subject = _normalise(_header(msg, "Subject"))
    name, address = parseaddr(_header(msg, "From"))
    sender = f"{name} <{address}>" if name else address
    try:
        date = parsedate_to_datetime(_header(msg, "Date")).isoformat()
    except (TypeError, ValueError, IndexError):
        date = ""
    headers = {}
    for key in msg.keys():
        headers.setdefault(key.lower(), _header(msg, key))
    signals = header_signals(msg, address.lower(), subject)
    return {
        "message_id": _header(msg, "Message-ID").strip(),
        "subject": subject,
        "from": sender,
        "from_address": address.lower(),
        "date": date,
        "body": extract_body(msg),
        "headers": headers,
        "signals": signals,
    }


def model_state(mail: dict, body_chars: int = BODY_CHARS) -> dict:
    """The only email content a model ever sees: subject, sender and the cleaned body start."""
    return {"subject": mail["subject"], "from": mail["from"], "body": clean_body(mail["body"], body_chars)}
