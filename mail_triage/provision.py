"""Install and find the Laya MLX runtime and the pinned checkpoints (Apple Silicon only).

find_installed() only looks on disk and never touches the network.
install() is used by `setup` alone: it creates a venv with laya-mlx and downloads the pinned
model files from Hugging Face. It never sees email.

Identical runtimes/checkpoints another app already installed (llmtui) are reused read-only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import subprocess
import sys

from .config import default_home, home


RUNTIME_VERSION = "0.2.0"
# alias -> (Hugging Face repo, pinned revision). Apache-2.0 MLX builds of upstream Laya.
MODELS = {
    "english-mlx": ("aac6fef/laya-mlx", "20aed815fc6acde75733882e7ec0e3f28aeb9717"),
    "multilingual-mlx": ("aac6fef/laya-multilingual-mlx", "f2b4faf51023039425946074e2cf1361d2db11d5"),
}
CHECKPOINT_FILES = ("model.safetensors", "mlx_config.json", "rl_agent_config.json",
                    "encoder/config.json", "tokenizer/tokenizer.json", "tokenizer/tokenizer_config.json")
# Other local Laya installs with byte-identical files (read-only reuse, never modified).
SHARED_STORES = (Path.home() / ".local/share/llmtui",)


class SetupError(Exception):
    pass


def mlx_supported() -> str | None:
    """None when this machine can run Laya MLX, else the reason why not."""
    if sys.platform != "darwin" or platform.machine().lower() != "arm64":
        return "Laya MLX needs an Apple Silicon Mac (use a llama.cpp server instead)"
    if int(platform.mac_ver()[0].split(".")[0] or "0") < 14:
        return "Laya MLX needs macOS 14 or later"
    return None


def complete_checkpoint(path: Path) -> bool:
    return all((path / name).is_file() and (path / name).stat().st_size > 0 for name in CHECKPOINT_FILES)


def _homes() -> list[Path]:
    """This data folder first; models installed in the default folder are shared by every profile."""
    return list(dict.fromkeys([home(), default_home()]))


def _checkpoint_candidates(alias: str) -> list[Path]:
    revision = MODELS[alias][1]
    return [h / "models" / alias / revision for h in _homes()] + [
        store / "models/laya" / alias / revision for store in SHARED_STORES]


def _runtime_candidates() -> list[Path]:
    name = f"laya-mlx-{RUNTIME_VERSION}"
    return [h / "runtime" / name / "bin/python" for h in _homes()] + [
        store / "runtimes" / name / "bin/python" for store in SHARED_STORES]


def runtime_ready(python: Path) -> bool:
    if not python.is_file():
        return False
    probe = (f"from importlib.metadata import version; assert version('laya-mlx') == '{RUNTIME_VERSION}'; "
             "import mlx.core")
    return subprocess.run([str(python), "-I", "-c", probe], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=120).returncode == 0


def find_installed(alias: str) -> Path | None:
    return next((p for p in _checkpoint_candidates(alias) if complete_checkpoint(p)), None)


def find_runtime() -> Path | None:
    return next((p for p in _runtime_candidates() if runtime_ready(p)), None)


def install(aliases: list[str], base_python: str | None = None) -> tuple[Path, dict[str, Path]]:
    """Create the runtime (if missing) and download missing checkpoints. Network allowed here only."""
    reason = mlx_supported()
    if reason:
        raise SetupError(reason)
    python = find_runtime()
    if python is None:
        python = _runtime_candidates()[0]
        base = base_python or sys.executable
        probe = subprocess.check_output(
            [base, "-I", "-c", "import json, platform, sys; print(json.dumps([list(sys.version_info[:2]), platform.machine()]))"],
            text=True, timeout=30)
        version, machine = json.loads(probe)
        if tuple(version) < (3, 11) or machine.lower() != "arm64":
            raise SetupError("creating the model runtime needs native ARM Python 3.11+ (python.org installer)")
        venv = python.parent.parent
        print(f"Installing laya-mlx {RUNTIME_VERSION} into {venv} (one time, about 300 MB)…", flush=True)
        subprocess.run([base, "-m", "venv", str(venv)], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                        f"laya-mlx=={RUNTIME_VERSION}"], check=True)
        if not runtime_ready(python):
            raise SetupError("the model runtime did not start; check macOS and Python versions")
    checkpoints = {}
    for alias in aliases:
        path = find_installed(alias)
        if path is None:
            repo, revision = MODELS[alias]
            path = _checkpoint_candidates(alias)[0]
            print(f"Downloading {repo} (pinned {revision[:8]}, about 700-850 MB)…", flush=True)
            env = {**os.environ, "HF_HUB_DISABLE_TELEMETRY": "1"}
            subprocess.run([str(python.parent / "hf"), "download", repo, "--revision", revision,
                            "--local-dir", str(path)], check=True, env=env)
            if not complete_checkpoint(path):
                raise SetupError(f"download of {alias} is incomplete; run setup again to resume")
        checkpoints[alias] = path
    return python, checkpoints
