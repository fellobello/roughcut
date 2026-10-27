"""Running model calls in parallel, within what the machine can take.

At most `max_parallel_calls` model calls run at once, and at most `model_slots[model]`
(default 1) on any one model, so a 16 GB machine never asks one model for four
answers at the same time. Every call is logged as llm_start and llm_end events with
its speed, which is what the status page shows.
"""

from __future__ import annotations

import asyncio
import itertools

from .llm import LLMError
from .protocol import CODE_STOP, REPLIES


class Scheduler:
    def __init__(self, llm, config, events):
        self.llm = llm
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

    async def ask(self, role, system, user, label, reply=None):
        """One call on the role's model.

        With `reply` (a key of protocol.REPLIES), returns that schema's data. Without it,
        returns (code text, cut_off) for a code reply.
        """
        model = self.models[role]
        limit = self.max_tokens.get(reply or role)
        async with self._model_lock(model), self._all:
            call = next(self._ids)
            self.events.emit("llm_start", call=call, role=role, model=model, label=label)
            try:
                if reply:
                    result, stats = await asyncio.to_thread(
                        self.llm.chat_json, model, system, user, REPLIES[reply], self.think.get(role), limit
                    )
                else:
                    text, stats = await asyncio.to_thread(
                        self.llm.chat_text, model, system, user, self.think.get(role), limit, CODE_STOP
                    )
                    result = (text, stats.cut_off)
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
                cut_off=stats.cut_off,
            )
            return result
