"""Read-only local Outlook adapters. Imports never load platform-specific libraries."""

import sys

from ..base import Connector, ConnectorError


def create_outlook() -> Connector:
    if sys.platform == "darwin":
        from .macos import MacOutlookConnector
        return MacOutlookConnector()
    if sys.platform == "win32":
        from .windows import WindowsOutlookConnector
        return WindowsOutlookConnector()
    raise ConnectorError("Outlook local automation requires macOS or Windows classic Outlook")
