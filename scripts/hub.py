#!/usr/bin/env python3
"""Shared state for roughcut rounds.

Agents never edit .hub/ by hand. They call this script, which keeps one JSON
file per task and per question and takes a lock for every change, so agents
running in parallel can't overwrite each other. Every command prints JSON.
"""

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows has no flock, so parallel writes aren't guarded there
    fcntl = None

TASK_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
QUESTION_ID = re.compile(r"^[a-z0-9][a-z0-9-]*\.[0-9]+$")
TASK_KINDS = ("build", "test")
FINISH_STATUSES = ("done", "blocked", "failed")
DEFAULT_MAX_ATTEMPTS = 3  # the first try plus two retries
HUB_OWNER = "hub"  # owner of questions that come from the planner or questioner, not a task
USAGE_WINDOWS = (("five_hour", "5-hour window"), ("seven_day", "Week"))


class HubError(Exception):
    """A request the hub refuses. The CLI prints it and exits with status 1."""


def check_task_id(value, what="task id"):
    if not TASK_ID.match(value):
        raise HubError(f"{what} {value!r} must be lowercase letters, digits and dashes")
    return value


def split_ids(value):
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def usage_file():
    """Where scripts/usage_statusline.py saves the latest usage reading."""
    return Path(os.environ.get("ROUGHCUT_USAGE_FILE") or Path.home() / ".claude" / "roughcut" / "usage.json")


class Hub:
    def __init__(self, project):
        self.project = Path(project).expanduser().resolve()
        self.root = self.project / ".hub"
        self.tasks_dir = self.root / "tasks"
        self.questions_dir = self.root / "questions"
        self.rounds_dir = self.root / "rounds"
        self.worktrees_dir = self.project / ".worktrees"

    # Storage

    @staticmethod
    def _read(path):
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_text(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)

    def _write(self, path, data):
        self._write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")

    @contextlib.contextmanager
    def locked(self):
        self.config()  # fail early when there is no hub
        if fcntl is None:
            yield
            return
        with open(self.root / ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def config(self):
        path = self.root / "hub.json"
        if not path.exists():
            raise HubError(f"no hub in {self.project}; run `hub.py init` there first")
        return self._read(path)

    def _git(self, *args, check=True):
        result = subprocess.run(
            ["git", "-C", str(self.project), *args], capture_output=True, text=True
        )
        if check and result.returncode != 0:
            raise HubError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result

    def _branch_exists(self, branch):
        return self._git("rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).returncode == 0

    # Setup

    def init(self, idea):
        if (self.root / "hub.json").exists():
            raise HubError(f"{self.project} already has a hub")
        if not idea.strip():
            raise HubError("the idea is empty")
        for folder in (self.tasks_dir, self.questions_dir, self.rounds_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self._write(self.root / "hub.json", {"version": 1, "round": 0, "max_attempts": DEFAULT_MAX_ATTEMPTS})
        self._write_text(self.root / "idea.md", idea.strip() + "\n")
        toplevel = self._git("rev-parse", "--show-toplevel", check=False)
        if toplevel.returncode != 0 or Path(toplevel.stdout.strip()).resolve() != self.project:
            self._git("init", "-q")  # the project gets its own repo even inside another one
        self._ignore(".worktrees/", ".hub/.lock", ".hub/.tmp-*")
        return {"project": str(self.project), "round": 0}

    def _ignore(self, *patterns):
        path = self.project / ".gitignore"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        missing = [p for p in patterns if p not in lines]
        if missing:
            self._write_text(path, "\n".join(lines + missing) + "\n")

    def next_round(self):
        with self.locked():
            cfg = self.config()
            cfg["round"] += 1
            self._write(self.root / "hub.json", cfg)
            return {"round": cfg["round"]}

    # Tasks

    def _task_path(self, task_id):
        return self.tasks_dir / f"{task_id}.json"

    def _save_task(self, task):
        self._write(self._task_path(task["id"]), task)

    def task(self, task_id):
        path = self._task_path(check_task_id(task_id))
        if not path.exists():
            raise HubError(f"no task {task_id}")
        return self._read(path)

    def tasks(self):
        return [self._read(p) for p in sorted(self.tasks_dir.glob("*.json"))]

    def add_task(self, task_id, title, kind="build", module=None, depends_on=(), description=""):
        with self.locked():
            check_task_id(task_id)
            if kind not in TASK_KINDS:
                raise HubError(f"kind must be one of: {', '.join(TASK_KINDS)}")
            if not title.strip():
                raise HubError("the task title is empty")
            if self._task_path(task_id).exists():
                raise HubError(f"task {task_id} already exists")
            for dep in depends_on:
                check_task_id(dep, "dependency")
            task = {
                "id": task_id,
                "kind": kind,
                "module": module or task_id,
                "title": title.strip(),
                "description": description.strip(),
                "depends_on": sorted(set(depends_on)),
                "status": "todo",
                "blocked_by": [],
                "attempts": 0,
                "round": self.config()["round"],
                "branch": None,
                "previous_branches": [],
                "worktree": None,
                "summary": None,
                "notes": [],
            }
            self._save_task(task)
            return task

    def check(self):
        """Return every problem in the task graph: missing dependencies and cycles."""
        tasks = {t["id"]: t for t in self.tasks()}
        errors = []
        for task in tasks.values():
            for dep in task["depends_on"]:
                if dep not in tasks:
                    errors.append(f"{task['id']} depends on missing task {dep}")
        state = {}

        def visit(task_id, path):
            state[task_id] = "visiting"
            for dep in tasks[task_id]["depends_on"]:
                if dep not in tasks:
                    continue
                if state.get(dep) == "visiting":
                    errors.append("dependency cycle: " + " -> ".join(path[path.index(dep):] + [dep]))
                elif dep not in state:
                    visit(dep, path + [dep])
            state[task_id] = "done"

        for task_id in sorted(tasks):
            if task_id not in state:
                visit(task_id, [task_id])
        return errors

    def ready(self):
        """Tasks that can start now, most useful first: the ones more tasks wait on, then builds before tests."""
        tasks = {t["id"]: t for t in self.tasks()}
        waiting_on = {task_id: set() for task_id in tasks}  # task -> every task downstream of it
        for task in tasks.values():
            stack = list(task["depends_on"])
            seen = set()
            while stack:
                dep = stack.pop()
                if dep in seen or dep not in tasks:
                    continue
                seen.add(dep)
                waiting_on[dep].add(task["id"])
                stack.extend(tasks[dep]["depends_on"])
        ready = [
            t for t in tasks.values()
            if t["status"] == "todo"
            and not t["blocked_by"]
            and all(tasks.get(dep, {}).get("status") == "done" for dep in t["depends_on"])
        ]
        return sorted(ready, key=lambda t: (-len(waiting_on[t["id"]]), t["kind"] != "build", t["id"]))

    def start_wave(self, limit=None):
        """Start every ready task: give each its own branch and worktree off the current HEAD."""
        with self.locked():
            errors = self.check()
            if errors:
                raise HubError("the task graph is invalid: " + "; ".join(errors))
            if self._git("rev-parse", "--verify", "-q", "HEAD", check=False).returncode != 0:
                raise HubError("the project has no commits yet; commit the plan before building")
            self._git("worktree", "prune")
            current_round = self.config()["round"]
            started = []
            for task in self.ready()[:limit]:
                branch = f"build/{task['id']}-{task['attempts'] + 1}"
                worktree = self.worktrees_dir / task["id"]
                if task["branch"] == branch and worktree.exists():
                    pass  # released after a stopped run: keep its worktree and any work in it
                elif task["branch"] == branch and self._branch_exists(branch):
                    self._git("worktree", "add", "-q", str(worktree), branch)  # same, but the folder is gone
                else:
                    if task["branch"] and self._branch_exists(task["branch"]):
                        task["previous_branches"].append(task["branch"])
                    if worktree.exists():
                        self._git("worktree", "remove", "--force", str(worktree))
                    self._git("worktree", "add", "-q", "-B", branch, str(worktree), "HEAD")
                task["attempts"] += 1
                task.update(status="in_progress", branch=branch, worktree=str(worktree), round=current_round)
                self._save_task(task)
                started.append(task)
            return {"tasks": started}

    def finish(self, task_id, status, summary=""):
        with self.locked():
            if status not in FINISH_STATUSES:
                raise HubError(f"status must be one of: {', '.join(FINISH_STATUSES)}")
            task = self.task(task_id)
            if task["status"] != "in_progress":
                raise HubError(f"task {task_id} is {task['status']}, not in_progress")
            if status == "done" and task["blocked_by"]:
                raise HubError(
                    f"task {task_id} has open critical questions ({', '.join(task['blocked_by'])}); "
                    "finish it as blocked"
                )
            if status == "blocked" and not task["blocked_by"]:
                raise HubError("finish as blocked only after asking a critical question without --choice")
            task["status"] = status
            task["summary"] = summary.strip() or task["summary"]
            self._save_task(task)
            return task

    def release(self):
        """Return tasks a stopped run left in progress to todo, without using up an attempt.

        Their worktrees and branches stay, so the next attempt can pick up any work in them.
        Call it only when no round is running.
        """
        with self.locked():
            released = []
            for task in self.tasks():
                if task["status"] != "in_progress":
                    continue
                task["status"] = "todo"
                task["attempts"] = max(0, task["attempts"] - 1)
                task["notes"].append("released: the run stopped before this task finished")
                self._save_task(task)
                released.append(task["id"])
            return {"released": released}

    def reopen(self, task_id, reason):
        """Send a done task back for another attempt, or to the user once it's out of attempts."""
        with self.locked():
            task = self.task(task_id)
            if task["status"] != "done":
                raise HubError(f"only done tasks can be reopened; {task_id} is {task['status']}")
            question = None
            if task["attempts"] >= self.config()["max_attempts"]:
                question = self._ask(
                    HUB_OWNER,
                    f"{task_id} still fails after {task['attempts']} attempts: {reason}. How should I proceed?",
                    critical=True,
                    affects=[task_id],
                )
                task = self.task(task_id)
                task["status"] = "blocked"
            else:
                task["status"] = "todo"
            task["notes"].append(f"reopened: {reason}")
            self._save_task(task)
            return {"task": task, "question": question}

    def cleanup(self, task_id):
        """Remove a finished task's worktree, and its branch once that branch is merged."""
        with self.locked():
            task = self.task(task_id)
            if task["status"] == "in_progress":
                raise HubError(f"task {task_id} is still in progress")
            worktree_removed = False
            if task["worktree"] and Path(task["worktree"]).exists():
                self._git("worktree", "remove", "--force", task["worktree"])
                worktree_removed = True
            branch_deleted = False
            if task["branch"] and task["status"] == "done":
                # git refuses -d for an unmerged branch, which is what we want
                branch_deleted = self._git("branch", "-d", task["branch"], check=False).returncode == 0
            task["worktree"] = None
            self._save_task(task)
            return {"task": task_id, "worktree_removed": worktree_removed, "branch_deleted": branch_deleted}

    # Questions and decisions

    def question(self, question_id):
        if not QUESTION_ID.match(question_id):
            raise HubError(f"question id {question_id!r} should look like map-ui.2")
        path = self.questions_dir / f"{question_id}.json"
        if not path.exists():
            raise HubError(f"no question {question_id}")
        return self._read(path)

    def questions(self):
        def order(path):
            owner, number = path.stem.rsplit(".", 1)
            return owner, int(number)

        return [self._read(p) for p in sorted(self.questions_dir.glob("*.json"), key=order)]

    def ask(self, owner, text, critical=False, choice=None, why="", options=(), affects=()):
        with self.locked():
            return self._ask(owner, text, critical, choice, why, options, affects)

    def _ask(self, owner, text, critical=False, choice=None, why="", options=(), affects=()):
        """Record a question (critical, no choice: blocks work) or a decision (has a choice: doesn't)."""
        if owner != HUB_OWNER:
            self.task(owner)
        for task_id in affects:
            self.task(task_id)
        if not text.strip():
            raise HubError("the question text is empty")
        if not critical and not choice:
            raise HubError("a non-critical decision needs --choice: say what you picked")
        numbers = [int(p.stem.rsplit(".", 1)[1]) for p in self.questions_dir.glob(f"{owner}.*.json")]
        question_id = f"{owner}.{max(numbers, default=0) + 1}"
        blocking = critical and not choice
        question = {
            "id": question_id,
            "task": owner,
            "text": text.strip(),
            "critical": critical,
            "choice": choice,
            "why": why.strip(),
            "options": list(options),
            "affects": sorted(set(affects)),
            "status": "open" if blocking else "decided",
            "answer": None,
            "round": self.config()["round"],
        }
        self._write(self.questions_dir / f"{question_id}.json", question)
        if blocking:
            for task_id in self._touched(question):
                task = self.task(task_id)
                task["blocked_by"] = sorted(set(task["blocked_by"]) | {question_id})
                self._save_task(task)
        return question

    @staticmethod
    def _touched(question):
        task_ids = set(question["affects"])
        if question["task"] != HUB_OWNER:
            task_ids.add(question["task"])
        return sorted(task_ids)

    def answer(self, question_id, text=None, keep=False):
        """Settle a question. Changing a decision sends the done tasks built on it back to todo."""
        with self.locked():
            question = self.question(question_id)
            if keep:
                if question["choice"] is None:
                    raise HubError(f"{question_id} is a question, not a decision; answer it with --text")
                text = question["answer"] or question["choice"]
            if not (text or "").strip():
                raise HubError("give an answer with --text, or --keep to confirm a decision")
            text = text.strip()
            built_on_guess = question["status"] != "open"
            changed = built_on_guess and text != (question["answer"] or question["choice"])
            question.update(status="answered", answer=text)
            self._write(self.questions_dir / f"{question_id}.json", question)
            unblocked, reworked = [], []
            for task_id in self._touched(question):
                task = self.task(task_id)
                if question_id in task["blocked_by"]:
                    task["blocked_by"].remove(question_id)
                    if not task["blocked_by"] and task["status"] == "blocked":
                        task["status"] = "todo"
                        unblocked.append(task_id)
                if changed and task["status"] == "done":
                    task["status"] = "todo"
                    reworked.append(task_id)
                if changed or not built_on_guess:
                    task["notes"].append(f"{question_id} answered: {text}")
                self._save_task(task)
            return {"question": question, "unblocked": unblocked, "reworked": reworked}

    def promote(self, question_id):
        """Mark a smaller decision critical, so QUESTIONS.md lists it with the big guesses."""
        with self.locked():
            question = self.question(question_id)
            if question["status"] != "decided":
                raise HubError(f"only decided decisions can be promoted; {question_id} is {question['status']}")
            question["critical"] = True
            self._write(self.questions_dir / f"{question_id}.json", question)
            return question

    def dismiss(self, question_id, reason):
        """Close a question without an answer, for example a duplicate. Unblocks its tasks."""
        with self.locked():
            question = self.question(question_id)
            question.update(status="dismissed", answer=f"dismissed: {reason.strip()}")
            self._write(self.questions_dir / f"{question_id}.json", question)
            unblocked = []
            for task_id in self._touched(question):
                task = self.task(task_id)
                if question_id in task["blocked_by"]:
                    task["blocked_by"].remove(question_id)
                    if not task["blocked_by"] and task["status"] == "blocked":
                        task["status"] = "todo"
                        unblocked.append(task_id)
                    self._save_task(task)
            return {"question": question, "unblocked": unblocked}

    # Reports

    def status(self):
        tasks = self.tasks()
        questions = self.questions()
        counts = {}
        for task in tasks:
            counts[task["status"]] = counts.get(task["status"], 0) + 1
        return {
            "project": str(self.project),
            "round": self.config()["round"],
            "tasks": counts,
            "ready": [t["id"] for t in self.ready()],
            "open_questions": [q["id"] for q in questions if q["status"] == "open"],
            "guesses_to_confirm": [q["id"] for q in questions if q["status"] == "decided" and q["critical"]],
        }

    def render(self):
        """Write QUESTIONS.md: big guesses first, then blocking questions, then this round's decisions."""
        current = self.config()["round"]
        questions = self.questions()
        guesses = [q for q in questions if q["status"] == "decided" and q["critical"]]
        blocking = [q for q in questions if q["status"] == "open"]
        decisions = [q for q in questions if q["status"] == "decided" and not q["critical"]]
        recent = [q for q in decisions if q["round"] == current]
        settled = [q for q in questions if q["status"] in ("answered", "dismissed")]

        def entry(q, picked_label):
            lines = [f"### `{q['id']}` {q['text']}", ""]
            if q["choice"]:
                lines.append(f"- **{picked_label}:** {q['choice']}")
            if q["why"]:
                lines.append(f"- **Why:** {q['why']}")
            if q["options"]:
                lines.append(f"- **Other options:** {' · '.join(q['options'])}")
            touched = self._touched(q)
            if touched:
                lines.append(f"- **{'Blocks' if q['status'] == 'open' else 'Affects'}:** {', '.join(touched)}")
            return lines + [""]

        out = [
            "# Questions",
            "",
            f"_Round {current}. Generated from `.hub/questions/` by `hub.py render`, so don't edit it by hand. "
            "Answer in Claude Code with `/roughcut:answer`._",
            "",
            f"## Big guesses to confirm ({len(guesses)})",
            "",
            "Choices that shape everything else, made so work didn't have to wait. Confirm or change each one; "
            "changing one rebuilds the parts that depend on it.",
            "",
        ]
        for q in guesses:
            out += entry(q, "Picked")
        if not guesses:
            out += ["None.", ""]
        out += [f"## Needs your answer ({len(blocking)})", ""]
        for q in blocking:
            out += entry(q, "Suggested")
        if not blocking:
            out += ["None.", ""]
        out += [f"## Smaller decisions this round ({len(recent)})", ""]
        if recent:
            out += [
                "Already built this way. Change any you disagree with; the reasons are in `.hub/questions/`.",
                "",
                "<details>",
                f"<summary>Show all {len(recent)}</summary>",
                "",
            ]
            out += [f"- `{q['id']}` {q['text']} → **{q['choice']}**" for q in recent]
            out += ["", "</details>", ""]
        else:
            out += ["None.", ""]
        if len(decisions) > len(recent):
            out += [f"_Plus {len(decisions) - len(recent)} smaller decisions from earlier rounds in `.hub/questions/`._", ""]
        if settled:
            out += [f"## Settled ({len(settled)})", ""]
            out += [f"- `{q['id']}` {q['text']} → {q['answer']}" for q in settled]
            out.append("")
        path = self.project / "QUESTIONS.md"
        self._write_text(path, "\n".join(out))
        return {
            "path": str(path),
            "guesses": len(guesses),
            "blocking": len(blocking),
            "decisions": len(recent),
            "settled": len(settled),
        }

    def report(self, notes=""):
        """Write .hub/rounds/<round>.md. Notes are kept, so a later call without notes reuses them."""
        current = self.config()["round"]
        notes_path = self.rounds_dir / f"{current}.notes.md"
        if notes.strip():
            self._write_text(notes_path, notes.strip() + "\n")
        elif notes_path.exists():
            notes = notes_path.read_text(encoding="utf-8")

        def cell(value):
            return str(value or "").replace("|", "\\|").replace("\n", " ")

        tasks = self.tasks()
        rows = ["| Task | Kind | Status | Attempts | Summary |", "| --- | --- | --- | --- | --- |"]
        for t in tasks:
            rows.append(f"| {t['id']} | {t['kind']} | {t['status']} | {t['attempts']} | {cell(t['summary'])} |")
        if not tasks:
            rows = ["No tasks yet."]
        state = self.status()
        out = [f"# Round {current} report", ""]
        if notes.strip():
            out += [notes.strip(), ""]
        out += ["## Tasks", ""] + rows + [""]
        out += [
            "## Questions",
            "",
            f"- Big guesses to confirm: {len(state['guesses_to_confirm'])}",
            f"- Questions that block work: {len(state['open_questions'])}",
            f"- Tasks ready for the next round: {len(state['ready'])}",
            "",
        ]
        out += self._usage_lines(current)
        path = self.rounds_dir / f"{current}.md"
        self._write_text(path, "\n".join(out))
        return {"path": str(path)}

    # Usage

    def latest_usage(self):
        source = usage_file()
        if not source.exists():
            return {
                "available": False,
                "reason": f"no usage readings at {source}; the README's usage section says how to turn them on",
            }
        return {"available": True, "reading": self._read(source)}

    def mark_usage(self, when):
        """Save the latest usage reading as this round's start or end. No readings isn't an error."""
        if when not in ("start", "end"):
            raise HubError("mark the usage at the round's start or end")
        latest = self.latest_usage()
        if not latest["available"]:
            return latest
        with self.locked():
            current = self.config()["round"]
            path = self.rounds_dir / f"{current}.usage.json"
            marks = self._read(path) if path.exists() else {}
            marks[when] = latest["reading"]
            self._write(path, marks)
        if when == "end" and (self.rounds_dir / f"{current}.md").exists():
            self.report()  # add the usage section to the report the questioner wrote
        return {"available": True, "round": current, when: latest["reading"]}

    def _usage_lines(self, current):
        path = self.rounds_dir / f"{current}.usage.json"
        marks = self._read(path) if path.exists() else {}
        if "start" not in marks or "end" not in marks:
            return []
        lines = []
        for key, label in USAGE_WINDOWS:
            start = (marks["start"].get("rate_limits") or {}).get(key)
            end = (marks["end"].get("rate_limits") or {}).get(key)
            if not start or not end:
                continue
            before, after = start["used_percentage"], end["used_percentage"]
            if start.get("resets_at") != end.get("resets_at"):
                lines.append(f"- {label}: {before:.0f}% at the start; it reset during the round and is now {after:.0f}%")
            else:
                lines.append(f"- {label}: {before:.0f}% → {after:.0f}% (+{after - before:.0f} points)")
        if not lines:
            return []
        return ["## Usage", ""] + lines + [
            "",
            "_Read from your status line, so it's approximate and includes anything else you did in Claude meanwhile._",
            "",
        ]


def build_parser():
    parser = argparse.ArgumentParser(prog="hub.py", description=__doc__.splitlines()[0])
    parser.add_argument("--project", default=".", help="project folder (default: the current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create .hub/ for a new idea")
    p.add_argument("--idea", help="the idea as text")
    p.add_argument("--idea-file", help="a file holding the idea, or - to read it from stdin")

    p = sub.add_parser("add-task", help="add a task to the plan")
    p.add_argument("id")
    p.add_argument("--title", required=True)
    p.add_argument("--kind", choices=TASK_KINDS, default="build")
    p.add_argument("--module", help="the module this task belongs to (default: the task id)")
    p.add_argument("--depends", default="", help="comma-separated ids of tasks whose code this one needs")
    p.add_argument("--description", default="")

    sub.add_parser("tasks", help="list every task")
    p = sub.add_parser("task", help="show one task")
    p.add_argument("id")
    sub.add_parser("check", help="report missing dependencies and cycles")
    sub.add_parser("ready", help="list tasks that can start now")
    p = sub.add_parser("start-wave", help="start every ready task in its own worktree")
    p.add_argument("--limit", type=int)

    p = sub.add_parser("finish", help="report a task as done, blocked or failed")
    p.add_argument("id")
    p.add_argument("--status", choices=FINISH_STATUSES, required=True)
    p.add_argument("--summary", default="")

    p = sub.add_parser("reopen", help="send a done task back, for example after a failing test")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    sub.add_parser("release", help="return tasks a stopped run left in progress to todo")
    p = sub.add_parser("cleanup", help="remove a task's worktree, and its branch once merged")
    p.add_argument("id")

    p = sub.add_parser("ask", help="record a decision (--choice) or a question for the user")
    p.add_argument("--task", default=HUB_OWNER, help="the asking task's id, or hub")
    p.add_argument("--text", required=True)
    p.add_argument("--critical", action="store_true", help="the user should see this first")
    p.add_argument("--choice", help="what you picked; without it a critical question blocks work")
    p.add_argument("--why", default="")
    p.add_argument("--option", action="append", default=[], help="an alternative; repeat for more")
    p.add_argument("--affects", default="", help="comma-separated ids of other tasks this touches")

    p = sub.add_parser("answer", help="answer a question, or change or confirm a decision")
    p.add_argument("id")
    p.add_argument("--text")
    p.add_argument("--keep", action="store_true", help="confirm a decision as it is")
    p = sub.add_parser("promote", help="move a smaller decision up to the big guesses")
    p.add_argument("id")
    p = sub.add_parser("dismiss", help="close a question without answering it")
    p.add_argument("id")
    p.add_argument("--reason", required=True)

    sub.add_parser("questions", help="list every question and decision")
    sub.add_parser("render", help="write QUESTIONS.md")
    p = sub.add_parser("report", help="write .hub/rounds/<round>.md")
    p.add_argument("--notes", default="")
    p = sub.add_parser("usage", help="show the latest usage reading, or save it as the round's start or end")
    p.add_argument("--mark", choices=("start", "end"))
    sub.add_parser("status", help="summarize tasks and questions")
    sub.add_parser("next-round", help="move to the next round")
    return parser


def run(hub, args):
    command = args.command
    if command == "init":
        if args.idea_file == "-":
            idea = sys.stdin.read()
        elif args.idea_file:
            idea = Path(args.idea_file).read_text(encoding="utf-8")
        elif args.idea:
            idea = args.idea
        else:
            raise HubError("give the idea with --idea or --idea-file")
        return hub.init(idea)
    if command == "add-task":
        return hub.add_task(args.id, args.title, args.kind, args.module, split_ids(args.depends), args.description)
    if command == "tasks":
        return hub.tasks()
    if command == "task":
        return hub.task(args.id)
    if command == "check":
        return {"errors": hub.check()}
    if command == "ready":
        return {"tasks": hub.ready()}
    if command == "start-wave":
        return hub.start_wave(args.limit)
    if command == "finish":
        return hub.finish(args.id, args.status, args.summary)
    if command == "reopen":
        return hub.reopen(args.id, args.reason)
    if command == "release":
        return hub.release()
    if command == "cleanup":
        return hub.cleanup(args.id)
    if command == "ask":
        return hub.ask(args.task, args.text, args.critical, args.choice, args.why, args.option, split_ids(args.affects))
    if command == "answer":
        return hub.answer(args.id, args.text, args.keep)
    if command == "promote":
        return hub.promote(args.id)
    if command == "dismiss":
        return hub.dismiss(args.id, args.reason)
    if command == "questions":
        return hub.questions()
    if command == "render":
        return hub.render()
    if command == "report":
        return hub.report(args.notes)
    if command == "usage":
        return hub.mark_usage(args.mark) if args.mark else hub.latest_usage()
    if command == "status":
        return hub.status()
    if command == "next-round":
        return hub.next_round()
    raise HubError(f"unknown command {command}")


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = run(Hub(args.project), args)
    except HubError as e:
        print(f"hub.py: error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
