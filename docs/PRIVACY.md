# Privacy and data protection

Mail Triage processes personal data: the emails in your inbox. This page explains what is
processed, where, what is kept, how to erase it, and how the code enforces that nothing leaves
your computer. It is written for users and for whoever assesses the tool under the GDPR.

## Summary

| Question | Answer |
| --- | --- |
| Where is mail processed? | On this computer only. The AI model runs locally. |
| Is anything sent to the internet? | No. While mail is read, network access is blocked in code. |
| What is stored? | Sender address, subject (optional), date, mailbox, decision and reasons. **Never the body.** |
| Where? | Your data folder (`./triage doctor` shows it), readable by your user account only. |
| How long? | 365 days after the email was last seen (configurable, `privacy.retention_days`). |
| How to erase? | `./triage forget --sender ADDRESS`, `--older-than DAYS`, or `--all`. |
| Telemetry or analytics? | None. |

## What happens to an email

1. **Listing.** The mail program (Apple Mail) gives sender, subject, date and Message-ID for
   each inbox message.
2. **Shortcuts.** Your rules, teachings and the sender memory may decide right away. The email
   body is never read in that case.
3. **Classification.** Otherwise the raw message is fetched into memory. Only the **subject,
   sender and the first ~3,000 characters of the body** (quoted replies and signatures removed)
   are passed to the local model.
4. **Storage.** The decision is stored. The body is discarded as soon as the decision is made.

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
- **`setup` is the only networked step.** It downloads pinned model files from Hugging Face
  and never opens mail.
- **Private files.** The data folder is mode 0700. `history.sqlite`, reports and the settings
  file are mode 0600. SQLite runs with `secure_delete`, so erased rows are overwritten, and
  `forget` rewrites the file (VACUUM).
- **Reports load nothing from the web.** The HTML report sets
  `Content-Security-Policy: default-src 'none'`.
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
  change that.
- The terminal shows redacted senders and subjects by default (`privacy.console = "redacted"`).
  `full` shows them in clear, so be careful when sharing screenshots or logs. `counts` shows
  only numbers.
- The history contains personal data (sender addresses, subjects). Do not commit it, sync it or
  attach it anywhere. `.gitignore` excludes it.

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
