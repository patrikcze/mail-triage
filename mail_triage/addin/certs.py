"""Localhost TLS certificate for the Outlook add-in (Office add-ins must be served over HTTPS).

The certificate is self-signed, not a CA (CA:FALSE, so it cannot sign other certificates), valid
only for localhost/127.0.0.1, and trusted for SSL in the user's login keychain on macOS. The private
key stays owner-only in the Mail Triage data folder and is never part of the repository.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .. import config as configmod, privacy

SUBJECT = "Mail Triage localhost"
DAYS = 397                  # Apple's limit for TLS server certificates is 398 days
RENEW_SECONDS = 30 * 86400  # renew when fewer than 30 days remain


class CertError(RuntimeError):
    pass


def folder() -> Path:
    return privacy.private_dir(privacy.private_dir(configmod.home()) / "addin-tls")


def paths() -> tuple[Path, Path]:
    base = folder()
    return base / "localhost.pem", base / "localhost-key.pem"


def _openssl() -> str:
    tool = shutil.which("openssl") or ("/usr/bin/openssl" if Path("/usr/bin/openssl").exists() else None)
    if tool is None:
        raise CertError("openssl was not found; the one-step setup currently supports macOS only")
    return tool


def create() -> Path:
    cert, key = paths()
    subprocess.run([_openssl(), "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", str(DAYS),
                    "-keyout", str(key), "-out", str(cert), "-subj", f"/CN={SUBJECT}",
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                    "-addext", "basicConstraints=critical,CA:FALSE",
                    "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
                    "-addext", "extendedKeyUsage=serverAuth"], check=True, capture_output=True)
    privacy.private_file(key)
    privacy.private_file(cert)
    return cert


def needs_renewal(cert: Path) -> bool:
    if not cert.exists():
        return True
    result = subprocess.run([_openssl(), "x509", "-in", str(cert), "-noout", "-checkend", str(RENEW_SECONDS)],
                            capture_output=True)
    return result.returncode != 0


def is_trusted() -> bool | None:
    """True/False on macOS; None where trust cannot be checked automatically."""
    if sys.platform != "darwin":
        return None
    result = subprocess.run(["security", "dump-trust-settings"], capture_output=True, text=True)
    return SUBJECT in result.stdout


def trust(cert: Path) -> None:
    """macOS asks for the user's password or Touch ID once."""
    if sys.platform != "darwin":
        raise CertError("automatic trust is macOS-only for now; see docs/OUTLOOK_ADDIN.md")
    keychain = Path.home() / "Library/Keychains/login.keychain-db"
    result = subprocess.run(["security", "add-trusted-cert", "-r", "trustRoot", "-p", "ssl",
                             "-k", str(keychain), str(cert)], capture_output=True, text=True)
    if result.returncode != 0:
        raise CertError("macOS did not trust the certificate (cancelled or blocked by policy)")


def untrust(cert: Path) -> None:
    if sys.platform != "darwin":
        return
    if cert.exists():
        subprocess.run(["security", "remove-trusted-cert", str(cert)], capture_output=True)
    subprocess.run(["security", "delete-certificate", "-c", SUBJECT], capture_output=True)


def ensure(out=print) -> Path:
    """Create, renew and trust the certificate as needed. Returns the certificate path."""
    cert, _ = paths()
    if needs_renewal(cert):
        if cert.exists():
            untrust(cert)
        out("Creating the local HTTPS certificate (localhost only)…")
        create()
    if is_trusted() is False:
        out("macOS will now ask for your password once, to trust that certificate for localhost.")
        trust(cert)
    return cert


def remove() -> None:
    cert, key = paths()
    untrust(cert)
    for path in (cert, key):
        path.unlink(missing_ok=True)
