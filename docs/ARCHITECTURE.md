# Architecture

```
                 ┌──────────────── core engine (stdlib only) ────────────────┐
 mail program    │                                                            │   model
┌────────────┐   │  list ──► dedup ──► teachings / rules ──► sender memory    │ ┌──────────────┐
│ Connector  │◄──┤    │      (store)       (rules.py)          (learning.py) │ │ Router       │
│ applemail  │   │    │                                             │        │ │  english     │
│ emlfolder  │   │    └──── fetch source ──► parse (mime.py) ──► model state ├─► │  multilingual│
│ (outlook)  │   │                │             header rules         │       │ │ MLX worker / │
│ (graph)    │   │                │                                  ▼       │ │ systemone    │
└────────────┘   │                └──────────► policy.py ◄──── answers ◄─────┤ └──────────────┘
                 │                               │                            │
                 │                               ▼                            │
                 │                   store.py (SQLite history) ──► report.py  │
                 └────────────────────────────────────────────────────────────┘
                   privacy.py: network guard · private files · redaction
```

| Module | Responsibility |
| --- | --- |
| `cli.py` | Commands, friendly errors. Installs the network guard before touching mail. |
| `config.py` | Loads `default_config.toml` plus the user's `config.toml`, validates, fingerprints. |
| `engine.py` | The pipeline. Talks only to the `Connector` and `Router` protocols and the `Store`. |
| `mime.py` | Raw RFC 822 → text, header evidence and guard signals. Builds the minimal model state. |
| `policy.py` | Questions from config. Fuses answers with evidence in log-odds, applies guards, picks category and folder. |
| `rules.py` / `learning.py` | Shortcuts that decide without fetching the message. |
| `store.py` | SQLite: messages, versioned decisions, sender stats, teachings, action journal, runs. |
| `report.py` | CSV plus self-contained HTML. |
| `connectors/` | Mail sources (`base.py` defines the protocol and the future `Mover`). |
| `addin/` | Outlook add-in: task pane (`static/`), localhost HTTPS helper (`server.py`, uses `Engine.decide_one`), certificate (`certs.py`), macOS LaunchAgent (`service.py`). Nothing in the mail-client scan path imports it. |
| `backends/` | Model backends (`base.py` defines the protocol, answer validation and language routing). |
| `provision.py` | MLX runtime and pinned checkpoint download (used by `setup` only). |
| `mlx_worker.py` | Runs in the laya-mlx venv. Loads one checkpoint and answers JSON lines. |

## Design rules

1. **Every email is decided once.** Legacy identity is sha256(account + Message-ID/native ID).
   Outlook opts into native scope + account + mailbox + native ID, avoiding duplicate RFC IDs
   without changing legacy keys or schema. Native ID changes may cause safe reclassification.
   A scan
   skips decided mail unless asked otherwise (`--rescan`, or `--rescan-changed` after a
   settings change). A teaching invalidates only the affected senders.
2. **Do the cheap thing first.** Listing metadata costs about 1 ms per message; fetching a full
   message costs 0.3–2 s in Apple Mail. Teachings, rules and sender memory run on metadata
   alone.
3. **The model never acts alone.** Junk needs "highly likely" **and** bulk-sender evidence.
   Transactional, notice and account guards always win. Unsure means review.
4. **Learning cannot reinforce itself.** Sender statistics count only model, rule and teach
   decisions, never memory reuse. Every Nth reuse is re-checked by the model.
5. **History is append-only per message.** Re-deciding adds a new decision row and marks it
   current, so earlier decisions stay inspectable.
6. **The core does not know the platform.** Apple Mail, MLX and llama.cpp are plug-ins behind
   protocols. Outlook adapters are isolated in `connectors/outlook/`; no Outlook-specific
   logic is required in the core. Unknown header availability prevents junk-memory shortcuts;
   structured-source provenance allows the existing policy to cap missing evidence.

Outlook is experimental: no live message-read version has been validated. See
[OUTLOOK_CONNECTOR_DESIGN.md](OUTLOOK_CONNECTOR_DESIGN.md). Direct-network Graph remains
unimplemented/blocked pending authorization.

## Database (schema 1)

| Table | Content |
| --- | --- |
| `messages` | key, connector, account, mailbox, native id, sender, domain, subject (optional), received, first/last seen |
| `decisions` | every decision per message: verdict, category, folder, probability, likelihood, source (`model`/`rule`/`teach`/`memory`), reasons, model, settings fingerprint, `current` flag |
| `sender_stats` | per sender address and category: number of authoritative decisions |
| `sender_memory` | reuse counter per sender (drives the periodic re-check) |
| `teachings` | user corrections by sender or domain |
| `actions` | journal for the future move feature: planned/applied/undone moves, for undo |
| `runs` | when scans ran and what they did (counts only) |
