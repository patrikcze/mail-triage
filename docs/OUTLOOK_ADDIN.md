# Outlook add-in

Mail Triage can sort mail in **Microsoft Outlook** through a small Outlook add-in. The add-in runs
inside Outlook with your normal access. It hands each message to Mail Triage **on your own
computer**, which decides **keep / review / junk** with the local Laya model. Nothing is moved or
changed in your mailbox, and no message content goes to any other service.

It needs **no IT app registration and no Microsoft Graph consent**. Your organization only has
to allow custom add-ins, which most Microsoft 365 tenants do.

| Outlook | How you use it | Transport headers | Status |
|---|---|---|---|
| **Outlook on the web** (any browser) | Select up to 100 messages → **Triage selection** → repeat | not provided by Outlook | Works |
| **Outlook for Windows** (new and classic) | Same as the web | expected | Expected to work, not yet tested |
| **New Outlook for Mac** | Tick **Auto-triage**, then go through the list with **↓** | ✅ full | Works, one message at a time |

Why the difference: Outlook for Mac does not (yet) let add-ins read the content of *selected*
messages (Office.js Mailbox 1.15), so on a Mac the add-in triages each message as it is shown.
In Outlook on the web, Outlook does not return internet headers for selected messages. Mail
Triage therefore never marks such mail as **junk**, only **review**: junk needs header evidence
(for example `List-Unsubscribe` or a failed SPF/DKIM check).

## Install (macOS)

You need Mail Triage itself (see the [README](../README.md)) and Python 3.11+.

```sh
./triage addin install
```

This one command:

1. sets up Mail Triage and the local model if that has not happened yet;
2. creates a **localhost-only HTTPS certificate**. macOS asks for your password **once** to trust
   it. Outlook only loads add-ins over HTTPS;
3. installs a **background helper** that starts automatically when you log in;
4. opens Finder at the add-in file and opens Outlook's add-in page.

Then, **once per mailbox**, add the add-in to Outlook:

1. In Outlook: **Get Add-ins** (or **Apps**) → **My add-ins** → **Custom add-ins** →
   **Add a custom add-in** → **Add from file**. In Outlook on the web you can go there
   directly: <https://aka.ms/olksideload>.
2. Choose `mail_triage/addin/static/manifest.xml` (the path is printed by the install command).
3. Accept the warning. Outlook requires the "read and modify" permission for add-ins that work
   on several selected messages. Mail Triage only reads; it never changes, moves or deletes
   mail, and an automated test checks that the add-in code calls no write functions.

Prefer the narrowest permission? Add `manifest-readonly.xml` instead. It triages only the open
message (permission `ReadItem`) and has no batch mode.

## Use

**Outlook on the web / Windows: batch**

1. Select up to **100** messages: click the first, then Shift-click the last. "Select all"
   always exceeds Outlook's limit for add-ins.
2. Open **Mail Triage** (Apps button or the **…** menu) and **pin** the pane.
3. Click **Triage selection**. Results appear in the pane.
4. Select the next messages and repeat. Messages decided before, in this or any earlier session,
   are skipped.

**Outlook for Mac: auto-triage**

1. Open a message, open **Mail Triage** and **pin** the pane.
2. Tick **Auto-triage each message I open**.
3. Go through the list with **↓**. Every message is triaged as it is shown.

Outlook may mark viewed messages as read, depending on its own reading-pane setting. That is
Outlook's behavior, not Mail Triage's. Outlook for Mac occasionally closes add-in panes, for
example on some conversation headers. Reopen the pane; already-triaged messages are skipped.

**Report:** click **Open report** in the pane, or run `./triage report --open`. Add-in decisions
are part of the normal history, together with any Apple Mail scans. Teachings, rules and sender
memory apply exactly as they do for mail-client scans.

## Manage the helper

```sh
./triage addin status      # installed? certificate trusted? helper responding?
./triage addin stop        # stop now (starts again at next login)
./triage addin start       # start / restart now
./triage addin run         # run in the foreground instead (Ctrl+C to stop); --verbose for status lines
./triage addin uninstall   # remove the helper and the certificate
```

After `uninstall`, also remove the add-in in Outlook: **My add-ins → Mail Triage → Remove**.

Updating Mail Triage (for example `git pull`) needs only `./triage addin start` to restart the
helper. Re-add the manifest in Outlook only when the release notes say the manifest changed.

## Configuration

The add-in uses the normal settings file (`config.toml` in the data folder; see
[CONFIGURATION.md](CONFIGURATION.md)): categories, rules, model, policy, `privacy.store_subjects`
and `privacy.retention_days` apply unchanged. Files the add-in adds to the data folder:

| File | Purpose |
|---|---|
| `addin-tls/localhost.pem`, `localhost-key.pem` | the localhost certificate (owner-only, never in Git) |
| `logs/addin.log` | helper log: start/stop and errors only, never message content (trimmed above 1 MB) |
| `~/Library/LaunchAgents/org.mail-triage.addin.plist` | the per-user background service definition |

The helper listens on `https://localhost:48765`. The port is fixed because it is written into
the add-in manifest.

## Privacy and security

- **Data flow:** Outlook → add-in pane (inside Outlook) → `https://localhost:48765` → Mail Triage
  → local model. The helper binds to `127.0.0.1` only and runs with Mail Triage's network guard
  (loopback only). Microsoft 365 remains the source of the mail; Outlook syncs as usual.
- **No mailbox changes:** the add-in calls only read functions (`getAllInternetHeadersAsync`,
  `body.getAsync`, `loadItemByIdAsync`/`unloadAsync`, `getSelectedItemsAsync`).
- **History** stores what mail-client scans store (sender, optional subject, decision), subject
  to `retention_days` and `triage forget`. Message bodies are never stored.
- **Logs** contain no subjects, addresses, bodies or message ids. Diagnostics report only step
  names, error codes and durations.
- **Requests** to the helper must come from the add-in's own origin with a custom header. Bodies
  are limited to 4 MB. The task pane has a strict Content-Security-Policy. Office.js needs
  `'unsafe-eval'` in Outlook on the web; Mail Triage's own scripts never evaluate strings.
- **Certificate:** self-signed, not a CA (cannot sign other certificates), valid only for
  `localhost`/`127.0.0.1`, renewed automatically before it expires, and trusted for SSL only, in
  your login keychain (not system-wide).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Pane says "Loading Office.js…" forever | `./triage addin status`; if the helper is not responding, `./triage addin start` |
| Pane is blank or reports a certificate error | `./triage addin install` again (re-trusts the certificate) |
| Mail Triage is missing from the Apps menu | Reload Outlook; with more than 100 selected messages, Outlook hides add-ins |
| "More than 100 messages are selected" | Select at most 100 (Shift-click a range) |
| Batch button says "Batch needs Outlook on the web" | You are in Outlook for Mac: use **Auto-triage** instead |
| Rows say "no headers" | Expected in Outlook on the web; junk is capped at review |
| "port 48765 is already in use" | The background helper is already running; use it, or `./triage addin stop` first |

## Windows and Linux

The add-in itself works in Outlook on the web from any operating system. The **one-step install**
(certificate trust plus background start) is currently macOS-only. On other systems, provide a
trusted localhost certificate at the paths above and run `./triage addin run` (or `triage.bat addin
run`). Automating this for Windows is planned.

## Limits

- Mail Triage cannot move messages through the add-in, so `./triage apply` works with Apple
  Mail only. Office.js has no move function (checked against the Mailbox 1.16 and preview
  requirement sets, September 2026), and the legacy Exchange tokens that add-ins once used for
  EWS are turned off in Microsoft 365. Moving needs either Microsoft Graph (`Mail.ReadWrite`
  through nested app authentication, which needs an app registration and organization
  approval) or Outlook's own *Sweep*/rules for recurring senders. The decisions and the folder
  plan are in the report, so you can build such rules from them.
- New Outlook for Mac supports Mailbox requirement sets up to 1.14, and reading *selected*
  messages needs 1.15. That is why Outlook for Mac has auto-triage instead of batch triage.
- It is not an automatic background scan of the whole mailbox; you select or browse. Fully
  automatic scanning of Microsoft 365 needs Graph (see
  [OUTLOOK_AUTH_INVESTIGATION.md](OUTLOOK_AUTH_INVESTIGATION.md)).
