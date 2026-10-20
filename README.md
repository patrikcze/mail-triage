# Mail Triage

**Mail Triage sorts the email already in your inbox into "for you", "to review" and "junk"**
(promotions, newsletters, marketing, spam). A small AI model decides, and it runs **entirely on
your computer**. No email, address or subject is ever sent to the internet. The program refuses
network connections while it reads mail.

It never deletes anything. Today it only **decides and shows you** what it would do, in a
report. Moving mail into folders (`Laya/Promotions`, `Laya/Newsletters`, …) is planned and
already previewed in the report.

> It is not a spam filter for your mail server. It tidies up mail that has already arrived.

## Quick start (Mac with Apple Mail)

0. **Get Mail Triage**: on GitHub click **Code › Download ZIP** and double-click the ZIP, or
   `git clone` the repository. Keep the whole folder together.
1. **Install Python** (one time): download "macOS 64-bit universal2 installer" for Python 3.13
   from [python.org](https://www.python.org/downloads/macos/) and run it.
2. **Double-click `Triage.command`** in this folder. On the first run it:
   - creates your settings file,
   - asks before downloading the AI model, about 2 GB once (only model files are downloaded,
     no email data),
   - asks macOS for permission to read Mail. Click **OK**. If you clicked "Don't Allow", turn it
     on under *System Settings › Privacy & Security › Automation › Terminal › Mail*.
3. Wait while it reads your inboxes, then your browser opens the **report**.

If macOS says the file "cannot be opened because it is from an unidentified developer",
right-click `Triage.command` › **Open** › **Open**. You only need to do this once.

The next runs are fast. Mail that was already decided is skipped, and senders it already knows
are recognised without asking the AI again.

### Using the Terminal instead

```sh
cd path/to/mail-triage
./triage start          # same as double-clicking
./triage scan           # decide new mail and write a report
./triage scan --open    # … and open the report
./triage doctor         # check that everything works
./triage help           # all commands
```

## What you'll see

The report (`report-….html`, opened in your browser) has three groups:

| Group | Meaning | Future folder |
| --- | --- | --- |
| **For you** | personal, work, receipts, bills, security alerts, contracts | stays in your inbox |
| **To review** | probably junk, but the AI is not sure enough | `Laya/Review` |
| **Junk** | "highly likely" promotions, newsletters, spam | `Laya/Promotions`, `Laya/Newsletters`, `Laya/Spam` |

Each email shows **why** it was decided: the AI's estimate, the email's headers (for example
"has an unsubscribe link"), a rule of yours, or "learned from 12 earlier emails from this
sender".

Mail Triage is careful by design:
- Something only counts as junk when the AI is *highly likely* right **and** the email carries
  the headers that bulk senders must include.
- Receipts, orders, invoices, bank statements, login codes, security alerts, contracts and
  terms changes are **never** junk.

## Correcting it

If a sender is decided wrongly, teach it once:

```sh
./triage teach sender news@shop.example.com personal     # this address is personal
./triage teach domain shop.example.com promotion         # everything from this shop is promotion
./triage teach list                                      # what you've taught
./triage teach remove 3                                  # undo teaching number 3
```

Emails already decided from that sender are re-decided on the next `./triage scan`.

## Settings

Your settings file is `config.toml` in your data folder. `./triage doctor` prints the folder;
on a Mac it is `~/Library/Application Support/MailTriage/`. The file is commented. You can:
- add your **own categories** with a destination folder, e.g. "invoices → Finance/Invoices";
- add **rules**, e.g. "everything from family.example.org is personal";
- reword the **questions** the AI answers, or make it more or less cautious (temperature,
  thresholds);
- choose what the Terminal shows (`console = "full" | "redacted" | "counts"`).

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for examples. Run `./triage doctor` after
editing; it tells you exactly what is wrong if something is.

## Which AI model?

Mail Triage uses **Laya**, a small decision model (≈ 400 M parameters) that answers yes/no
and multiple-choice questions with calibrated probabilities. You can run it two ways:

| | Laya MLX (default on Apple Silicon Macs) | llama.cpp (any computer) |
| --- | --- | --- |
| Install | `./triage setup` downloads it (asks first) | you run a local server |
| Speed | ~50 ms per email on the Mac's GPU | similar |
| Languages | English + multilingual model (Czech, German, …) | English GGUF; optional multilingual GGUF |

**Already have llama.cpp?** Start Laya and point Mail Triage to it:

```sh
llama serve -hf ggml-org/Laya-GGUF:Q8_0 --host 127.0.0.1 --port 8080
```

Then put this in `config.toml`:

```toml
[model]
backend = "systemone"
```

Optionally add a second server for non-English mail:
`llama serve -hf mys/laya-multilingual-GGUF:Q8_0 --host 127.0.0.1 --port 8081`, and set
`multilingual_url = "http://127.0.0.1:8081"` under `[model.systemone]`.

Any local server that speaks the same `/v1/systemone` API works, for example Ollama with a
decision model. **Only servers on this computer are accepted.** Mail Triage refuses any other
address.

## Privacy

- Mail is read, decided and remembered **on this computer only**. While reading mail, the
  program blocks every network connection that is not to this computer, and so does the model
  process. Only `setup` downloads the model, and `setup` never reads mail.
- Your history (`history.sqlite`) and reports keep senders, subjects and decisions, never email
  bodies. They are readable by your user account only. Subjects can be switched off
  (`store_subjects = false`). Old entries are deleted after 365 days by default.
- Erase it any time: `./triage forget --sender someone@example.com`, or `./triage forget --all`.
- No telemetry, no accounts, no cloud.

Details for GDPR: [docs/PRIVACY.md](docs/PRIVACY.md).

## Also works with exported mail

You can try it on a folder of `.eml` / `.mbox` files: exports from Outlook or Thunderbird, or
the included demo mail.

```sh
./triage scan --folder tests/fixtures/demo --console full
```

On Windows today this is the way to use it (`triage.bat scan --folder C:\path\to\export`). A
direct Outlook / Microsoft 365 connection is planned. See
[docs/EXTENDING.md](docs/EXTENDING.md).

## Troubleshooting

| Problem | What to do |
| --- | --- |
| "needs Python 3.11 or newer" | Install Python 3.13 from python.org (step 1). |
| `Triage.command` does nothing / "permission denied" | Some ZIP tools drop the "executable" mark. In Terminal, in this folder: `chmod +x triage Triage.command` |
| "Mail access not allowed" | *System Settings › Privacy & Security › Automation › Terminal* → enable **Mail**. |
| "Laya MLX is not installed yet" | Run `./triage setup`. |
| "no model server answers" | Start `llama serve …` (see above), or use `backend = "auto"` on an Apple Silicon Mac. |
| "settings: …" | The message names the line in `config.toml` to fix. `./triage doctor` re-checks. |
| It is slow the first time | Mail downloads message bodies the first time (~0.3–2 s each). Later runs only read new mail. |
| A sender is always wrong | `./triage teach sender ADDRESS CATEGORY`. |

## For developers

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how the core engine, connectors and models fit together
- [docs/EXTENDING.md](docs/EXTENDING.md): add Outlook / Microsoft Graph / IMAP, another model backend, or the move action
- Tests use synthetic mail only: `python3 -m unittest discover -s tests`
- [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md): never attach real email to issues; report privacy problems privately

Laya model weights are Apache-2.0 (convaiinnovations/laya and community GGUF/MLX builds). No
weights are included in this repository.
