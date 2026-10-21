# Privacy and data protection

Mail Triage processes personal data: the emails in your inbox. This page explains what is
processed, where, what is kept, how to erase it, and the limits of the local-processing
safeguards. It is written for users and for whoever assesses the tool under the GDPR.

## Summary

| Question | Answer |
| --- | --- |
| Where is mail processed? | On this computer only. The AI model runs locally. |
| Is mail sent to external AI? | No. Python/model socket connections are loopback-only; external mail clients may still synchronize normally. |
| What is stored? | Sender address, subject (optional), date, mailbox, decision and reasons. **Never the body.** |
| Where? | Your data folder (`./triage doctor` shows it), readable by your user account only. |
| How long? | 365 days after the email was last seen (configurable, `privacy.retention_days`). |
| How to erase? | `./triage forget --sender ADDRESS`, `--older-than DAYS`, or `--all`. |
| Telemetry or analytics? | None. |

## What happens to an email

1. **Listing.** Apple Mail or the selected experimental Outlook adapter gives native ID,
   sender, subject, received date and RFC Message-ID where available, for inbox messages.
   Outlook Mac metadata has no separate RFC Message-ID getter; this field remains unknown
   until real headers are read. No bodies or attachments are fetched for native listing.
2. **Shortcuts.** Your rules, teachings and the sender memory may decide right away. The email
   body is never read in that case.
3. **Classification.** Otherwise raw/normalized message text is fetched into memory. Only the **subject,
   sender and the first ~3,000 characters of the body** (quoted replies and signatures removed)
   are passed to the local model.
4. **Storage.** The decision is stored. Sources are fetched in small batches and released
   from that batch as each is classified. Bodies and raw headers are never stored in history.

## Technical safeguards

- **Network guard** (`mail_triage/privacy.py`). Every command that reads mail (`scan`,
  `report`, `teach`, `doctor`, `start`) installs a guard first. From then on the Python process
  can only connect to loopback addresses (127.0.0.1, ::1) or local Unix sockets, and can only
  resolve `localhost`. The Laya MLX worker installs the same guard **before** the model library
  is loaded. Hugging Face libraries are additionally forced into offline mode. Tests prove that
  outbound connections, DNS lookups and UDP fail
  (`tests/test_privacy_safety.py`).
- **Local model servers only.** A llama.cpp/Ollama URL that is not loopback is rejected when
  the settings are loaded, and again when the backend starts.
- **`setup` is the only model-download step.** It downloads pinned files from Hugging Face
  before installing the guard, then performs local model/source discovery. It never reads
  message bodies or sends mail to the download service.
- **Private files.** The data folder is mode 0700. `history.sqlite`, reports and the settings
  file are mode 0600. SQLite runs with `secure_delete`, so erased rows are overwritten, and
  `forget` rewrites the file (VACUUM).
- **Reports load nothing from the web.** The HTML report sets
  `Content-Security-Policy: default-src 'none'`, so it cannot fetch or send anything. Its single
  inline script (filters, sorting, overview bars) is allowed only by its SHA-256 hash, the email
  data sits in an inert JSON block, and subjects and senders are inserted as text, never as
  markup, so a crafted email cannot run code in the report.
- **Injection-safe output.** CSV cells that could act as spreadsheet formulas are neutralised,
  and control characters are stripped before anything is printed to the terminal.
- **Errors do not leak content.** Error messages name the problem type, never the email text.
  The model worker never echoes a request.
- **No deletion.** The code contains no delete, trash or expunge operation. A test fails if one
  is added to a connector.

## Limits you should know

- The guard protects the Python processes of Mail Triage. A **llama.cpp or Ollama server** is
  a separate program you start yourself. Start it with `--host 127.0.0.1` so that only this
  computer can use it. It downloads its model the first time.
- **Apple Mail** itself keeps syncing with your mail provider as usual. Mail Triage does not
  change that. **Outlook** also syncs independently. Neither native connector disables the
  Python guard. The guard is not a firewall/sandbox: it does not control osascript, COM,
  browser automation, arbitrary native code or other external processes.
- The terminal shows redacted senders and subjects by default (`privacy.console = "redacted"`).
  `full` shows them in clear, so be careful when sharing screenshots or logs. `counts` shows
  only numbers.
- The history contains personal data (sender addresses, subjects). Do not commit it, sync it or
  attach it anywhere. `.gitignore` excludes it.

## Outlook trust boundary

- macOS uses fixed public JXA scripts with parameters as JSON arguments, not executable
  source interpolation. Windows classic uses an isolated, time-bounded worker attached to
  the current signed-in COM profile. Optional pywin32 is never loaded on non-Windows hosts.
- Automation/COM grants the existing client's privileges, not a read-only security scope.
  Our adapters contain reads only: no opening/selecting items, writes, attachments,
  credential properties, protected database access or private service calls. Company
  authorization remains a prerequisite even for local processing.
- The client may expose incomplete transport data. Body properties plus authentic headers
  are normalized in memory; **original MIME is not downloaded**, since it may contain
  attachments. At most 12,000 body characters enter the normalized source; the existing
  parser/model further bounds its body preview. Missing authentication, unsubscribe or
  bulk evidence is never fabricated. A local provenance marker distinguishes normalization.
  Missing headers cap model junk to review and cannot be filled by junk sender memory.
  Explicit rules and teachings still intentionally override model policy.
- Client-reported rights-protected or partially downloaded messages fail clearly; this is
  not general sensitivity-label/DLP detection. Selected reads compare read
  state before/after, abort on observed changes, and never write status back. Independent
  live verification is required; asynchronous client behavior is not controlled by Python.
- `doctor --source outlook` returns counts/capability flags, not accounts, subjects or mail.
  `--probe-read` explicitly reads at most one message per selected inbox into memory,
  returning availability flags only. An installation or dictionary is not read validation.
- No session cookies, browser/Outlook tokens, OAuth credentials, mail debug dumps, screenshots
  or telemetry are collected. Graph/browser UI/IMAP/EWS connectors are **not** enabled as
  fallbacks. Approved future direct-network transports need isolated, narrowly scoped
  authorization without weakening local model restrictions.

See [OUTLOOK_CONNECTOR_DESIGN.md](OUTLOOK_CONNECTOR_DESIGN.md) for tested versus untested
status, alternative-method prerequisites and the small-sample validation checklist.

## Outlook add-in

- The add-in runs inside Outlook with the user's own access and sends message headers (when
  Outlook provides them) and body to `https://localhost:48765`. That is Mail Triage's helper,
  bound to `127.0.0.1` and running under the loopback-only network guard. No other endpoint
  receives mail content.
- The add-in code calls only Office.js read functions. Outlook's multi-select feature requires
  the `ReadWriteMailbox` add-in permission; a test asserts that the task pane calls no write
  functions. `manifest-readonly.xml` offers the least-privilege `ReadItem` alternative.
- Decisions are stored in the normal history (connector `outlook-addin`), under the same
  `store_subjects` and `retention_days` settings and the same `forget` rights. Bodies are never
  stored. The helper's log contains no subjects, addresses, bodies or message ids.
- The localhost certificate is self-signed, cannot act as a CA, is valid only for localhost,
  is trusted for SSL in the user's login keychain, and its private key is owner-only in the data
  folder. `./triage addin uninstall` removes both the certificate and the background service.
- Microsoft 365 remains the source of the mail. Classification is local. See
  [OUTLOOK_ADDIN.md](OUTLOOK_ADDIN.md).

## Your rights (GDPR art. 15-17)

You are the controller of your own mailbox data. Mail Triage gives you direct tools:
- **Access:** `./triage report --open` shows everything stored. `history.sqlite` is a plain
  SQLite file.
- **Rectification:** `./triage teach …` corrects decisions.
- **Erasure:** `./triage forget --sender`, `--older-than`, `--all`. Deleting the data folder
  removes everything, including settings and models.
- **Minimisation:** `store_subjects = false`, `retention_days`, `console = "counts"`.

If you use Mail Triage on a work mailbox, your employer's policies on processing third-party
personal data apply. Local processing makes compliance easier but does not replace those
policies.

## For developers and AI assistants

Never test with real mailboxes in shared or AI-assisted sessions. Use `tests/fixtures/demo`
(synthetic, `example.*` domains only; a test enforces this) or your own exported test data.
When you need to check a real mailbox connection, use `--console counts`.
