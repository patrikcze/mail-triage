# Contributing

Thanks for helping. A few rules keep this project safe for people's mailboxes:

1. **No real mail, ever.** Tests, fixtures, examples, issues and screenshots use synthetic
   messages on `example.*` or `.test` domains. `tests/test_privacy_safety.py` checks the
   fixtures. Use `--console counts` when you have to touch a real mailbox while developing.
2. **The core stays standard-library only** (Python 3.11+). Model runtimes run out of process
   (`backends/`), mail sources sit behind the `Connector` protocol (`connectors/`).
3. **Nothing leaves the computer.** Do not weaken `privacy.install_network_guard()`. A connector
   that must reach a mail server gets an explicit, configured allow-list. See
   [docs/EXTENDING.md](docs/EXTENDING.md).
4. **Never delete mail.** The only mutation that will ever exist is moving to a configured
   folder.
5. Run the tests before opening a pull request:

   ```sh
   python3 -m unittest discover -s tests
   ```

Architecture overview: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
