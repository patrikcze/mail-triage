# Changelog

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
