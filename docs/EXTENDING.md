# Extending Mail Triage

The core engine (`engine.py`, `policy.py`, `store.py`, `rules.py`, `learning.py`) does not
change when you add a mail source or a model. You implement a small protocol and register it
in `cli.py`.

## A new mail source (Outlook, Microsoft Graph, IMAP, Gmail …)

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
| **Outlook on Windows (classic)** | COM via `pywin32`: `Namespace("MAPI")`, `Folder.Items`. `PR_TRANSPORT_MESSAGE_HEADERS` gives headers; build the source from `MailItem` or save it as `.eml`. |
| **New Outlook / Outlook for Mac / Microsoft 365** | Microsoft Graph with a public-client OAuth app (device-code flow), using the `Mail.Read` scope now and `Mail.ReadWrite` only once moving is enabled. |
| **IMAP (any provider)** | `imaplib`: `FETCH (BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])` to list, `BODY.PEEK[]` to fetch. `PEEK` keeps messages unread. |

**The network guard.** These connectors must talk to a mail server, so the guard has to allow
exactly that host. Add an explicit allow-list parameter to `install_network_guard()` holding
the configured mail server(s) only. Do not disable the guard. Model servers stay
loopback-only. Document the change in `docs/PRIVACY.md`.

Register the connector in `cli._connector()` (for example `--source graph`) and add a test with
a fake transport, like `tests/helpers.FakeConnector`.

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

## Ideas that fit the design

- **Implicit feedback.** When the user moves a message *out* of `Laya/…` back to the inbox,
  record a teaching for that sender.
- **More questions.** Add a question in `policy.questions()` and use its answer in `decide()`,
  e.g. "needs a reply?" for a priority view.
- **Scheduled runs.** A launchd agent (macOS) or Task Scheduler entry (Windows) running
  `triage scan --console counts --no-report` every hour.
