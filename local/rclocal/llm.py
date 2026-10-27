"""Talking to Ollama: one call in, one schema-checked JSON reply out.

Every call passes the reply schema as Ollama's `format`, so the model can only write
JSON of that shape. The reply is validated again here; if it still doesn't fit, the
model gets one more try with the error spelled out. FakeLLM has the same interface
and answers from a function, for tests.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .protocol import ProtocolError, validate


class LLMError(RuntimeError):
    """A call that failed: Ollama unreachable, unknown model, or no valid reply after a retry."""


@dataclass
class Stats:
    seconds: float
    prompt_tokens: int
    output_tokens: int

    @property
    def tokens_per_second(self):
        return round(self.output_tokens / self.seconds, 1) if self.seconds else 0.0


class OllamaClient:
    def __init__(self, host="http://127.0.0.1:11434", num_ctx=8192, temperature=0.2, keep_alive="30m", timeout=1800):
        self.host = host.rstrip("/")
        self.options = {"num_ctx": num_ctx, "temperature": temperature}
        self.keep_alive = keep_alive
        self.timeout = timeout

    def _post(self, path, body, timeout=None):
        req = urllib.request.Request(
            self.host + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise LLMError(f"Ollama {path} said {e.code}: {e.read().decode(errors='replace')[:300]}") from e
        except (urllib.error.URLError, OSError) as e:
            raise LLMError(f"can't reach Ollama at {self.host} ({e}); is `ollama serve` running?") from e

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.host + path, timeout=5) as resp:
                return json.load(resp)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LLMError(f"can't reach Ollama at {self.host} ({e})") from e

    def chat_json(self, model, system, user, schema, think=None):
        """Return (reply, Stats). Raises LLMError."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        total = Stats(0.0, 0, 0)
        for attempt in (1, 2):
            body = {
                "model": model,
                "messages": messages,
                "format": schema,
                "stream": False,
                "options": self.options,
                "keep_alive": self.keep_alive,
            }
            if think is not None:
                body["think"] = think
            start = time.monotonic()
            resp = self._post("/api/chat", body)
            total.seconds += time.monotonic() - start
            total.prompt_tokens += resp.get("prompt_eval_count", 0)
            total.output_tokens += resp.get("eval_count", 0)
            content = (resp.get("message") or {}).get("content", "")
            try:
                return validate(json.loads(content), schema), total
            except (ValueError, ProtocolError) as e:
                if attempt == 2:
                    raise LLMError(f"{model} gave no valid reply after a retry: {e}") from e
                messages += [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": f"That reply was not valid ({e}). Reply again with only JSON matching the schema."},
                ]

    def loaded(self):
        """Models Ollama has in memory now, with their sizes (from /api/ps)."""
        return self._get("/api/ps").get("models", [])

    def installed(self):
        return [m["name"] for m in self._get("/api/tags").get("models", [])]


class FakeLLM:
    """Answers with respond(role, model, system, user, schema) -> dict. For tests."""

    def __init__(self, respond, seconds=0.0):
        self.respond = respond
        self.seconds = seconds
        self.calls = []

    def chat_json(self, model, system, user, schema, think=None):
        self.calls.append({"model": model, "system": system, "user": user})
        role = system.split(":", 1)[0].strip().lower()
        if self.seconds:
            time.sleep(self.seconds)
        return validate(self.respond(role, model, system, user, schema), schema), Stats(self.seconds, 10, 10)

    def loaded(self):
        return []

    def installed(self):
        return []
