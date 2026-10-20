"""Launcher: python3 triage.py <command>. Works from any folder, no installation needed."""

import sys
from pathlib import Path

if sys.version_info < (3, 11):
    raise SystemExit("Mail Triage needs Python 3.11 or newer. Install it from https://www.python.org/downloads/")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mail_triage.cli import main  # noqa: E402

raise SystemExit(main())
