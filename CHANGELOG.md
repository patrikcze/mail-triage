# Changelog

## 1.1.0 — 2026-10-21

### Added

- **Outlook add-in** (`./triage addin install|status|start|stop|run|uninstall`). Triage mail in
  Outlook on the web, Outlook for Windows and new Outlook for Mac without an app registration or
  Graph consent. Batch mode handles up to 100 selected messages (Mailbox 1.15 clients), and
  auto-triage while browsing works in Outlook for Mac with full headers. Decisions go into the
  normal history and report, and messages decided earlier are skipped. A localhost-only HTTPS
  helper runs as a per-user macOS LaunchAgent; the certificate is created and trusted
  automatically. See `docs/OUTLOOK_ADDIN.md`.
- Experimental Outlook scripting connector (`--source outlook`): the public AppleScript/JXA
  interface on macOS and classic Outlook COM on Windows (`.[outlook-windows]`). New Outlook for
  Mac exposes no script-visible accounts, so it does not work there.
- Opt-in Microsoft Graph access probe `tools/graph_probe.py` (`.[graph]` extra): stages A–E,
  status-only output, host-allowlisted network, verified TLS. `docs/OUTLOOK_AUTH_INVESTIGATION.md`
  compares all Microsoft 365 access options.
- `Engine.decide_one()` for sources that already hold the message (used by the add-in);
  `scan()` is unchanged.

### Changed

- Sources without transport headers (Outlook structured sources) are never decided as junk by
  the model, and junk sender memory needs header evidence. The marker for this is protected by a
  per-process token, so received mail cannot forge it; raw mail (Apple Mail, `.eml`) is
  unaffected.
- Native identity namespaces for connectors whose RFC Message-IDs may repeat. Legacy keys and
  the database schema are unchanged, so existing Apple Mail history stays valid.
- Docs clarify that the network guard covers Mail Triage's own processes, not mail-client sync.

### Unchanged

- Apple Mail remains the default source; `setup`, `doctor`, `scan` and `start` behave as in 1.0.0.

## 1.0.0

First public release.

- **Core engine.** Decides each email once (keyed by account + Message-ID). Teachings, rules
  and sender memory decide without fetching the message. Otherwise the local Laya model
  decides, fused with header evidence; only "highly likely" junk with bulk-sender headers
  counts as junk.
- **Guards.** Receipts, orders, payments, codes, security alerts, contracts, terms changes and
  account notices are never junk. The Czech "(OS)" commercial marker counts as bulk evidence.
- **Models.** Laya MLX on Apple Silicon (English and multilingual, pinned, downloaded by
  `setup`), or any local `/v1/systemone` server such as llama.cpp
  (`ggml-org/Laya-GGUF:Q8_0`) or Ollama.
- **Mail sources.** Apple Mail (JXA, read-only) and folders of `.eml` / `.mbox` files.
- **Configuration.** One commented TOML file: categories with future destination folders,
  rules, model instructions, temperature, thresholds, learning, privacy.
- **History.** SQLite history with versioned decisions, sender statistics, teachings and a
  journal ready for the future move action.
- **Privacy.** Network guard in every process that reads mail, owner-only files,
  secure-delete, retention, `forget`, redacted console by default. The HTML report cannot load
  remote content, and CSV/terminal output is injection-safe.
- **Moving mail** is planned (`apply`) and previewed in reports. Deletion is never possible.
