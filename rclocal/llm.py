"""Talking to Ollama: data replies held to a schema, and code replies as one fenced file.

chat_json passes the reply schema as Ollama's `format`, validates what comes back and,
if it doesn't fit, gives the model one more try with the error spelled out. chat_text
asks for a file and stops at the closing fence. Both cap the reply length (max_tokens),
because a small model that loses track can otherwise write until the call times out,
and both report when a reply was cut off by that cap. FakeLLM has the same interface
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
    seconds: float = 0.0
    prompt_tokens: int = 0
    output_tokens: int = 0
    cut_off: bool = False  # the reply hit max_tokens before it ended

    @property
    def tokens_per_second(self):
        return round(self.output_tokens / self.seconds, 1) if self.seconds else 0.0


class OllamaClient:
    def __init__(self, host="http://127.0.0.1:11434", num_ctx=8192, temperature=0.2, keep_alive="30m", timeout=900):
        self.host = host.rstrip("/")
        self.options = {"num_ctx": num_ctx, "temperature": temperature}
        self.keep_alive = keep_alive
        self.timeout = timeout

    def _post(self, path, body):
        req = urllib.request.Request(
            self.host + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise LLMError(f"Ollama {path} said {e.code}: {e.read().decode(errors='replace')[:300]}") from e
        except TimeoutError as e:
            raise LLMError(f"no reply within {self.timeout}s") from e
        except (urllib.error.URLError, OSError) as e:
            raise LLMError(f"can't reach Ollama at {self.host} ({e}); is `ollama serve` running?") from e

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.host + path, timeout=5) as resp:
                return json.load(resp)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LLMError(f"can't reach Ollama at {self.host} ({e})") from e

    def _chat(self, model, messages, stats, think, max_tokens, **extra):
        options = dict(self.options)
        if max_tokens:
            options["num_predict"] = max_tokens
        if "stop" in extra:
            options["stop"] = extra.pop("stop")
        body = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": options,
            "keep_alive": self.keep_alive,
            **extra,
        }
        if think is not None:
            body["think"] = think
        start = time.monotonic()
        try:
            resp = self._post("/api/chat", body)
        except LLMError as e:
            if "repeat limit" not in str(e):
                raise
            # The model got stuck repeating itself and Ollama stopped it: once more, a little looser.
            body["options"] = {**options, "temperature": max(0.6, options.get("temperature", 0)), "repeat_penalty": 1.15}
            resp = self._post("/api/chat", body)
        stats.seconds += time.monotonic() - start
        stats.prompt_tokens += resp.get("prompt_eval_count", 0)
        stats.output_tokens += resp.get("eval_count", 0)
        stats.cut_off = resp.get("done_reason") == "length"
        return (resp.get("message") or {}).get("content", "")

    def chat_json(self, model, messages, schema, think=None, max_tokens=None):
        """Return (reply, Stats) for a conversation. Raises LLMError."""
        messages = list(messages)
        stats = Stats()
        for attempt in (1, 2):
            content = self._chat(model, messages, stats, think, max_tokens, format=schema)
            try:
                if stats.cut_off:
                    raise ProtocolError(f"the reply ran past {max_tokens} tokens and was cut off; keep it shorter")
                return validate(json.loads(content), schema), stats
            except (ValueError, ProtocolError) as e:
                if attempt == 2:
                    raise LLMError(f"{model} gave no valid reply after a retry: {e}") from e
                messages += [
                    {"role": "assistant", "content": content[:2000]},
                    {"role": "user", "content": f"That reply was not valid ({e}). Reply again with only JSON matching the schema."},
                ]

    def chat_text(self, model, messages, think=None, max_tokens=None, stop=None):
        """Return (text, Stats). Check stats.cut_off: a cut-off file is incomplete."""
        stats = Stats()
        extra = {"stop": [stop]} if stop else {}
        return self._chat(model, messages, stats, think, max_tokens, **extra), stats

    def loaded(self):
        """Models Ollama has in memory now, with their sizes (from /api/ps)."""
        return self._get("/api/ps").get("models", [])

    def installed(self):
        return [m["name"] for m in self._get("/api/tags").get("models", [])]


class FakeLLM:
    """Answers with respond(role, model, system, user, schema) -> dict or str. For tests.

    The role is the system prompt's first word ("Builder: ..." -> "builder"), user is the
    conversation's latest user message, and schema is None for code replies.
    """

    def __init__(self, respond, seconds=0.0):
        self.respond = respond
        self.seconds = seconds
        self.calls = []

    def _answer(self, model, messages, schema):
        system = messages[0]["content"]
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        self.calls.append({"model": model, "system": system, "user": user, "messages": list(messages)})
        role = system.split(":", 1)[0].strip().lower()
        if self.seconds:
            time.sleep(self.seconds)
        return self.respond(role, model, system, user, schema)

    def chat_json(self, model, messages, schema, think=None, max_tokens=None):
        return validate(self._answer(model, messages, schema), schema), Stats(self.seconds, 10, 10)

    def chat_text(self, model, messages, think=None, max_tokens=None, stop=None):
        text = self._answer(model, messages, None)
        if stop and stop in text:
            text = text[: text.index(stop)]  # like Ollama, leave the stop sequence out
        return text, Stats(self.seconds, 10, 10)

    def loaded(self):
        return []

    def installed(self):
        return []
