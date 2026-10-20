"""Any local server with the /v1/systemone API: llama.cpp (`llama serve -hf ggml-org/Laya-GGUF:Q8_0`)
or Ollama. The URL must be loopback; config validation and the network guard both enforce it.
"""

from __future__ import annotations

import json
from urllib import error, request

from ..config import is_loopback_url
from .base import BackendError


class SystemOneBackend:
    def __init__(self, url: str, model: str = "", timeout: float = 60):
        if not is_loopback_url(url):
            raise BackendError(f"{url} is not on this computer; refusing to send email data there")
        self.url = url.rstrip("/") + "/v1/systemone"
        self.model = model
        self.timeout = timeout
        self.label = f"systemone {model or url}"

    def predict(self, state: dict, questions: dict) -> dict:
        payload = {"state": state, "questions": questions}
        if self.model:
            payload["model"] = self.model
        req = request.Request(self.url, data=json.dumps(payload).encode(), method="POST",
                              headers={"Content-Type": "application/json"})
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                result = json.load(response)
        except error.HTTPError as exc:
            raise BackendError(f"model server answered HTTP {exc.code}") from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise BackendError(f"cannot reach the model server at {self.url}; is `llama serve` running?") from exc
        except ValueError as exc:
            raise BackendError("model server returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise BackendError("model server returned an unexpected answer")
        return result.get("answers")

    def ping(self) -> bool:
        try:
            self.predict({"subject": "ping", "body": "ping"},
                         {"t": {"type": "noul", "instructions": "Is this a test?"}})
            return True
        except BackendError:
            return False

    def close(self) -> None:
        pass
