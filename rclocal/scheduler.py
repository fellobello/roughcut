"""Running model calls in parallel, within what the machine can take.

At most `max_parallel_calls` model calls run at once, and at most `model_slots[model]`
(default 1) on any one model, so a 16 GB machine never asks one model for four
answers at the same time. Every call is logged as llm_start and llm_end events with
its speed, which is what the status page shows.

What each model is writing right now goes to `.hub/live.json` (Live), so the studio can
show it as it comes in, along with the last few replies that finished. `cancel()` is
checked before each call and as each reply streams in: when it says so, the call stops
and raises Interrupted.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import threading
import time

from .llm import Interrupted, LLMError
from .protocol import CODE_STOP, REPLIES

LIVE_CHARS = 4000  # the end of each reply kept for the studio
LIVE_RECENT = 8  # finished replies kept


class Live:
    """The replies being written now, and the last few finished, in one small JSON file."""

    def __init__(self, path):
        self.path = path
        self.running, self.recent = {}, []
        self._lock = threading.Lock()

    def start(self, call, role, model, label):
        with self._lock:
            self.running[call] = {"call": call, "role": role, "model": model, "label": label,
                                  "started": round(time.time(), 1), "text": "", "thinking": ""}
            self._save()

    def update(self, call, text, thinking):
        with self._lock:
            if call in self.running:
                self.running[call].update(text=text[-LIVE_CHARS:], thinking=thinking[-LIVE_CHARS:])
                self._save()

    def end(self, call, outcome):
        with self._lock:
            entry = self.running.pop(call, None)
            if entry:
                self.recent = ([{**entry, "ended": round(time.time(), 1), "outcome": outcome}] + self.recent)[:LIVE_RECENT]
            self._save()

    def _save(self):
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps({"running": list(self.running.values()), "recent": self.recent}), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass  # only the studio reads it; a missed update is fine

    @staticmethod
    def read(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"running": [], "recent": []}


class Scheduler:
    def __init__(self, llm, config, events, cancel=None):
        self.llm = llm
        self.cancel = cancel or (lambda: False)
        self.live = Live(events.path.parent / "live.json")
        self.models = config["models"]
        self.think = config.get("think", {})
        self.max_tokens = config.get("max_tokens", {})
        self.events = events
        self._all = asyncio.Semaphore(config["max_parallel_calls"])
        self._slots = config.get("model_slots", {})
        self._per_model = {}
        self._ids = itertools.count(1)

    def _model_lock(self, model):
        if model not in self._per_model:
            self._per_model[model] = asyncio.Semaphore(self._slots.get(model, 1))
        return self._per_model[model]

    async def ask(self, role, messages, label, reply=None):
        """One call on the role's model, given the conversation so far.

        With `reply` (a key of protocol.REPLIES), returns that schema's data. Without it,
        returns (code text, cut_off) for a code reply.
        """
        model = self.models[role]
        limit = self.max_tokens.get(reply or role)
        if self.cancel():
            raise Interrupted()
        async with self._model_lock(model), self._all:
            if self.cancel():  # stopped while this call waited its turn
                raise Interrupted()
            call = next(self._ids)
            self.events.emit("llm_start", call=call, role=role, model=model, label=label)
            self.live.start(call, role, model, label)

            def live(text, thinking):
                if self.cancel():
                    raise Interrupted()
                self.live.update(call, text, thinking)

            try:
                if reply:
                    result, stats = await asyncio.to_thread(
                        self.llm.chat_json, model, messages, REPLIES[reply], self.think.get(role), limit, live
                    )
                else:
                    text, stats = await asyncio.to_thread(
                        self.llm.chat_text, model, messages, self.think.get(role), limit, CODE_STOP, live
                    )
                    result = (text, stats.cut_off)
            except Interrupted:
                self.live.end(call, "stopped")
                self.events.emit("note", text=f"stopped {role} {label} partway, because you asked")
                raise
            except LLMError as e:
                self.live.end(call, "failed")
                self.events.emit("llm_error", call=call, role=role, model=model, label=label, error=str(e))
                raise
            self.live.end(call, "done")
            self.events.emit(
                "llm_end",
                call=call,
                role=role,
                model=model,
                label=label,
                seconds=round(stats.seconds, 2),
                prompt_tokens=stats.prompt_tokens,
                output_tokens=stats.output_tokens,
                tokens_per_second=stats.tokens_per_second,
                cut_off=stats.cut_off,
            )
            return result
