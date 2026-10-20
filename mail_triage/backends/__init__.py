"""Pick and start the configured model backend. Never downloads anything (that is `setup`'s job)."""

from __future__ import annotations

from ..config import Config
from .. import provision
from .base import Backend, BackendError, Router
from .mlx import MlxBackend
from .systemone import SystemOneBackend

__all__ = ["Backend", "BackendError", "Router", "create"]


def _mlx(config: Config) -> Router:
    reason = provision.mlx_supported()
    if reason:
        raise BackendError(reason)
    mlx = config.model["mlx"]
    python = provision.find_runtime()
    english = provision.find_installed(mlx["english"]) if mlx["english"] in provision.MODELS else None
    if python is None or english is None:
        raise BackendError("Laya MLX is not installed yet. Run:  ./triage setup")
    multilingual = None
    if mlx.get("multilingual"):
        path = provision.find_installed(mlx["multilingual"]) if mlx["multilingual"] in provision.MODELS else None
        if path is None:
            raise BackendError("the multilingual model is not installed. Run ./triage setup, "
                               "or set model.mlx.multilingual = \"\"")
        multilingual = MlxBackend(python, path, "laya-multilingual-mlx")
    return Router(config, MlxBackend(python, english, "laya-english-mlx"), multilingual)


def _systemone(config: Config) -> Router:
    s1 = config.model["systemone"]
    english = SystemOneBackend(s1["url"], s1.get("model", ""), s1["timeout_seconds"])
    if not english.ping():
        raise BackendError(f"no model server answers at {s1['url']}. Start one, e.g.:\n"
                           "  llama serve -hf ggml-org/Laya-GGUF:Q8_0 --host 127.0.0.1 --port 8080")
    multilingual = None
    if s1.get("multilingual_url"):
        multilingual = SystemOneBackend(s1["multilingual_url"], s1.get("multilingual_model", ""), s1["timeout_seconds"])
        if not multilingual.ping():
            raise BackendError(f"no multilingual model server answers at {s1['multilingual_url']}")
    return Router(config, english, multilingual)


def create(config: Config) -> Router:
    backend = config.model["backend"]
    if backend == "mlx":
        return _mlx(config)
    if backend == "systemone":
        return _systemone(config)
    errors = []
    for attempt in (_mlx, _systemone):
        try:
            return attempt(config)
        except BackendError as exc:
            errors.append(str(exc))
    raise BackendError("no model available:\n  - " + "\n  - ".join(errors))
