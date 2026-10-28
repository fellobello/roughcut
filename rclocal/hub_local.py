"""Roughcut's hub, reused for local rounds.

Tasks, questions, decisions, QUESTIONS.md and the round report all come from
plugin/scripts/hub.py unchanged. What differs: every task owns exactly one file, so tasks
work in the project folder itself instead of a git worktree each, and the hub also
holds the contract registry, the shared memory, the event log and local.json.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin" / "scripts"))

from hub import HUB_OWNER, Hub, HubError  # noqa: E402

from .contracts import Contracts  # noqa: E402
from .events import Events  # noqa: E402
from .memory import Memory  # noqa: E402
from .stacks import stack_for  # noqa: E402

__all__ = ["HUB_OWNER", "HubError", "LocalHub", "DEFAULT_CONFIG"]

# Picked from benchmarks on a 16 GB Apple M5. Replies are held to a JSON schema, so
# models that fail native tool calling (the qwen coders) still work here. Two models
# fit in memory together; a third would make Ollama swap them in and out.
DEFAULT_CONFIG = {
    "stack": "web",  # web: a browser app with a designed page; python: standard-library Python
    "host": "http://127.0.0.1:11434",
    "models": {
        "brainstormer": "qwen3:8b",
        "planner": "qwen3:8b",
        "surveyor": "qwen2.5-coder:7b",
        "tester": "qwen2.5-coder:7b",
        "builder": "qwen2.5-coder:7b",
        "designer": "qwen2.5-coder:7b",
        "user": "qwen3:8b",
        "reviewer": "qwen3:8b",
        "reporter": "qwen3:8b",
    },
    # Thinking is off: on the reviewer it took 1.5 to 4 minutes a call for the same verdict.
    "think": {"brainstormer": False, "planner": False, "user": False, "reviewer": False, "reporter": False},
    "max_parallel_calls": 2,  # model calls in flight at once, across all models
    "model_slots": {},  # per-model limit, e.g. {"qwen3:8b": 1}; default 1 each
    "max_parallel_tasks": 3,  # tasks per wave
    "agent_steps": 14,  # actions a builder agent may take per attempt
    "design_tries": 3,  # page attempts before the design task gives up
    "test_users": 3,  # test users who try each new page (0 turns them off)
    "user_fixes": 2,  # times test users may send the page back, across all rounds
    "brainstorm_turns": 4,  # most back-and-forth turns before building starts
    # Longest reply per call kind, in tokens. A model that loses track stops here instead of
    # writing until the call times out.
    "max_tokens": {
        "brainstorm": 1500, "planner": 3000, "split": 2500, "survey": 1200, "tester": 3000, "builder": 5000, "designer": 4000,
        "step": 300, "notes": 800, "page_part": 200, "user_plan": 900, "user_verdict": 700,
        "reviewer": 600, "reporter": 600,
    },
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
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        for key, value in (config or {}).items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
        stack = stack_for(cfg["stack"])
        result = super().init(idea)
        self._write(self.root / "local.json", cfg)
        self.contracts.dir.mkdir(parents=True, exist_ok=True)
        self.memory.init()
        for name, text in stack.scaffold(self.project).items():
            if not (self.project / name).exists():  # a project brought in keeps its own package.json
                self._write_text(self.project / name, text)
        self._ignore(".hub/events.jsonl", "__pycache__/", "node_modules/", ".probe.html")
        return {**result, "stack": stack.name}

    @property
    def stack(self):
        return stack_for(self.local_config()["stack"])

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

    def set_model(self, role, model):
        """Give a role another model, from the next call on."""
        from .protocol import ROLES

        if role not in ROLES or not model.strip():
            raise HubError(f"pick a role from {', '.join(ROLES)} and a model")
        with self.locked():
            path = self.root / "local.json"
            cfg = self._read(path) if path.exists() else {}
            cfg.setdefault("models", {})[role] = model.strip()
            self._write(path, cfg)
        return self.local_config()["models"]

    # Tasks without worktrees

    def start_tasks(self, limit=None, first=()):
        """Mark up to `limit` ready tasks in progress, the ids in `first` before the rest.

        Each owns one file, so there is nothing to check out.
        """
        with self.locked():
            errors = self.check()
            if errors:
                raise HubError("the task graph is invalid: " + "; ".join(errors))
            current = self.config()["round"]
            started = []
            for task in sorted(self.ready(), key=lambda t: t["id"] not in first)[:limit]:
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

    def mark_done(self, task_id, summary):
        """Mark a task done without running it: its file already exists (a project brought in)."""
        with self.locked():
            task = self.task(task_id)
            task.update(status="done", summary=summary)
            self._save_task(task)

    def restart(self, task_id, reason):
        """Give a task a fresh set of tries next round, with a note saying why."""
        with self.locked():
            task = self.task(task_id)
            task.update(status="todo", attempts=0)
            task["notes"].append(f"retry: {reason}")
            self._save_task(task)

    def set_field(self, task_id, key, value):
        """Keep a small fact on a task (like how often its code has been blamed)."""
        with self.locked():
            task = self.task(task_id)
            task[key] = value
            self._save_task(task)

    def mark_acted(self, question_id):
        """Note that a round has acted on this answer, so it isn't acted on twice."""
        with self.locked():
            question = self.question(question_id)
            question["acted"] = True
            self._write(self.questions_dir / f"{question_id}.json", question)

    def note(self, task_id, text):
        with self.locked():
            task = self.task(task_id)
            task["notes"].append(text)
            self._save_task(task)

    def ask(self, owner, text, critical=False, choice=None, why="", options=(), affects=(), meta=None):
        """Hub.ask, where options may be {label, detail} pairs (protocol.OPTION) as well as plain labels.

        Labels are what's stored and shown; details go in the question's "details", for the
        studio to show on hover. `meta` is kept on the question for whoever acts on the
        answer (an example to confirm keeps its file, call and value there).
        """
        labels, details = [], {}
        for option in options:
            if isinstance(option, dict):
                label = " ".join(option.get("label", "").split())
                if label and option.get("detail", "").strip():
                    details[label] = option["detail"].strip()
            else:
                label = " ".join(str(option).split())
            if label and label not in labels:
                labels.append(label)
        question = super().ask(owner, text, critical, choice, why, labels, affects)
        if details or meta:
            with self.locked():
                question.update(meta or {})
                if details:
                    question["details"] = details
                self._write(self.questions_dir / f"{question['id']}.json", question)
        return question

    def answered_since(self, round_no):
        """Questions answered after the given round was planned: what a new plan must take in."""
        return [q for q in self.questions() if q["status"] == "answered" and q.get("answered_round", q["round"]) >= round_no]

    def answer(self, question_id, text=None, keep=False):
        result = super().answer(question_id, text, keep)
        with self.locked():
            question = self.question(question_id)
            question["answered_round"] = self.config()["round"]
            question["acted"] = False  # a changed answer is acted on again
            self._write(self.questions_dir / f"{question_id}.json", question)
        return result

    def render(self):
        result = super().render()
        path = Path(result["path"])
        text = path.read_text(encoding="utf-8").replace("Answer in Claude Code with `/roughcut:answer`", ANSWER_HINT)
        self._write_text(path, text)
        return result
