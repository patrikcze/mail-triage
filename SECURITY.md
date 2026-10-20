# Security and privacy policy

Mail Triage reads personal email. Its promises are: nothing leaves the computer while mail is
processed, no email is ever deleted, and stored history is private to the user. See
[docs/PRIVACY.md](docs/PRIVACY.md) for how this is enforced.

## Reporting a vulnerability

Please report anything that could break those promises privately, through GitHub's
**"Report a vulnerability"** button (Security tab), not in a public issue. For example:
- a way for email content to reach the network;
- data leaking into logs, reports or error messages beyond what is documented;
- a path that deletes, trashes or modifies mail;
- script or formula injection through crafted emails.

Include steps to reproduce, using **synthetic emails only**.

## Never attach real email

Do not attach real messages, `history.sqlite`, reports or screenshots with real senders or
subjects to issues, pull requests or discussions. Reproduce problems with the synthetic
fixtures in `tests/fixtures/demo` or messages you write yourself, on `example.com` / `.test`
domains.
