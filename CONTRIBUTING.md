# Contributing

Thanks for helping. A few rules keep this project safe for people's mailboxes:

1. **No real mail, ever.** Tests, fixtures, examples, issues and screenshots use synthetic
   messages on `example.*` or `.test` domains. `tests/test_privacy_safety.py` checks the
   fixtures. Use `--console counts` when you have to touch a real mailbox while developing.
2. **The core stays standard-library only** (Python 3.11+). Model runtimes run out of process
   (`backends/`), mail sources sit behind the `Connector` protocol (`connectors/`).
   pywin32 is an optional Windows-only Outlook extra; imports stay inside its isolated worker.
3. **Model processing stays local.** Do not weaken `privacy.install_network_guard()`. A future
   direct-network connector needs a separately authorized, narrowly scoped transport;
   external clients already sync outside Python's guard. See
   [docs/EXTENDING.md](docs/EXTENDING.md).
4. **Never delete mail.** The only mutation that will ever exist is moving to a configured
   folder.
5. Run the tests before opening a pull request:

   ```sh
   python3 -m unittest discover -s tests
   python3 -m compileall -q mail_triage
   git diff --check
   ```

Architecture overview: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Outlook tests mock all mailbox boundaries; macOS also executes fixed JXA against synthetic
objects, never a real application. Do not add live Outlook access to CI. The
[Outlook design](docs/OUTLOOK_CONNECTOR_DESIGN.md) distinguishes mock-tested APIs from
verified live capabilities. Use its opt-in checklist on an authorized test inbox outside
AI-assisted/shared sessions; never attach real content or native exception dumps.
