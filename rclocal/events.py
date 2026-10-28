"""The event log: one JSON line per thing that happened, in `.hub/events.jsonl`.

The round writes it and the status page reads it, so they don't have to run in the
same process. Event types and their fields are defined in protocol.EVENTS.
"""

from __future__ import annotations

import json
import os
import threading
import time

from .protocol import check_event


class Events:
    def __init__(self, path, echo=None):
        self.path = path
        self.echo = echo  # called with each event, e.g. to print progress in the terminal
        self.round = None
        self._lock = threading.Lock()

    def emit(self, type, **fields):
        event = check_event({"type": type, "ts": round(time.time(), 3), "round": self.round, **fields})
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        if self.echo:
            self.echo(event)
        return event

    def read(self, last=None):
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        if last:
            lines = lines[-last:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # a line cut off mid-write
        return out
