"""The desk: the one thing the UI talks to. It talks to the hub.

The UI could call the hub directly, but the hub is a store (tasks, questions, contracts)
with no idea of what's running. The desk is the go-between that knows:

- which project is open (the workspace), and how to make or bring in a new one;
- the chat: what you say goes to the brainstormer, its questions come back with options,
  and "build" starts rounds;
- what's running now. Long work (a brainstorm turn, rounds, reading a project brought in)
  runs on one background thread, one job at a time, so the UI never waits and two rounds
  never touch the hub at once. Stop stops it partway (the model call that's running is
  cut off, and the files it was on go back to todo), or after the round if you'd rather;
- every answer you gave, so you can change one: the chat keeps which question each was;
- the app's preview server and each role's model.

Everything it does goes through the hub, Brainstorm and run_rounds, the same code the
terminal commands use, so a project can be worked on from either. The chat is kept in
.hub/desk.json, so it's still there after a restart.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import traceback

from . import project
from .brainstorm import Brainstorm
from .hub_local import HubError, LocalHub
from .hub_local import HUB_OWNER
from .llm import Interrupted, LLMError, OllamaClient
from .protocol import ROLE_JOBS
from .rounds import run_rounds, run_summary
from .runner import Runner, free_port
from .scheduler import Scheduler

# The file manager and editor: what they skip, what they may change, and how big a file they open.
HIDDEN = {".git", "node_modules", "__pycache__", ".probe.html", ".DS_Store", ".worktrees"}
READ_ONLY = {".git", ".hub"}  # the hub is written by rounds only; git by git
MAX_FILES = 2000
MAX_OPEN = 1_000_000  # bytes
LANGUAGES = {
    ".js": "JavaScript", ".mjs": "JavaScript", ".ts": "TypeScript", ".py": "Python", ".html": "HTML",
    ".css": "CSS", ".json": "JSON", ".md": "Markdown", ".txt": "Text", ".jsonl": "JSON Lines",
    ".sh": "Shell", ".yml": "YAML", ".yaml": "YAML", ".toml": "TOML", ".svg": "SVG", ".csv": "CSV",
}

BUILD_WORDS = ("build", "build it", "go", "start", "start building")
# The cycle, in order: (key, name, what happens). The studio lists these and lights up the current one.
STAGES = [
    ("talk", "Talk it through", "You and the brainstormer agree on a brief"),
    ("plan", "Plan", "Files and the contracts between them"),
    ("build", "Build files", "Tests first, then each file until its tests pass"),
    ("page", "Design the page", "Built from the kit, checked in Chrome"),
    ("verify", "Check everything", "Every file's tests, and one fix pass"),
    ("try", "Test users", "Three people try the page"),
    ("wrap", "Report", "What was built, what's left for you, what was guessed"),
]


def client_for(hub):
    cfg = hub.local_config()
    return OllamaClient(cfg["host"], cfg["num_ctx"], cfg["temperature"], cfg["keep_alive"], cfg["call_timeout"])


class Desk:
    def __init__(self, llm_for=client_for):
        self.llm_for = llm_for  # hub -> model client; tests pass a fake
        self.hub = None
        self.busy = None  # what's running, in words, or None
        self.stop_after_round = False
        self.stop_now = False
        self.pending = []  # the brainstormer's open questions: [{question, answer}]
        self.ready = False
        self.turns = 0
        self.app = None  # (url, server process, runner)
        self._lock = threading.Lock()

    # Workspace

    def open(self, folder):
        """Work on a project folder that is already a roughcut-local project."""
        if self.busy:
            raise HubError(f"wait until it's done: {self.busy}")
        hub = LocalHub(folder)
        hub.config()  # fails if it isn't a project
        self.stop_app()
        self.hub, self.pending, self.ready, self.turns = hub, [], False, 0
        project.remember(hub)
        return self.state()

    def new(self, idea, stack="web"):
        """Make a project from an idea and start talking it through."""
        if not idea.strip():
            raise HubError("say what you want to build")
        hub = LocalHub(project.free_folder(project.slug(idea)))
        hub.init(idea, {"stack": stack})
        project.render(hub)
        self.open(hub.project)
        self.post("desk", f"New project in {hub.project}. Let's talk it through before building.")
        return self.say(idea)

    def bring_in(self, target):
        """Open a folder, project name or GitHub URL; a project from elsewhere is surveyed first."""
        folder, new = project.locate(target.strip())
        if not new:
            return self.open(folder)

        def work():
            hub, found = project.bring_in(folder, OllamaClient())
            self.open_after_job = hub.project
            return f"Brought in {folder}. Contracts for: {', '.join(found['surveyed']) or 'nothing in app/'}. " \
                   "Say what you'd like to change."

        return self.run("reading the code it brought in", work)

    # The chat

    def say(self, text):
        hub = self.need_hub()
        text = text.strip()
        if not text:
            return self.state()
        if text.lower() in BUILD_WORDS:
            return self.build()
        self.post("you", text)

        def work():
            session = Brainstorm(hub)
            if self.pending:  # unanswered questions take the pick, and what you said is added
                self.answer_rest(session)
                session.add(text)
            else:
                session.start(text)
                self.turns = 0
            return self.next_turn(session)

        return self.run("thinking it through", work)

    def answer(self, index, reply):
        """Answer one of the brainstormer's questions: "" takes its pick, else an option or your words."""
        item = self.pending[index]
        if item["answer"] is not None:  # answered already: this changes it
            return self.answer_question(item["id"], reply or item["question"]["default"])
        session = Brainstorm(self.need_hub())
        item["answer"], item["id"] = session.answer(item["question"], reply), session.last_id
        self.post("swarm", item["question"]["text"])
        self.post("you", item["answer"], ref=self.ref(item["id"], item["question"]))
        return self.state()

    def ref(self, question_id, question):
        """What the chat keeps with your answer, so the studio can offer to change it."""
        options = [o for o in [question.get("default") or question.get("choice"), *question.get("options", [])] if o]
        return {"id": question_id, "text": question["text"], "options": list(dict.fromkeys(options)),
                "details": question.get("details") or {}, "pick": question.get("default") or question.get("choice")}

    def keep_talking(self):
        """Another brainstorm turn, after the questions are answered (any left take the pick)."""
        hub = self.need_hub()

        def work():
            session = Brainstorm(hub)
            self.answer_rest(session)
            return self.next_turn(session)

        return self.run("thinking it through", work)

    def answer_rest(self, session):
        for item in self.pending:
            if item["answer"] is None:
                item["answer"], item["id"] = session.answer(item["question"], ""), session.last_id

    def next_turn(self, session):
        hub = self.hub
        cfg = hub.local_config()
        idea = (hub.root / "idea.md").read_text(encoding="utf-8")
        left = max(1, cfg["brainstorm_turns"] - self.turns)
        reply = asyncio.run(session.turn(Scheduler(self.llm_for(hub), cfg, hub.events, lambda: self.stop_now), idea, left))
        self.turns += 1
        project.set_idea_from_brief(hub, idea)
        project.render(hub)
        self.pending = [{"question": q, "answer": None} for q in reply["questions"]]
        self.ready = reply["ready"] or not reply["questions"]
        brief = reply["brief"]
        lines = [f"{brief['name']}: {brief['pitch']}"] + [f"• {p}" for p in reply["pushback"]]
        if self.ready and not self.pending:
            lines.append("The brief looks clear to me. Press Build when you're ready, or tell me more.")
        self.post("brainstormer", "\n".join(lines), questions=[i["question"] for i in self.pending])
        return None

    # Building

    def build(self, rounds=None):
        hub = self.need_hub()
        rounds = rounds or (6 if not hub.contracts.all() else 4)
        self.stop_after_round = False

        def work():
            if self.pending:
                self.answer_rest(Brainstorm(hub))
                self.pending = []
            idea = (hub.root / "idea.md").read_text(encoding="utf-8")
            project.set_idea_from_brief(hub, idea)
            summaries, why = run_rounds(hub, self.llm_for(hub), rounds, stop=lambda: self.stop_after_round,
                                        cancel=lambda: self.stop_now)
            project.render(hub)
            return "Built " + run_summary(hub, summaries, why)

        self.post("you", f"Build ({rounds} round{'s' if rounds != 1 else ''} at most)")
        return self.run("building", work)

    def stop(self, now=True):
        """Stop what's running: now (the model call running is cut off), or once this round is done."""
        if not self.busy:
            return self.state()
        self.stop_after_round = True
        self.stop_now = self.stop_now or now
        self.post("desk", "Stopping now." if now else "I'll stop after this round finishes.")
        return self.state()

    def answer_question(self, question_id, text=None, keep=False):
        """Answer a question or guess from the rounds (QUESTIONS.md), or change an answer you gave before."""
        hub = self.need_hub()
        before = hub.question(question_id)
        result = hub.answer(question_id, text, keep)
        question = result["question"]
        if before["status"] == "answered":
            for item in self.pending:  # a brainstorm question not sent back yet
                if item.get("id") == question_id:
                    item["answer"] = question["answer"]
            if question["task"] == HUB_OWNER:
                Brainstorm(hub).add(f"I changed my answer: {question['text']} -> {question['answer']}")
            self.post("you", f"Changed my answer to \u201c{question['text']}\u201d: {question['answer']}",
                      ref=self.ref(question_id, question))
            if self.busy == "building":
                self.post("desk", "The next round plans with it. Press Stop if you want it picked up sooner.")
        else:
            self.post("swarm", question["text"])
            self.post("you", question["answer"], ref=self.ref(question_id, question))
        hub.render()
        project.render(hub)
        return {**self.state(), "reworked": result["reworked"]}

    # The app and models

    def app_url(self):
        hub = self.need_hub()
        if hub.stack.name != "web" or not (hub.project / "index.html").exists():
            raise HubError("there's no page yet: build first")
        if self.app is None:
            runner = Runner(hub.project, hub.stack, sandbox=hub.local_config()["sandbox"])
            port = free_port()
            self.app = (f"http://127.0.0.1:{port}/", runner.serve(port), runner)
            time.sleep(0.4)
        return {"url": self.app[0]}

    def stop_app(self):
        if self.app:
            self.app[2].stop(self.app[1])
            self.app = None

    # Files: the open project's folder, for the file manager and the editor

    def project_path(self, path):
        """A path inside the open project, or an error. Nothing outside it can be read or written."""
        hub = self.need_hub()
        root = hub.project.resolve()
        target = (root / path).resolve()
        if target != root and root not in target.parents:
            raise HubError("that's outside the project")
        return root, target

    def files(self):
        """Every file and folder in the project, for the tree (hidden ones left out)."""
        root, _ = self.project_path(".")
        entries = []
        for item in sorted(root.rglob("*"), key=lambda p: (str(p.parent).lower(), p.is_file(), p.name.lower())):
            rel = item.relative_to(root)
            if any(part in HIDDEN for part in rel.parts):
                continue
            entries.append({"path": rel.as_posix(), "dir": item.is_dir(), "size": item.stat().st_size if item.is_file() else 0})
            if len(entries) >= MAX_FILES:
                break
        return {"root": root.name, "files": entries}

    def read_file(self, path):
        """A file's text, with its language and, for a file with a contract, the contract and its tests."""
        hub = self.need_hub()
        root, target = self.project_path(path)
        if not target.is_file():
            raise HubError(f"no file {path}")
        if target.stat().st_size > MAX_OPEN:
            raise HubError("that file is too big to open here")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise HubError("that isn't a text file") from None
        rel = target.relative_to(root).as_posix()
        contract = hub.contracts.get(rel) if hub.contracts.exists(rel) else None
        return {
            "path": rel, "text": text, "language": LANGUAGES.get(target.suffix.lower(), "Text"),
            "read_only": rel.split("/")[0] in READ_ONLY,
            "contract": hub.contracts.describe(contract) if contract else None,
            "test_path": contract["test_path"] if contract and (root / contract["test_path"]).exists() else None,
        }

    def write_file(self, path, text):
        """Save a file you edited. Rounds pick up a file left for you once its tests pass."""
        root, target = self.project_path(path)
        rel = target.relative_to(root).as_posix()
        if rel.split("/")[0] in READ_ONLY or rel in ("", "."):
            raise HubError(f"{rel or 'that'} can't be edited here")
        if target.is_dir():
            raise HubError("that's a folder")
        self.hub._write_text(target, text)
        return {"saved": rel, "bytes": len(text.encode("utf-8"))}

    def test_file(self, path):
        """Run a file's tests in the box, the same way a round does. Returns whether they pass, and the output."""
        hub = self.need_hub()
        info = self.read_file(path)
        if not info["test_path"]:
            raise HubError(f"{path} has no tests")
        runner = Runner(hub.project, hub.stack, hub.local_config()["test_timeout"], hub.local_config()["sandbox"])
        passed, output = asyncio.run(runner.run(info["test_path"]))
        return {"passed": passed, "output": output[-3000:], "test_path": info["test_path"]}

    def models(self):
        """Each role's model and job, what Ollama has installed, and whether it answers."""
        cfg = self.hub.local_config() if self.hub else None
        client = self.llm_for(self.hub) if self.hub else OllamaClient()
        try:
            installed, reachable, error = client.installed(), True, ""
        except LLMError as e:
            installed, reachable, error = [], False, str(e)
        roles = [{"role": r, "job": job, "model": cfg["models"].get(r) if cfg else None} for r, job in ROLE_JOBS.items()]
        limits = {k: cfg[k] for k in ("max_parallel_calls", "max_parallel_tasks", "num_ctx", "test_users", "design_tries")} \
            if cfg else {}
        return {"reachable": reachable, "error": error, "installed": installed, "roles": roles, "limits": limits}

    def set_model(self, role, model):
        return {"models": self.need_hub().set_model(role, model)}

    # Jobs, messages and state

    def run(self, what, work):
        """Run work() on the background thread. Its return value, if any, is posted to the chat."""
        with self._lock:
            if self.busy:
                raise HubError(f"wait until it's done: {self.busy}")
            self.busy = what
            self.open_after_job = None
            self.stop_now = False

        def go():
            try:
                message = work()
                if self.open_after_job:
                    self.busy = None
                    self.open(self.open_after_job)
                if message:
                    self.post("desk", message)
            except Interrupted:
                self.post("desk", "Stopped. Change anything you like, then Build or keep talking.")
            except Exception as e:  # shown in the chat; the details go to the terminal
                traceback.print_exc()
                self.post("desk", f"That didn't work: {e}")
            finally:
                self.busy = None

        threading.Thread(target=go, daemon=True).start()
        return self.state()

    def need_hub(self):
        if self.hub is None:
            raise HubError("pick a project first")
        return self.hub

    def chat_path(self):
        return self.hub.root / "desk.json" if self.hub else None

    def chat(self):
        path = self.chat_path()
        return json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else []

    def post(self, who, text, questions=None, ref=None):
        if not self.hub:
            return
        messages = self.chat()
        message = {"who": who, "text": text, "questions": questions or [], "ts": time.time()}
        if ref:
            message["ref"] = ref
        messages.append(message)
        self.hub._write_text(self.chat_path(), json.dumps(messages[-200:], indent=2) + "\n")

    def stage(self, snapshot=None):
        """Which stage of the cycle is running now, or None when nothing is."""
        if self.busy == "thinking it through":
            return "talk"
        if self.busy != "building" or not snapshot:
            return None
        phase = snapshot.get("phase")
        if phase == "build":
            running = [t for t in snapshot.get("tasks", []) if t["status"] == "in_progress"]
            return "page" if running and all(t["module"] == "index.html" for t in running) else "build"
        return {"plan": "plan", "verify": "verify", "fix": "verify", "try": "try", "questions": "wrap"}.get(phase, "plan")

    def state(self):
        hub = self.hub
        return {
            "project": {"path": str(hub.project), "name": project.load_brief(hub)["name"] if project.load_brief(hub)
                        else hub.project.name, "stack": hub.stack.name, "round": hub.config()["round"]} if hub else None,
            "busy": self.busy,
            "stopping": ("now" if self.stop_now else "after this round") if self.stop_after_round and self.busy else None,
            "chat": self.chat()[-60:],
            "pending": [{"index": i, **item} for i, item in enumerate(self.pending)],
            "ready": self.ready,
            "app": self.app[0] if self.app else None,
            "stages": [{"key": k, "name": n, "what": w} for k, n, w in STAGES],
        }
