"""Background service for the Outlook add-in server (macOS LaunchAgent, per user, no admin rights).

The agent runs ``python -m mail_triage.addin.server`` at login, restarts it if it crashes, and
writes a small content-free log to the Mail Triage data folder. It listens on 127.0.0.1 only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import ssl
import subprocess
import sys
import time
import urllib.request

from . import certs
from .server import ORIGIN
from .. import config as configmod, privacy

LABEL = "org.mail-triage.addin"
LOG_LIMIT = 1_000_000          # bytes; the log is truncated at (re)start beyond this
ROOT = Path(__file__).resolve().parents[2]


class ServiceError(RuntimeError):
    pass


def supported() -> bool:
    return sys.platform == "darwin"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path() -> Path:
    return privacy.private_dir(privacy.private_dir(configmod.home()) / "logs") / "addin.log"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def definition(config_path: Path | None = None) -> dict:
    arguments = [sys.executable, "-m", "mail_triage.addin.server"]
    if config_path is not None:
        arguments += ["--config", str(config_path)]
    return {
        "Label": LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(ROOT),
        # Pin the data folder so the service uses the same settings/history as the terminal.
        "EnvironmentVariables": {"PYTHONPATH": str(ROOT), "MAIL_TRIAGE_HOME": str(configmod.home())},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": str(log_path()),
        "StandardErrorPath": str(log_path()),
    }


def _trim_log() -> None:
    log = privacy.private_file(log_path())
    if log.stat().st_size > LOG_LIMIT:
        log.write_text("")


def loaded() -> bool:
    return _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0


def install(config_path: Path | None = None) -> None:
    if not supported():
        raise ServiceError("the background service is macOS-only for now; use  ./triage addin run")
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(definition(config_path), handle)
    os.chmod(path, 0o600)
    _trim_log()
    if loaded():
        _launchctl("bootout", f"{_domain()}/{LABEL}")
    result = _launchctl("bootstrap", _domain(), str(path))
    if result.returncode != 0:
        raise ServiceError("macOS did not start the background service (launchctl bootstrap failed)")


def start() -> None:
    if not plist_path().exists():
        raise ServiceError("not installed yet: run  ./triage addin install")
    _trim_log()
    if loaded():
        _launchctl("kickstart", "-k", f"{_domain()}/{LABEL}")
    elif _launchctl("bootstrap", _domain(), str(plist_path())).returncode != 0:
        raise ServiceError("macOS did not start the background service")


def stop() -> None:
    if loaded():
        _launchctl("bootout", f"{_domain()}/{LABEL}")


def uninstall() -> None:
    stop()
    plist_path().unlink(missing_ok=True)


def healthy(timeout: float = 2.0) -> bool:
    cert, _ = certs.paths()
    if not cert.exists():
        return False
    context = ssl.create_default_context(cafile=str(cert))
    try:
        with urllib.request.urlopen(f"{ORIGIN}/health", timeout=timeout, context=context) as response:
            return json.loads(response.read()).get("ok") is True
    except (OSError, ValueError):
        return False


def wait_until_healthy(seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if healthy():
            return True
        time.sleep(0.5)
    return False
