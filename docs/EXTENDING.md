# Extending Mail Triage

The core engine (`engine.py`, `policy.py`, `store.py`, `rules.py`, `learning.py`) does not
change when you add a mail source or a model. You implement a small protocol and register it
in `cli.py`.

## A new mail source (Microsoft Graph, IMAP, Gmail …)

Implement the `Connector` protocol from
[`mail_triage/connectors/base.py`](../mail_triage/connectors/base.py):

```python
class GraphConnector:
    name = "graph"

    def mailboxes(self, accounts=(), mailbox=None) -> list[Mailbox]:
        """Normally each account's inbox."""

    def list(self, mailbox) -> list[MessageInfo]:
        """Cheap metadata for every message: native id, Message-ID, sender address, subject, date.
        Called on every scan, so use one batched request (Graph: $select=id,internetMessageId,from,subject,receivedDateTime)."""

    def fetch_sources(self, mailbox, native_ids) -> dict[str, str]:
        """Raw RFC 822 source of a few messages (Graph: GET /messages/{id}/$value)."""
```

Notes per platform:

| Platform | Approach |
| --- | --- |
| **Outlook for Mac** | Experimental public JXA adapter in `connectors/outlook/macos.py`. Dictionary presence is insufficient: runtime discovery and an opt-in read probe must succeed. No live version is validated yet. |
| **Outlook on Windows (classic)** | Experimental isolated `pywin32` worker in `outlook/windows.py`: current profile, stores, `GetTable/GetArray`, selected `GetItemFromID`. Transport headers + body normalized in memory. COM SaveAs does **not** offer EML. No Windows live validation yet. |
| **New Outlook on Windows** | COM adapter unsupported. An approved Graph app is a separate potential integration, not assumed available. |
| **Approved Graph** | Own public-client OAuth application, interactive PKCE, delegated `Mail.Read` and tenant consent/Conditional Access approval. Not implemented; no write scope today. |
| **Approved IMAP** | Requires enabled protocol and authorized OAuth app. `BODY.PEEK` avoids Seen changes; UIDVALIDITY must be part of identity. Not implemented; not a consent bypass. |

**Source and identity contracts.** `fetch_sources()` returns only requested IDs; disappeared
items may be absent, but permission/timeouts/unsupported capabilities must raise a
privacy-safe `ConnectorError`. Listing must not silently truncate or zip mismatched columns.
`MessageInfo.identity_scope` is optional: legacy sources retain their old keys; native-scoped
sources use adapter/store scope + account + mailbox + native ID, not RFC Message-ID alone.
Set `headers_available=None` when metadata cannot establish header availability; junk memory
then requires fetching evidence. Explicit rules/teachings are still user overrides.

**Structured sources.** Reuse Outlook's small `normalize_source()` helper where appropriate.
It preserves genuine transport headers, replaces only content/MIME headers, adds available
sender/subject/Message-ID properties and records local provenance. Do not synthesize
unsubscribe/authentication/bulk evidence. Missing headers cap model junk to review.

**The network guard.** Native automation requires no Python network exception. Outlook/Mail
sync independently, outside the guard. A future direct-network connector needs a separately
authorized transport with narrow endpoint/port/method/TLS/redirect restrictions; model
processes must remain loopback-only. Do not disable the guard or globally allow mail hosts
for model processing. Document the new trust boundary before implementing it.

Register the connector in `cli._connector()` (for example `--source graph`) and add a test with
a fake transport, like `tests/helpers.FakeConnector`.
See [OUTLOOK_CONNECTOR_DESIGN.md](OUTLOOK_CONNECTOR_DESIGN.md) for research and the opt-in
live checklist. Never use another app's identity, browser sessions, tokens or private APIs.

## A new model backend (ONNX, OpenVINO, a Windows runtime …)

Implement `Backend` from [`mail_triage/backends/base.py`](../mail_triage/backends/base.py):

```python
class OnnxBackend:
    label = "laya-onnx"
    def predict(self, state: dict, questions: dict) -> dict: ...   # system-one "answers" format
    def close(self) -> None: ...
```

- `state` is `{"subject", "from", "body"}` and nothing else.
- `questions` uses the `choice` / `noul` types.
- Return `{qid: {"type": "choice", "probabilities": {...}}}` or
  `{qid: {"type": "noul", "noul": p}}`. Answers are validated before use.

Wire it into `backends.create()` and add a `backend` value in `config.py`. Community ONNX,
OpenVINO and GGUF builds of Laya exist on Hugging Face. A llama.cpp server already works on
Windows through the existing `systemone` backend.

## The move action (planned)

Everything is prepared, but nothing executes yet:
- `policy.Decision.folder` is the planned destination. The report shows it today.
- `connectors.base.Mover` has exactly one method: `move(mailbox, native_id, folder)`.
- `connectors.base.validate_destination()` refuses anything outside `actions.allowed_root` /
  `actions.also_allowed`, and any Trash/Junk/Deleted folder.
- The `actions` table in `store.py` is the journal: planned → applied → undone.

To implement `./triage apply`:
1. Add `move()` to a connector. For Apple Mail (JXA):
   `Mail.move(msg, {to: mailbox})`, creating the mailbox under the account if missing.
2. In a new `actions.py`, read current decisions whose folder is set and not yet applied,
   validate each destination, write a `planned` journal row, move, then mark `applied`.
3. Add `./triage apply --dry-run` (default), `./triage apply --yes`, and
   `./triage undo [--run N]`, which moves journaled messages back.
4. Keep the rule: **no delete, no trash, no flag changes**. `tests/test_privacy_safety.py`
   checks this.
5. Require explicit authorization, re-resolve native identities, journal before execution,
   validate both destinations and retry state, make retries idempotent and report failures.
   Do not acquire write permissions while only the read connector exists.

## Ideas that fit the design

- **Implicit feedback.** When the user moves a message *out* of `Laya/…` back to the inbox,
  record a teaching for that sender.
- **More questions.** Add a question in `policy.questions()` and use its answer in `decide()`,
  e.g. "needs a reply?" for a priority view.
- **Scheduled runs.** A launchd agent (macOS) or Task Scheduler entry (Windows) running
  `triage scan --console counts --no-report` every hour.
