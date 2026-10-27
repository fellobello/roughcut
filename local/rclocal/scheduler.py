"""Running model calls and test runs in parallel, within what the machine can take.

At most `max_parallel_calls` model calls run at once, and at most `model_slots[model]`
(default 1) on any one model, so a 16 GB machine never asks one model for four
answers at the same time. Every call is logged as llm_start and llm_end events with
its speed, which is what the status page shows.
"""

from __future__ import annotations

import asyncio
import itertools

from .llm import LLMError
from .protocol import REPLIES


class Scheduler:
    def __init__(self, llm, config, events):
        self.llm = llm
        self.models = config["models"]
        self.think = config.get("think", {})
        self.events = events
        self._all = asyncio.Semaphore(config["max_parallel_calls"])
        self._slots = config.get("model_slots", {})
        self._per_model = {}
        self._ids = itertools.count(1)

    def _model_lock(self, model):
        if model not in self._per_model:
            self._per_model[model] = asyncio.Semaphore(self._slots.get(model, 1))
        return self._per_model[model]

    async def ask(self, role, system, user, label):
        """One model call for a role. The reply schema comes from protocol.REPLIES[role]."""
        model = self.models[role]
        schema = REPLIES[role]
        async with self._model_lock(model), self._all:
            call = next(self._ids)
            self.events.emit("llm_start", call=call, role=role, model=model, label=label)
            try:
                reply, stats = await asyncio.to_thread(
                    self.llm.chat_json, model, system, user, schema, self.think.get(role)
                )
            except LLMError as e:
                self.events.emit("llm_error", call=call, role=role, model=model, label=label, error=str(e))
                raise
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
            )
            return reply
