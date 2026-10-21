# Outlook read-only connector decision

**The scripting connector described here has not passed a live message-read test**, because new
Outlook for Mac exposes no script-visible accounts. For Outlook, use the
[Outlook add-in](OUTLOOK_ADDIN.md); see [OUTLOOK_AUTH_INVESTIGATION.md](OUTLOOK_AUTH_INVESTIGATION.md)
for all access options.

## Decision and evidence

Prefer the public local scripting interface on macOS, conditional on runtime capabilities.
Implement it as an experimental, read-only adapter, not as a claim of modern Outlook support.
Do not fall back automatically to Graph, browser automation, IMAP, EWS, databases or tokens.

With new Outlook for Mac (2026), `/usr/bin/sdef /Applications/Microsoft\ Outlook.app` succeeds
and the dictionary declares:

- Exchange, IMAP and POP accounts, each with an `inbox` and `root folder`.
- Folder/message integer `id`, message `sender`, `subject`, `time received`.
- `headers`, `plain text content`, `content`, `has html`, `source`, `is read`,
  `is partially downloaded`, and `is rights protected`.
- A generic `move` command (not used or implemented).

A fixed JXA probe read the version and enumerated account collections, returning **zero
accounts of all three kinds and zero inboxes**. This is a blocked mailbox integration,
not a successful empty-mailbox scan. No body/header fetch, ID stability, read-state behavior,
shared mailbox access or future move was verified. The dictionary exposes no reliable active
classic/modern mode property; **mode is unknown**, not inferred from version or empty results.
Installation and dictionary presence do not establish functionality.

Microsoft's current Mac support documentation describes the modern/legacy split and the
October 2026 legacy Exchange Online transition. Switching to legacy is **not** recommended as
a workaround for corporate restrictions or service retirement.

## Verification performed

With synthetic data on macOS (Python 3.14, arm64):

| Check | Actual result |
| --- | --- |
| `python3 -m unittest discover -s tests -q` | **111 passed**, including synthetic JXA execution with the application bridge replaced, mocked COM, CLI, history, privacy and existing regression tests |
| `python3 -m compileall -q mail_triage` | Passed |
| Python 3.11 grammar parsing | Passed; not a Python 3.11 runtime test |
| `git diff --check` | Passed |
| `python3 -m pip wheel --no-deps --no-build-isolation` in a temporary source copy/output directory | Passed; all four Outlook modules and Windows optional dependency metadata included; temporary files removed |
| Synthetic Outlook connector through installed local Laya, network guard active | Two messages classified by the real local models, both review; no real mailbox involved |
| `./triage --home <temporary-home> doctor --source outlook` | Model ready; Outlook dictionary accessible; mailbox capability **BLOCKED**, zero script-visible accounts; expected exit status 1 |

No Outlook message, live native-ID stability, read-state effects on real mail, large-mailbox
wall-clock performance or Windows COM installation was validated. Existing Apple Mail code
is unchanged; its synthetic pipeline regression tests passed, not a new live Apple Mail test.

## Decision matrix

All local automation has the user's existing client privileges; read-only is enforced by our
code, not by a read-only OS permission. Company authorization is still required.

| Method / variants | Platforms | Permission / policy prerequisite | Stability and read capabilities | Future move potential | Privacy / performance | Verification / decision |
| --- | --- | --- | --- | --- | --- | --- |
| Public Outlook scripting; legacy or modern only where runtime works | macOS | Signed-in Outlook, IT approval, terminal Automation → Outlook | Installed dictionary verified; metadata arrays and selected header/body getters implemented; modern runtime coverage unknown | Dictionary declares move; unverified, not implemented | Local Apple Events; client sync independent; bulk metadata, selected body reads | Experimental adapter; new Outlook for Mac: no script-visible accounts |
| Outlook Object Model / COM, **classic only** | Windows | Running signed-in classic profile; optional pywin32; IT/Object Model Guard approval | Documented Store/GetTable/GetItemFromID APIs; headers and structured body, not original MIME | Documented Move; no writes implemented | Local COM; GetArray pages, selected body reads; client sync independent | Mock-tested implementation; no Windows host available |
| Delegated Graph, approved public client | Mac, Windows, Web account | Organization-approved app registration/client ID, delegated Mail.Read consent, Conditional Access | Official paginated inbox API, MIME `$value`; no assumption of consent | Separate future authorization; no Mail.ReadWrite today | Direct cloud network boundary, must remain isolated from model processing | **BLOCKED**, not implemented |
| Office.js approved Outlook add-in | Mac, Windows (including new), Web subject to requirement sets | Add-in deployment approval; manifest/client compatibility; possibly additional OAuth | ReadItem covers current item, not arbitrary whole-inbox enumeration; selected-item APIs are not mailbox scans | Broader manifest/API permissions, separate approval | Hosted code/runtime trust; bounded event lifetime, unsuitable for unattended full scans alone | Evaluated, not implemented |
| Playwright + Edge authorized UI only | Mac, Windows | Explicit user and IT approval; separate disposable profile; normal interactive MFA | Browser automation documented; Outlook DOM not a stable mailbox API; opening items may mark read | UI mutations too brittle; not proposed | No token/cookie extraction; UI-only; virtualized lists and per-message navigation costly | Conditional last resort, disabled/not implemented |
| OAuth IMAP | Provider dependent | Enabled protocol, approved Entra app/consent and mailbox policy | Official protocol; PEEK required to avoid Seen changes; UIDVALIDITY-aware identity | Separate approval and safe move protocol | Direct network; batched header reads possible | Not implemented; not a Graph-consent bypass |
| EWS | Exchange deployment dependent | Enabled service and approved OAuth/application permissions | Exchange Online deprecation/disablement from October 2026; on-prem lifecycle differs | Technically available but no new integration chosen | Direct network; broad mailbox API | Not selected; not an authorization workaround |

## Implementation boundaries

- `--source applemail` remains the default. `--folder` remains the export source.
  `--source outlook` dispatches by OS; new Outlook for Windows is not supported by COM.
- The existing Connector and Mover protocols remain separate. No adapter implements Mover.
- macOS discovers only the three public account collections and their own inboxes by default.
  Shared/delegated accounts not exposed there are not searched or assumed supported.
  Explicit folder paths resolve beneath that account's root; `INBOX` is the special inbox.
- Metadata listing uses column reads, not per-message bodies or transport headers.
  Outlook has no separate RFC Message-ID property in this dictionary; Mac listings leave it
  empty. The real header, when present, is preserved at fetch time.
- Fetch only requested IDs in batches of ten, scoped to the selected folder. Read plain/HTML
  body properties and transport headers; never `source`, attachments, UI selection or opening.
  Original `source` is declared but not fetched because it can include attachment payloads.
  Client-reported rights-protected/partially downloaded items fail clearly instead of
  silently classifying a stub; this is not general sensitivity-label/DLP detection.
- Compare read state before/after selected reads; abort on change, never write a status back.
  This detects observed changes, not a guarantee against asynchronous client behavior.
- Normalize in memory with `email`: keep authentic non-content headers, rebuild only MIME
  content headers, add missing From/Subject/Message-ID from actual properties. Do not invent a
  sent Date from a received timestamp, authentication, unsubscribe or bulk evidence.
  `X-Mail-Triage-Source-Provenance` is a local normalization marker, not a transport header.
  Without transport headers, model junk is capped to review even for `(OS)` subjects.
  Junk sender-memory shortcuts require known header availability; explicit rules/teachings
  remain deliberate user overrides.
  The normalized body preview is at most 12,000 characters; existing parser/model bounds
  apply afterward. Native getters may materialize a longer body transiently in memory.
  Normalized MIME is not byte-for-byte original and must not be used to verify an original
  DKIM signature. No new SPF/DKIM/DMARC verification is performed or claimed.
- All enumeration completes or raises; mismatched columns, duplicate native IDs, changed
  snapshots, timeouts, permission errors and ambiguous account selectors are not empty success.
  A disappeared requested message alone is absent from the result, per Connector contract.
- No scan window: list all metadata. `--limit` bounds decisions/body fetches, not enumeration.
  Mac metadata is O(N) memory and bulk Apple Events; selected reads use several local events
  per item. Windows uses GetTable/GetArray pages of 256. Neither is an atomic mailbox snapshot;
  concurrent changes may require retry. Wall-clock performance is unverified.
  Windows table string limits require a selective full-subject metadata read, conservatively
  when its UTF-8 representation reaches 255 bytes; potentially truncated RFC IDs are unknown in listing
  and can be read from the real header/property at fetch time.

## Identity and history

Legacy Apple Mail/export keys remain byte-for-byte unchanged; no schema migration is needed.
Outlook supplies an optional, platform-neutral `MessageInfo.identity_scope` incorporating
adapter and account/store identity. Scoped keys include account, mailbox and native ID,
**not RFC Message-ID**, so duplicate/missing RFC IDs do not collapse different items.
Duplicate native IDs in a listing are an error. A changed native ID, renamed account/folder,
profile rebuild or moved item may be scanned again, rather than reusing another item's decision.

Mac local IDs and Windows StoreID/EntryID are not interchangeable. The same mailbox scanned
on both platforms deliberately has separate history. Cross-platform deduplication, reliable
move reconciliation and shared-store aliases require future verified mappings; RFC Message-ID
alone cannot safely solve them. Existing legacy cross-connector RFC-ID behavior is unchanged.

## Privacy, diagnostics and safe validation

`doctor --source outlook` is a counts-only capability probe, independent of model readiness.
It reports OS, adapter/client version, script-visible account count and bounded metadata
access. It does not declare read readiness from installation alone. `--probe-read` explicitly
opts into reading at most one message per discovered inbox into memory, reporting only
availability and unchanged read state; no body, headers, subjects or account names are printed.
An empty inbox cannot validate body access.

On an IT-approved machine, run these **outside an AI-assisted/shared session**:

```sh
./triage doctor --source outlook
./triage doctor --source outlook --probe-read --account USER
./triage scan --source outlook --account USER --limit 2 --console counts --no-report
./triage scan --source outlook --account USER --limit 20 --console counts --no-report
```

Use a synthetic/test inbox first. Independently compare unread counts, flags, folder contents
and selected message read states before/after; confirm no message is opened. Check a restart
skips known items, multiple accounts remain isolated, Unicode/HTML parse correctly, missing
IDs are safe, and permissions/timeouts do not leak data. An independently configured local
model is required for scans. Counts-only mode still writes configured history metadata;
use `privacy.store_subjects = false` and approved retention before a corporate scan.

macOS: allow the hosting Terminal/iTerm/Python application under System Settings → Privacy &
Security → Automation → Microsoft Outlook. Endpoint management can block it; ask IT, do not
change managed preferences. A zero-account result means no usable scripting access, not
permission to inspect Outlook databases or browser sessions.

Windows: install Python 3.11+, optional `pip install '.[outlook-windows]'`, start **classic**
Outlook with the intended signed-in profile, and run the same doctor commands. Complete the
above validation on Windows before claiming support. Object Model Guard prompts must be
approved normally; never disable protection. COM runs in a bounded subprocess so a hung
client can time out without exposing COM exception descriptions.

The Python loopback guard remains installed for every mailbox/model path. It does not
control Outlook, osascript, COM, Edge or other external processes. Outlook may synchronize
normally. No credentials, tokens, cache files, internal APIs, telemetry, message dumps or
body persistence are introduced. A future cloud connector needs an isolated, narrowly scoped
transport authorization design, not a global socket-guard bypass.

## Unimplemented alternatives and future mutation

Graph needs an approved **own** public client and interactive authorization-code + PKCE
(MSAL recommended). Delegated `Mail.Read` is the intended scope for body/header reads;
`Mail.ReadBasic` is insufficient. Delegated Mail.Read does not inherently require admin
consent, but tenant consent policies, app registration restrictions and Conditional Access
can require IT approval or block it. Shared mailboxes need an explicit authorized design.
No token collection or another app's identity/session reuse is acceptable.

Office.js ReadItem is item-context access. Multi-select starts at Mailbox requirement set
1.13; loading selected items has additional version requirements (e.g. 1.15) and manifest/
platform differences. Body and internet-header APIs have their own minimum sets (body 1.1,
getAllInternetHeadersAsync 1.8). Validate exact clients with isSetSupported; don't assume that
selection or event activation permits arbitrary inbox enumeration. Event-based activation
is event-bound and time-limited, not a permanent mailbox daemon. Add-in deployment/hosting
and any Graph permissions require separate organizational approval.

Playwright officially supports Edge, not a stable Outlook mailbox extraction contract.
Even an approved UI-only implementation would need separate-profile interactive auth,
no stored auth exports/traces/screenshots of mail, no private HTTP interception, safe DOM
failure handling and verified read-state behavior. Virtualization, localization, UI changes
and read-on-selection make bulk read-only scans impractical without further validation.

IMAP needs OAuth registration/consent, the intended delegated scope
`https://outlook.office.com/IMAP.AccessAsUser.All`, and enabled mailbox protocol settings.
That scope is not itself a read-only permission; an adapter must enforce PEEK/read-only
operations and obtain appropriate corporate approval. EWS is not
chosen for a new Exchange Online integration amid its retirement; consult the tenant's
current disablement schedule. Neither avoids corporate authorization.

Future moves must use Mover, destination validation, explicit authorization, default dry-run,
journal-before-execute, identity re-resolution, idempotent retry, visible failures and undo
where the provider supports it. Do not acquire write permissions or add write calls now.

## Authoritative references

Read on 2026-10-21; documentation describes APIs, not this connector's live compatibility.

- [Outlook for Mac and legacy/modern behavior](https://support.microsoft.com/en-us/outlook/outlook-for-mac)
- [Legacy Mac support and Exchange Online transition](https://support.microsoft.com/en-us/outlook/end-of-support-for-legacy-outlook-for-mac)
- New Outlook for Mac public `sdef` dictionary (not mailbox caches).
- [Outlook automation and Object Model Guard](https://learn.microsoft.com/en-us/office/vba/outlook/concepts/getting-started/automating-outlook-from-other-office-applications)
- [GetTable](https://learn.microsoft.com/en-us/office/vba/api/outlook.folder.gettable),
  [GetArray](https://learn.microsoft.com/en-us/office/vba/api/outlook.table.getarray),
  [GetItemFromID](https://learn.microsoft.com/en-us/office/vba/api/outlook.namespace.getitemfromid),
  [EntryID lifetime](https://learn.microsoft.com/en-us/office/vba/api/outlook.mailitem.entryid)
- [Table property truncation and date representation](https://learn.microsoft.com/en-us/office/vba/outlook/how-to/search-and-filter/factors-affecting-property-value-representation-in-the-table-and-view-classes)
- [Transport headers property](https://learn.microsoft.com/en-us/office/client-developer/outlook/mapi/pidtagtransportmessageheaders-canonical-property),
  [SaveAs formats (no EML)](https://learn.microsoft.com/en-us/office/vba/api/outlook.mailitem.saveas)
- [New Windows Outlook extensibility](https://learn.microsoft.com/en-us/office/dev/add-ins/outlook/one-outlook)
- [Graph messages and pagination](https://learn.microsoft.com/en-us/graph/api/user-list-messages),
  [permissions](https://learn.microsoft.com/en-us/graph/permissions-reference#mailread),
  [interactive PKCE](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow)
- [Office.js permissions](https://learn.microsoft.com/en-us/office/dev/add-ins/outlook/understanding-outlook-add-in-permissions),
  [multi-select](https://learn.microsoft.com/en-us/office/dev/add-ins/outlook/item-multi-select),
  [event activation](https://learn.microsoft.com/en-us/office/dev/add-ins/develop/event-based-activation)
- [Playwright with Edge](https://learn.microsoft.com/en-us/microsoft-edge/playwright/)
- [IMAP OAuth prerequisites](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth),
  [EWS deprecation](https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/deprecation-of-ews-exchange-online)
