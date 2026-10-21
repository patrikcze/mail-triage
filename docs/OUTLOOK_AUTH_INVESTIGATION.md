# Microsoft 365 / Outlook mailbox access: options and findings

How Mail Triage can read a Microsoft 365 (Exchange Online) mailbox, what each option needs, and
what was verified. Findings are product-level; nothing here depends on one organization.

Keep four claims separate. They are often confused:

| Claim | What it proves |
|---|---|
| Outlook shows the mailbox | Nothing about programmatic access |
| Outlook has a scripting dictionary | Nothing: new Outlook for Mac exposes no scriptable accounts |
| An OAuth sign-in succeeds | Authentication only; mail access still needs granted delegated scopes |
| An authorized mailbox read succeeds | Actual access. This is the bar Mail Triage uses |

## Summary

| Option | Needs | Verified | Mail Triage status |
|---|---|---|---|
| **Outlook add-in (Office.js)** | Custom add-ins allowed (common default) | ✅ real reads on new Outlook for Mac and Outlook on the web | **Supported**, see [OUTLOOK_ADDIN.md](OUTLOOK_ADDIN.md) |
| **Microsoft Graph, delegated** | An approved public-client app registration with `Mail.ReadBasic`/`Mail.Read` granted | Sign-in verified; mail scopes need tenant approval | Probe only (`tools/graph_probe.py`); connector after a verified read |
| Apple Mail with the Microsoft 365 account | Tenant allows Apple's mail client (consent and Conditional Access) | — | Uses the existing Apple Mail connector where allowed |
| Exported `.eml` files | Nothing (drag from Outlook to a folder) | single-message export verified | `./triage scan --folder …` |
| Outlook for Mac AppleScript/JXA | Script-visible accounts | ❌ new Outlook for Mac exposes none | Experimental connector; not usable with new Outlook |
| Classic Outlook for Windows (COM) | Classic Outlook running | Not run | Experimental connector (Windows only) |
| IMAP / EWS with OAuth | App registration plus admin consent; EWS is being retired in Exchange Online | — | Not pursued |
| Outlook on the web UI automation, cookies, tokens, local cache files | — | — | Rejected: unsupported, fragile or unsafe |

## Microsoft Graph (delegated)

Graph is the only option for **automatic** background scanning of a Microsoft 365 mailbox.
It always needs an **app registration** that the organization has approved. Mail Triage never
borrows another application's client id (Outlook, Graph PowerShell, Azure CLI, Graph Explorer),
never extracts tokens or cookies, and never asks for more than it needs.

Typical blockers, and what resolves them:

| Entra / Graph signal | Meaning | Minimum change |
|---|---|---|
| `AADSTS700016` | Client id not registered in the tenant | Approved registration |
| `AADSTS50011` / `500113` | Redirect URI missing | Add `http://localhost` (and the broker URIs below) |
| `AADSTS7000218` | Public client flows disabled | Enable *Allow public client flows* |
| `AADSTS65001` / `90094` / `90095` ("Admin approval required") | User consent disabled | An administrator grants the delegated scopes, ideally for the requesting user only |
| `AADSTS53000` / `53001` / `530003` / `53003` | Conditional Access (device or client) | Compliant device / browser, or a CA decision by IT |
| Graph `403` | Delegated permission missing or blocked | Grant `Mail.ReadBasic` / `Mail.Read` |
| Graph `404 MailboxNotEnabledForRESTAPI` | Mailbox not reachable via Graph | Exchange Online mailbox / licensing |

**Authentication brokers.** MSAL Python (≥ 1.31) can use the macOS broker (Company Portal with
the Microsoft Enterprise SSO plug-in) and Windows WAM. On macOS the broker works only when the
organization's device management has **activated** the SSO extension. Otherwise `pymsalruntime`
reports *"SSO extension is inactive"* and MSAL silently falls back to the system browser
(authorization code + PKCE), which also works with MFA and Conditional Access.

### Registration request template (for IT)

| Field | Value |
|---|---|
| Name | `Mail Triage (local, read-only)` |
| Accounts | This organization only |
| Platform | Mobile and desktop. *Allow public client flows* = Yes |
| Redirect URIs | `http://localhost`, `msauth.com.msauth.unsignedapp://auth` (macOS broker), `ms-appx-web://microsoft.aad.brokerplugin/<client-id>` (Windows WAM) |
| Delegated Graph permissions | `Mail.ReadBasic`, `Mail.Read`. No write, send or application permissions, no secrets |
| Assignment | *Assignment required* = Yes, assigned users only |
| Consent | Admin-granted for the assigned users (principal-scoped grants) rather than tenant-wide |

Do not use the consent page's *"Have an admin account?"* link to approve: that grants consent
for **all** users.

### Graph probe

`tools/graph_probe.py` proves access step by step without the connector. Install its
dependencies with `pip install ".[graph]"` into a virtual environment, then run:

```sh
python tools/graph_probe.py --login-hint you@example.com                       # A: capability, no sign-in
python tools/graph_probe.py --client-id GUID --login-hint you@example.com --sign-in            # B–D
python tools/graph_probe.py --client-id GUID --login-hint you@example.com --sign-in --read-mime 1  # E
```

| Stage | Proves |
|---|---|
| A | MSAL/broker availability, tenant discovery, verified TLS reachability |
| B | Interactive sign-in (broker, else browser + PKCE) |
| C | Granted delegated scopes |
| D | `GET /me/mailFolders/inbox/messages?$top=N&$select=id,isRead` |
| E | `GET /me/messages/{id}/$value`, parsed in memory; read state compared before and after |

It prints status values only: never tokens, subjects, addresses, bodies or ids. Tokens stay in
memory, and network access is limited to the Microsoft identity and Graph hosts. Run stages
B–E in your own terminal, not in an AI-assisted or shared session.

### Connector design (built only after stage E passes)

- **Acquisition** runs in its own process, restricted to Microsoft identity/Graph hosts. It lists
  metadata first, follows `@odata.nextLink`, uses `Prefer: IdType="ImmutableId"`, fetches `$value`
  MIME only for messages that need a decision, and honours `Retry-After`.
- **Classification** stays in the existing loopback-only pipeline.
- **Tokens** use the broker cache (macOS SSO extension / Windows WAM) or a memory-only cache.
  They are never written to config, history or the repository.
- Moving mail later needs `Mail.ReadWrite` and a separate approval.

## Outlook add-in findings

- Office.js reads the open message's internet headers (`getAllInternetHeadersAsync`, Mailbox 1.8)
  and body without any Entra app or Graph consent.
- **Multi-select** (Mailbox 1.13) works on up to 100 messages. Reading their content needs
  `loadItemByIdAsync` (Mailbox 1.15): available in Outlook on the web and Outlook for Windows,
  **not** in new Outlook for Mac. Multi-select requires the add-in permission `ReadWriteMailbox`.
- In Outlook on the web, `getAllInternetHeadersAsync` does not answer for loaded (selected)
  messages, so batch triage runs without transport headers and junk is capped at review.
- On Mac, a pinned pane receives `ItemChanged` while the user browses, with full headers.
- Office.js has no move function, and legacy Exchange tokens (used for EWS calls from add-ins) are
  turned off in Microsoft 365, so add-ins cannot move mail. Categories are the only item write.
- Office.js on Mac loads `ajax.aspnetcdn.com`, and on the web it uses `eval`. Both are allowed in
  the task pane's Content-Security-Policy, which was found through CSP violation reports.

## References

- MSAL Python macOS broker: https://learn.microsoft.com/en-us/entra/msal/python/advanced/macos-broker
- MSAL Python WAM: https://learn.microsoft.com/en-us/entra/msal/python/advanced/wam
- Microsoft Enterprise SSO plug-in: https://learn.microsoft.com/en-us/entra/identity-platform/apple-sso-plugin
- Graph messages and MIME: https://learn.microsoft.com/en-us/graph/api/user-list-messages, https://learn.microsoft.com/en-us/graph/outlook-get-mime-message
- Immutable ids: https://learn.microsoft.com/en-us/graph/outlook-immutable-id
- Outlook add-in multi-select: https://learn.microsoft.com/en-us/office/dev/add-ins/outlook/item-multi-select
- Outlook requirement sets by client: https://learn.microsoft.com/en-us/javascript/api/requirement-sets/outlook/outlook-api-requirement-sets
- Legacy Exchange tokens: https://learn.microsoft.com/en-us/office/dev/add-ins/outlook/faq-nested-app-auth-outlook-legacy-tokens
