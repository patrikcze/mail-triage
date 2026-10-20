"""Laya MLX on Apple Silicon: one long-lived worker process per checkpoint (JSON lines over pipes)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import time

from .base import BackendError


WORKER = Path(__file__).resolve().parent.parent / "mlx_worker.py"
MAX_LINE = 4 * 1024 * 1024


class MlxBackend:
    def __init__(self, python: Path, checkpoint: Path, label: str, timeout: float = 120):
        self.label = label
        self.timeout = timeout
        # Minimal environment: nothing from the user's shell leaks into the model process.
        env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR") if key in os.environ}
        env.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", TRANSFORMERS_OFFLINE="1")
        self.proc = subprocess.Popen([str(python), "-I", "-u", str(WORKER), str(checkpoint)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env)
        self.buffer = b""
        self.next_id = 0
        ready = self._read()
        if ready.get("type") != "ready":
            self.close()
            raise BackendError(ready.get("message", "Laya MLX worker did not start"))

    def _read(self) -> dict:
        deadline = time.monotonic() + self.timeout
        fd = self.proc.stdout.fileno()
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise BackendError(f"Laya MLX worker did not answer within {self.timeout:g}s")
            chunk = os.read(fd, 65536)
            if not chunk:
                raise BackendError("Laya MLX worker stopped")
            self.buffer += chunk
            if len(self.buffer) > MAX_LINE:
                raise BackendError("Laya MLX worker sent an oversized answer")
        line, _, self.buffer = self.buffer.partition(b"\n")
        try:
            return json.loads(line)
        except ValueError as exc:
            raise BackendError("Laya MLX worker sent invalid JSON") from exc

    def predict(self, state: dict, questions: dict) -> dict:
        self.next_id += 1
        frame = json.dumps({"id": self.next_id, "state": state, "questions": questions}).encode() + b"\n"
        try:
            self.proc.stdin.write(frame)
            self.proc.stdin.flush()
        except OSError as exc:
            raise BackendError("Laya MLX worker stopped") from exc
        answer = self._read()
        if answer.get("id") != self.next_id:
            raise BackendError("Laya MLX worker answer out of order")
        if answer.get("type") != "result":
            raise BackendError(answer.get("message", "Laya MLX prediction failed"))
        return answer.get("answers")

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
