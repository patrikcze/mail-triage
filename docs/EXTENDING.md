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

## The move action

`./triage apply` / `./triage undo` (in `actions.py`) decide what may move; a connector only
carries it out by implementing `connectors.base.Mover`:

- `ensure_folder(account, folder) -> bool` creates a missing destination folder (and its
  parents) inside the account. It never renames or deletes folders.
- `move(mailbox, [(native_id, message_id), …], folder) -> moved native ids` moves messages
  within one account. It must re-check each message's Message-ID inside the mail program
  right before moving and skip any mismatch, so a stale or reused native id cannot move a
  different message.

`actions.py` re-lists the mailbox, matches messages to current decisions by identity, applies
the guards (current settings, same mailbox, `keep_in_place`, `validate_destination()`, not
moved before), journals each move as `planned` before it happens and marks it `applied` or
`failed`. Undo lists the destination folder and moves journaled messages back.

Apple Mail implements `Mover` with two fixed JXA scripts (`_ENSURE`, `_MOVE` in
`applemail.py`). Keep the rule: **no delete, no trash, no flag or read-state changes**.
`tests/test_privacy_safety.py` checks this.

## Ideas that fit the design

- **Implicit feedback.** When the user moves a message *out* of `Laya/…` back to the inbox,
  record a teaching for that sender.
- **More questions.** Add a question in `policy.questions()` and use its answer in `decide()`,
  e.g. "needs a reply?" for a priority view.
- **Scheduled runs.** A launchd agent (macOS) or Task Scheduler entry (Windows) running
  `triage scan --console counts --no-report` every hour.
