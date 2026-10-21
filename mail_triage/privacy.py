"""Keep email data on this computer.

- install_network_guard(): from then on this Python process can only open sockets to
  loopback addresses or Unix sockets, and can only resolve localhost names. Installed by every
  command that touches mail and by the model worker. Only `setup` (model download) runs
  without it; local source discovery follows guard installation and never reads messages.
  External clients/processes (including Outlook and osascript) are outside this socket guard
  and may synchronize normally; it is not a system firewall.
- redact(): what the terminal shows in console = "redacted" mode.
- private_dir() / private_file(): data folder 0700, files 0600 (owner only).

Standard library only and no package imports: the MLX worker loads this file on its own.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import socket


class NetworkBlocked(ConnectionError):
    """Raised when code tries to reach anything that is not this computer."""


_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
_installed = False


def is_local_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip("[]").lower()
    if host in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def _check_address(family, address) -> None:
    if family == getattr(socket, "AF_UNIX", object()):
        return
    host = address[0] if isinstance(address, tuple) and address else address
    if not is_local_host(host):
        raise NetworkBlocked(f"mail-triage blocks network access to {host!r}: email data stays on this computer")


def install_network_guard() -> None:
    """Refuse every non-loopback connection and DNS lookup in this process (idempotent)."""
    global _installed
    if _installed:
        return
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    real_sendto = socket.socket.sendto

    def connect(self, address):
        _check_address(self.family, address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _check_address(self.family, address)
        return real_connect_ex(self, address)

    def sendto(self, data, *args):
        _check_address(self.family, args[-1])
        return real_sendto(self, data, *args)

    def getaddrinfo(host, *args, **kwargs):
        if not is_local_host(host):
            raise NetworkBlocked(f"mail-triage blocks DNS lookup of {host!r}: email data stays on this computer")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
    socket.getaddrinfo = getaddrinfo
    # Libraries such as huggingface_hub honour these and never try the network.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["NO_PROXY"] = "*"
    _installed = True


def guard_installed() -> bool:
    return _installed


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def private_file(path: Path) -> Path:
    """Create (if needed) and restrict a file to its owner."""
    if not path.exists():
        path.touch(mode=0o600)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def printable(text: str) -> str:
    """Strip control characters, so a crafted subject cannot inject terminal escape codes."""
    return "".join(c if c.isprintable() else " " for c in (text or ""))


def csv_safe(value: str) -> str:
    """Neutralise spreadsheet formulas (=HYPERLINK(…) could send data out when opened in Excel)."""
    value = printable(str(value))
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def mask(text: str, keep: int = 3) -> str:
    text = text or ""
    return text[:keep] + "…" if len(text) > keep else text


def redact_sender(address: str) -> str:
    """'jan.novak@company.example' → 'jan…@company.example': enough to recognise, not to quote."""
    local, _, domain = (address or "").partition("@")
    return f"{mask(local)}@{domain}" if domain else mask(local)


def redact_subject(subject: str) -> str:
    words = (subject or "").split()
    return " ".join(mask(word, 2) for word in words[:6]) + (" …" if len(words) > 6 else "")
