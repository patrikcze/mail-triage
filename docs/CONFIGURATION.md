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

## Categories and folders

```toml
[[categories]]
name = "invoices"                       # letters, digits, _
description = "invoice or bill that I have to pay"   # the AI reads this text
action = "keep"                         # keep | review | junk
folder = "Finance/Invoices"             # future destination ("" = stay in inbox)
coarse = ""                             # personal | transactional | marketing | ""
```

- `description` is what the model chooses between, so make it concrete.
- `action = "junk"` categories are only used when the email is **highly likely** junk and
  carries bulk-mail headers. Otherwise it goes to `review` (folder `policy.review_folder`).
- `coarse` connects the category to the multilingual model, which can only tell three things
  apart. The *first* category of each coarse bucket receives that answer. Leave it `""` for
  categories only the English model should pick.
- At most 16 categories. Keep at least one `keep` and one `junk` category.

Moving into `folder` is not implemented yet. Folders must sit under `actions.allowed_root`
(default `Laya/`) or be listed in `actions.also_allowed`, and can never be Trash, Junk or
Deleted.

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

## Privacy

```toml
[privacy]
store_subjects = true       # false: history keeps senders and decisions only
console = "redacted"        # full | redacted | counts
retention_days = 365        # 0 = keep forever
```
