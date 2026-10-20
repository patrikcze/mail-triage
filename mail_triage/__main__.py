import sys

if sys.version_info < (3, 11):
    raise SystemExit("Mail Triage needs Python 3.11 or newer. Install it from https://www.python.org/downloads/")

from .cli import main  # noqa: E402

raise SystemExit(main())
