"""Roughcut's hub, reused for local rounds.

Tasks, questions, decisions, QUESTIONS.md and the round report all come from
scripts/hub.py unchanged. What differs: every task owns exactly one file, so tasks
work in the project folder itself instead of a git worktree each, and the hub also
holds the contract registry, the shared memory, the event log and local.json.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from hub import HUB_OWNER, Hub, HubError  # noqa: E402

from .contracts import Contracts  # noqa: E402
from .events import Events  # noqa: E402
from .memory import Memory  # noqa: E402

__all__ = ["HUB_OWNER", "HubError", "LocalHub", "DEFAULT_CONFIG"]

# Picked from benchmarks on a 16 GB Apple M5. Replies are held to a JSON schema, so
# models that fail native tool calling (the qwen coders) still work here. Two models
# fit in memory together; a third would make Ollama swap them in and out.
DEFAULT_CONFIG = {
    "host": "http://127.0.0.1:11434",
    "models": {
        "planner": "qwen3:8b",
        "tester": "qwen2.5-coder:7b",
        "builder": "qwen2.5-coder:7b",
        "reviewer": "qwen3:8b",
        "reporter": "qwen3:8b",
    },
    "think": {"planner": False, "reviewer": False, "reporter": False},
    "max_parallel_calls": 2,  # model calls in flight at once, across all models
    "model_slots": {},  # per-model limit, e.g. {"qwen3:8b": 1}; default 1 each
    "max_parallel_tasks": 3,  # tasks per wave
    "fix_tries": 3,  # write-and-test tries per build task before a review
    # Longest reply per call kind, in tokens. A model that loses track stops here instead of
    # writing until the call times out.
    "max_tokens": {"planner": 3000, "tester": 2500, "builder": 3500, "notes": 800, "reviewer": 600, "reporter": 600},
    "call_timeout": 900,  # seconds per model call
    "num_ctx": 8192,
    "temperature": 0.2,
    "keep_alive": "30m",
    "test_timeout": 60,
    "sandbox": True,  # run tests with no network and no writes outside the project (macOS)
}

ANSWER_HINT = "Answer with `roughcut-local answer <id> --text ...` (or `--keep`)"


class LocalHub(Hub):
    def __init__(self, project):
        super().__init__(project)
        self.contracts = Contracts(self)
        self.memory = Memory(self)
        self.events = Events(self.root / "events.jsonl")

    def init(self, idea, config=None):
        result = super().init(idea)
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        for key, value in (config or {}).items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
        self._write(self.root / "local.json", cfg)
        self.contracts.dir.mkdir(parents=True, exist_ok=True)
        self.memory.init()
        self._ignore(".hub/events.jsonl", "__pycache__/")
        return result

    def local_config(self):
        path = self.root / "local.json"
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        if path.exists():
            for key, value in self._read(path).items():
                if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                    cfg[key].update(value)
                else:
                    cfg[key] = value
        return cfg

    # Tasks without worktrees

    def start_tasks(self, limit=None):
        """Mark up to `limit` ready tasks in progress. Each owns one file, so there is nothing to check out."""
        with self.locked():
            errors = self.check()
            if errors:
                raise HubError("the task graph is invalid: " + "; ".join(errors))
            current = self.config()["round"]
            started = []
            for task in self.ready()[:limit]:
                task["attempts"] += 1
                task.update(status="in_progress", round=current)
                self._save_task(task)
                started.append(task)
            return started

    def retry(self, task_id, reason, use_attempt=False):
        """Send a task back to todo from any finished state, with a note saying why."""
        with self.locked():
            task = self.task(task_id)
            if task["status"] == "in_progress":
                raise HubError(f"task {task_id} is still in progress")
            task["status"] = "todo"
            if not use_attempt:
                task["attempts"] = max(0, task["attempts"] - 1)
            task["notes"].append(f"retry: {reason}")
            self._save_task(task)
            return task

    def retry_failed(self):
        """Failed tasks that still have attempts left go back to todo. Run at the start of a round."""
        limit = self.config()["max_attempts"]
        retried = []
        for task in self.tasks():
            if task["status"] == "failed" and task["attempts"] < limit:
                self.retry(task["id"], "failed last time; trying again", use_attempt=True)
                retried.append(task["id"])
        return retried

    def set_depends(self, task_id, depends_on):
        with self.locked():
            task = self.task(task_id)
            task["depends_on"] = sorted(set(depends_on))
            self._save_task(task)
            return task

    def note(self, task_id, text):
        with self.locked():
            task = self.task(task_id)
            task["notes"].append(text)
            self._save_task(task)

    def answered_since(self, round_no):
        """Questions answered after the given round was planned: what a new plan must take in."""
        return [q for q in self.questions() if q["status"] == "answered" and q.get("answered_round", q["round"]) >= round_no]

    def answer(self, question_id, text=None, keep=False):
        result = super().answer(question_id, text, keep)
        with self.locked():
            question = self.question(question_id)
            question["answered_round"] = self.config()["round"]
            self._write(self.questions_dir / f"{question_id}.json", question)
        return result

    def render(self):
        result = super().render()
        path = Path(result["path"])
        text = path.read_text(encoding="utf-8").replace("Answer in Claude Code with `/roughcut:answer`", ANSWER_HINT)
        self._write_text(path, text)
        return result
