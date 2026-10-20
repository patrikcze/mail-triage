"""Laya MLX worker: load one checkpoint once, answer questions as JSON lines. Runs in the laya-mlx venv.

argv: <checkpoint>. Request {"id", "state", "questions"} → {"type": "result", "id", "answers"}.
The network guard is installed before the model library is imported.
"""

import json
import os
from pathlib import Path
import sys


protocol_out = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr
# -I drops the script directory from sys.path; privacy.py is a standalone sibling.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from privacy import install_network_guard  # noqa: E402

install_network_guard()
MAX_LINE = 4 * 1024 * 1024
# MLX keeps freed GPU buffers for reuse. Every email has a different length, so they are almost
# never reused and, unlimited, the cache grew to 20+ GB after ~100 emails while the model needs
# 0.8 GB (1.6 GB peak). A small cache keeps the worker at ~1 GB with no measurable slowdown.
CACHE_LIMIT = 256 * 1024 * 1024
CLEAR_EVERY = 100


def send(**fields):
    protocol_out.write(json.dumps(fields, allow_nan=False) + "\n")


def main():
    checkpoint = Path(sys.argv[1])
    if not checkpoint.is_absolute() or not checkpoint.is_dir():
        send(type="error", message="checkpoint must be an existing absolute folder")
        return
    try:
        import mlx.core as mx
        import laya_mlx
        mx.set_cache_limit(CACHE_LIMIT)
        agent = laya_mlx.load(checkpoint, dtype="float16", device="gpu", batch_size=16,
                              compile=False, cache_prompts=False)
    except Exception as exc:
        send(type="error", message=f"Laya load failed: {type(exc).__name__}")
        return
    send(type="ready")
    handled = 0
    while True:
        handled += 1
        if handled % CLEAR_EVERY == 0:
            mx.clear_cache()
        line = sys.stdin.buffer.readline(MAX_LINE + 1)
        if not line:
            return
        request = None
        try:
            if len(line) > MAX_LINE:
                raise ValueError("request too large")
            request = json.loads(line)
            result = agent.predict(request["state"], request["questions"])
            send(type="result", id=request["id"], answers=result["answers"])
        except Exception as exc:
            # Never echo the request: it contains email text.
            send(type="error", id=request.get("id") if isinstance(request, dict) else None,
                 message=f"prediction failed: {type(exc).__name__}")


if __name__ == "__main__":
    main()
