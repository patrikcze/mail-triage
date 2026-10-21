# Configuration

Settings live in `config.toml` in your data folder (`./triage doctor` shows where). `setup`
creates it as a commented copy of
[`mail_triage/default_config.toml`](../mail_triage/default_config.toml). Anything you remove
falls back to that default. **Lists replace the defaults**: if you define `[[categories]]`,
define all the categories you want.

After editing:

```sh
./triage doctor                 # validates the file and explains any mistake
./triage scan --rescan-changed  # re-decide mail that was decided with the old settings
```

Changes to categories, rules, model, instructions or policy change the settings *fingerprint*.
`--rescan-changed` re-decides exactly the emails decided under an older fingerprint. Privacy
and learning settings do not cause re-decisions.

## Outlook add-in

The add-in needs no extra settings: categories, rules, model, policy and `[privacy]` apply as
they do for scans. Manage the localhost helper with `./triage addin install|status|start|stop|run|uninstall`.
See [OUTLOOK_ADDIN.md](OUTLOOK_ADDIN.md) for the files it adds to the data folder.

## Mail source and Outlook capability checks

Source selection is a CLI option, not a new TOML section. Existing configurations need no
migration and default to Apple Mail:

```sh
./triage scan --source applemail
./triage doctor --source outlook
./triage doctor --source outlook --account USER --probe-read
./triage scan --source outlook --account USER --limit 20 --console counts --no-report
./triage scan --source outlook --account USER --mailbox Projects/Team --console counts
./triage scan --folder tests/fixtures/demo
```

`--source` and `--folder` are mutually exclusive. `--account` is repeatable and exact
(email/display name on Mac, email/store name on Windows); invalid or ambiguous selections
fail without printing identifiers. `--mailbox` requires exactly one selected account and is
a path beneath its root; default `INBOX` uses each account's own inbox, not a mailbox search.
Standalone `report --account` filters by the canonical account stored in history, not an
Outlook display-name alias; scan-generated reports resolve aliases automatically.

`--limit` limits decisions and selective body reads, not full metadata enumeration.
`--probe-read` is Outlook-only and opt-in, sampling at most one item per selected inbox with
flags/counts output. Without a sample, body/header capabilities remain unverified. Outlook
is experimental; see [the design and support status](OUTLOOK_CONNECTOR_DESIGN.md).

Before corporate use, obtain approval and consider `[privacy] store_subjects = false`,
`console = "counts"` and approved retention. `--no-report` prevents report generation, not
history metadata storage. No Graph/OAuth client IDs, credentials, browser profiles or write
permissions are configured by this implementation.

## Categories and folders

```toml
[[categories]]
name = "invoices"                       # letters, digits, _
description = "invoice or bill that I have to pay"   # the AI reads this text
action = "keep"                         # keep | review | junk
folder = "Finance/Invoices"             # where ./triage apply moves it ("" = stays where it is)
coarse = ""                             # personal | transactional | marketing | ""
```

- `description` is what the model chooses between, so make it concrete.
- `action = "junk"` categories are only used when the email is **highly likely** junk and
  carries bulk-mail headers. Otherwise it goes to `review` (folder `policy.review_folder`).
- `coarse` connects the category to the multilingual model, which can only tell three things
  apart. The *first* category of each coarse bucket receives that answer. Leave it `""` for
  categories only the English model should pick.
- At most 16 categories. Keep at least one `keep` and one `junk` category.

`./triage apply` moves mail into `folder` (see [Actions](#actions-moving-mail)). Folders must
sit under `actions.allowed_root` (default `Laya/`) or be listed in `actions.also_allowed`, and
can never be Trash, Junk or Deleted.

## Rules

Rules decide without the AI. The first matching rule wins, and teachings (`./triage teach`)
take precedence over rules.

```toml
[[rules]]
name = "family"
domain = "family.example.org"           # sender domain and its subdomains
category = "personal"

[[rules]]
name = "shop newsletter"
sender = "news@shop.example.com"        # exact address
category = "newsletter"

[[rules]]
name = "invoices by subject"
subject_regex = "^(Faktura|Invoice)\\b"
category = "invoices"

[[rules]]
name = "mailing list"
header = "List-Id: devs.example.org"    # needs the full message, so it is checked after fetching
category = "work"
```

All matchers given in one rule must match.

## Model

```toml
[model]
backend = "auto"        # auto | mlx | systemone
temperature = 1.0       # 1 = as calibrated; 1.5 = softer (more "review"); 0.7 = sharper
body_chars = 3000       # how much of the body the model reads

[model.mlx]
english = "english-mlx"
multilingual = "multilingual-mlx"       # "" = English model reads everything (reduced weight)

[model.systemone]
url = "http://127.0.0.1:8080"           # llama.cpp / Ollama on THIS computer only
model = ""                              # model name, if the server hosts several
multilingual_url = ""                   # e.g. "http://127.0.0.1:8081"
multilingual_model = ""
timeout_seconds = 60
```

## Instructions (what the AI is asked)

```toml
[instructions]
kind = "What kind of email is this?"
is_marketing = "Is this email promotional marketing, advertising, a newsletter or a sales offer?"
is_spam = "Is this email unsolicited spam or bulk marketing?"
is_phishing = "Is this email a phishing or scam attempt to steal money, credentials, or personal data?"
```

Short, concrete questions work best. Test a change on the demo mail before your inbox:
`./triage --home /tmp/mt-test scan --folder tests/fixtures/demo --console full`.

## Policy

```toml
[policy]
junk_threshold = 0.90       # "highly likely" junk → junk
review_threshold = 0.70     # "likely" junk → review
review_folder = "Laya/Review"
scam_category = "spam"      # kept only for unsigned / no-unsubscribe mail

[policy.weights]            # log-odds; positive pushes towards junk
list_unsubscribe = 1.5
commercial_marker = 2.0     # Czech "(OS)"
transactional = -4.0        # receipts, orders, codes …
notice = -4.0               # contracts, terms, account actions
# … see default_config.toml for all
```

## Learning

```toml
[learning]
enabled = true
min_samples = 5     # a sender needs this many decisions …
agreement = 0.9     # … agreeing this much, before Mail Triage trusts its history
recheck_every = 10  # still ask the AI every 10th time
```

Receipts, notices and account mail from a known sender always get the full check.

## Actions (moving mail)

```toml
[actions]
enabled = false               # true = ./triage apply may move mail (it still shows the plan and asks)
allowed_root = "Laya/"        # destinations must be under this prefix …
also_allowed = []             # … or one of these exact folders, e.g. ["Finance/Invoices"]
forbidden = ["Trash", "Deleted Messages", "Deleted Items", "Koš", "Junk", "Spam", "Bin"]
keep_in_place = ["personal", "work"]   # never moved, even if they have a folder
exchange_flat_separator = ""  # "-" = on Exchange, create top-level Laya-Newsletters if Laya/Newsletters is missing
```

Apple Mail cannot create nested folders on Exchange accounts by script: its scripting dictionary
covers POP, IMAP and iCloud accounts, and on Exchange a path becomes one folder literally named
`Laya/Newsletters`, while creating inside a parent fails. Nested folders you create yourself
(Outlook on the web works) are found and used. With `exchange_flat_separator = "-"`, a missing
folder on Exchange is created at the top level as `Laya-Newsletters` instead; journal and `undo`
use the folder actually used.

Where each category goes by default (`./triage categories` shows your own settings):

| Category | Moves to |
| --- | --- |
| personal, work | stays where it is (`keep_in_place`) |
| transactional, notification | stays where it is (no folder) |
| newsletter / promotion / spam | `Laya/Newsletters` / `Laya/Promotions` / `Laya/Spam` |
| review (unsure) | `policy.review_folder`, `Laya/Review` |

Add `"review"` to `keep_in_place` to leave mail the model is unsure about in the inbox (a good
first step). To move receipts or notifications too, give the category a folder, e.g.
`folder = "Laya/Notifications"`. With `enabled = true`, `./triage doctor` refuses folders that
break the `[actions]` limits.

Running `apply` again is safe: existing folders are reused, messages already moved are no longer
in the scanned mailbox, and a message is never moved twice unless it was re-decided since. `undo`
reverses each journaled move once. A message moves only if it is still in the mailbox where it was decided, was decided with the
current settings (otherwise run `./triage scan --rescan-changed` first), has a Message-ID
(checked again inside Mail just before the move), and was not moved before. Mail you moved back
with `./triage undo` stays where it is until it is re-decided. Moving works with Apple Mail;
`apply --account NAME --mailbox PATH` applies the decisions of a scan of that mailbox.

## Privacy

```toml
[privacy]
store_subjects = true       # false: history keeps senders and decisions only
console = "redacted"        # full | redacted | counts
retention_days = 365        # 0 = keep forever
```
