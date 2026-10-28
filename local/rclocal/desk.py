"""The desk: the one thing the UI talks to. It talks to the hub.

The UI could call the hub directly, but the hub is a store (tasks, questions, contracts)
with no idea of what's running. The desk is the go-between that knows:

- which project is open (the workspace), and how to make or bring in a new one;
- the chat: what you say goes to the brainstormer, its questions come back with options,
  and "build" starts rounds;
- what's running now. Long work (a brainstorm turn, rounds, reading a project brought in)
  runs on one background thread, one job at a time, so the UI never waits and two rounds
  never touch the hub at once. Asking it to stop takes effect between rounds;
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
from .llm import LLMError, OllamaClient
from .protocol import ROLE_JOBS
from .rounds import run_rounds
from .runner import Runner, free_port
from .scheduler import Scheduler

BUILD_WORDS = ("build", "build it", "go", "start", "start building")


def client_for(hub):
    cfg = hub.local_config()
    return OllamaClient(cfg["host"], cfg["num_ctx"], cfg["temperature"], cfg["keep_alive"], cfg["call_timeout"])


class Desk:
    def __init__(self, llm_for=client_for):
        self.llm_for = llm_for  # hub -> model client; tests pass a fake
        self.hub = None
        self.busy = None  # what's running, in words, or None
        self.stop_after_round = False
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
        if item["answer"] is None:
            item["answer"] = Brainstorm(self.need_hub()).answer(item["question"], reply)
            self.post("you", f"{item['question']['text']} → {item['answer']}")
        return self.state()

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
                item["answer"] = session.answer(item["question"], "")

    def next_turn(self, session):
        hub = self.hub
        cfg = hub.local_config()
        idea = (hub.root / "idea.md").read_text(encoding="utf-8")
        left = max(1, cfg["brainstorm_turns"] - self.turns)
        reply = asyncio.run(session.turn(Scheduler(self.llm_for(hub), cfg, hub.events), idea, left))
        self.turns += 1
        project.set_idea_from_brief(hub, idea)
        project.render(hub)
        self.pending = [{"question": q, "answer": None} for q in reply["questions"]]
        self.ready = reply["ready"] or not reply["questions"]
        brief = reply["brief"]
        lines = [f"**{brief['name']}**: {brief['pitch']}"] + [f"• {p}" for p in reply["pushback"]]
        if self.ready and not self.pending:
            lines.append("The brief looks clear to me. Press Build when you're ready, or tell me more.")
        self.post("brainstormer", "\n".join(lines), questions=[i["question"] for i in self.pending])
        return None

    # Building

    def build(self, rounds=None):
        hub = self.need_hub()
        rounds = rounds or (4 if not hub.contracts.all() else 2)
        self.stop_after_round = False

        def work():
            if self.pending:
                self.answer_rest(Brainstorm(hub))
                self.pending = []
            idea = (hub.root / "idea.md").read_text(encoding="utf-8")
            project.set_idea_from_brief(hub, idea)
            summaries, why = run_rounds(hub, self.llm_for(hub), rounds, stop=lambda: self.stop_after_round)
            project.render(hub)
            last = summaries[-1] if summaries else {}
            status = hub.status()
            return (f"Built {len(summaries)} round{'s' if len(summaries) != 1 else ''}; stopped because {why}. "
                    f"Tests {'pass' if last.get('tests_passed') else 'do not all pass yet'}. "
                    f"{len(status['open_questions'])} question(s) need you, "
                    f"{len(status['guesses_to_confirm'])} guess(es) to confirm.")

        self.post("you", f"Build ({rounds} round{'s' if rounds != 1 else ''} at most)")
        return self.run("building", work)

    def stop(self):
        self.stop_after_round = True
        self.post("desk", "I'll stop after this round finishes.")
        return self.state()

    def answer_question(self, question_id, text=None, keep=False):
        """Answer a question or guess from the rounds (QUESTIONS.md)."""
        hub = self.need_hub()
        result = hub.answer(question_id, text, keep)
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

        def go():
            try:
                message = work()
                if self.open_after_job:
                    self.busy = None
                    self.open(self.open_after_job)
                if message:
                    self.post("desk", message)
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

    def post(self, who, text, questions=None):
        if not self.hub:
            return
        messages = self.chat()
        messages.append({"who": who, "text": text, "questions": questions or [], "ts": time.time()})
        self.hub._write_text(self.chat_path(), json.dumps(messages[-200:], indent=2) + "\n")

    def state(self):
        hub = self.hub
        return {
            "project": {"path": str(hub.project), "name": project.load_brief(hub)["name"] if project.load_brief(hub)
                        else hub.project.name, "stack": hub.stack.name, "round": hub.config()["round"]} if hub else None,
            "busy": self.busy,
            "stopping": self.stop_after_round and bool(self.busy),
            "chat": self.chat()[-60:],
            "pending": [{"index": i, **item} for i, item in enumerate(self.pending)],
            "ready": self.ready,
            "app": self.app[0] if self.app else None,
        }
