"""One round: plan, build every ready file in parallel, verify, then questions for the user.

This is the only part that writes the project's files and the hub. Agents return data
(protocol.REPLIES) or code; the round checks it, writes it, runs it inside the box
(runner.py) and decides what happens next. A task starts as soon as a slot is free and
everything it depends on is done, so one slow file doesn't hold up the rest.

Each logic file gets two tasks: `<id>-tests`, written from the contract alone, then `<id>`,
a builder agent that owns that one file. The agent picks its own steps (read, write,
test, finish, ask), but every step is carried out here and only if it's allowed: it
can read only its file, its tests and the files it imports, write only its own file,
and nothing it writes runs until the stack's guard has passed it. When an agent runs
out of steps with failing tests, a reviewer decides whether the code, the test or the
contract is wrong. On the web stack the page comes first: a designer makes index.html and
styles.css as soon as there's a plan, against placeholder files that return their
contracts' examples, so you can open the app and try it while the logic is built. It's
loaded in headless Chrome to check that it loads, and checked fully (and tried by the
test users) once every logic file is done.
"""

from __future__ import annotations

import asyncio
import json
import posixpath
import re
import subprocess
import time

from . import agents, blame, pages, project
from .contracts import ContractError, file_id
from .hub_local import HUB_OWNER, HubError
from .likeness import settled
from .llm import Interrupted, LLMError
from .notify import awake, notify
from .runner import Runner
from .scheduler import Scheduler
from .stacks import PLACEHOLDER, example_cases, filter_styles, literal_prefix
from .users import TestUsers

OPEN_FENCE = re.compile(r"^\s*```[a-zA-Z]*[ \t]*\n")
CLOSE_FENCE = re.compile(r"\n?```\s*$")
PAGE_IMPORT = re.compile(r"""import\s*\{([^}]*)\}\s*from\s*['"]\./(app/[^'"]+)['"]""")
MAX_DECISIONS = 2  # per task; small models over-report
MAX_GUESSES = 3  # per plan, so the questions you get are few and real
# What you can say about a file the models got stuck on. Nothing waits for your answer:
# the file is left for you, and the run goes on with everything else.
# The answers are your replies, so they're in your voice.
I_GOT_IT = "I got it"
IT_WORKS = "It works"
TRY_AGAIN = "Try again"
SIMPLIFY = "Simplify it"
KEEP_ANSWERS = {I_GOT_IT, "Left for you"}  # "Left for you" is what earlier versions wrote
STUCK_OPTIONS = [
    {"label": I_GOT_IT, "detail": "You'll finish it yourself. It keeps its best attempt, with a note at the top saying "
                                  "what's missing; once its tests pass, the next round picks it up."},
    {"label": IT_WORKS, "detail": "It does what you want as it is: count it as done. The tests it fails stay, but they "
                                  "don't hold anything back, and the files that need it get built."},
    {"label": TRY_AGAIN, "detail": "The builder gets a fresh set of tries next round, with what you say here. When "
                                   "the contract was to blame, its tests are written again too."},
    {"label": SIMPLIFY, "detail": "The planner trims what this file promises next round, its tests are written "
                                  "again, then it's built again."},
]
# Small models get arithmetic wrong, and a wrong example fails a right file. So you check each new one.
RIGHT, WRONG = "Right", "Wrong"
EXAMPLE_OPTIONS = [
    {"label": RIGHT, "detail": "Keep it: the file's tests hold it to this."},
    {"label": WRONG, "detail": "Drop it from the contract and the tests. Or type what it should return instead."},
]
MAX_EXAMPLE_CHECKS = 4  # per plan
KEEP_TURNS = 12  # agent messages kept after the opening two, so its context stays small
SPLIT_AFTER = 2  # failed attempts with the code to blame before a file is split into smaller ones
MAX_PIECES = 4
DESIGN_TASK = "page-design"
# On the web stack the page's own script is the UI; a logic file by one of these names would need the DOM.
INLINE_HANDLER = re.compile(r"\s(on[a-z]+)\s*=\s*[\"']", re.I)
UI_NAMES = {"ui", "dom", "view", "views", "page", "index", "main", "app", "display", "render", "renderer", "interface", "screen"}
VERIFY_PARALLEL = 3
REPLAN = "Plan it again"  # the answer recorded when a contract can't work, so the next plan revises it
MAX_RULES = 20


def clean_code(text):
    """The file inside a code reply. Generation stops at the closing fence, so it is usually gone already."""
    text = OPEN_FENCE.sub("", text, count=1)
    return CLOSE_FENCE.sub("", text).strip() + "\n"


STACK_LINE = re.compile(r"^\s+at |node:internal|^\s*(generatedMessage|code|operator|diff):|^\s*[{}]\s*$|^ℹ ")


def failure_digest(output, limit):
    """The part of a test run that says what went wrong, at most `limit` characters.

    The end of node's output is a summary ("test failed"); the error and the actual and
    expected values come before it, buried in stack frames. Keep those, drop the frames.
    """
    lines = [l for l in (output or "").splitlines() if not STACK_LINE.search(l)]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text if len(text) <= limit else text[: limit - 15] + "\n… (cut short)"


def examples_of(contract):
    return {(e["name"], x["call"], x["returns"]) for e in contract["exports"] for x in e.get("examples", [])}


def with_verdicts(export, verdicts):
    """An export with your answers about its examples applied: wrong ones out, corrected ones fixed."""
    examples = []
    for x in export.get("examples", []):
        said = verdicts.get((export["name"], x["call"], x["returns"]))
        if said is None or said == RIGHT:
            examples.append(x)
        elif said.lower() != WRONG.lower():
            examples.append({**x, "returns": said})
    return {**export, "examples": examples}


def norm(path):
    """A path as the model wrote it, tidied: './app/x.js' -> 'app/x.js'. '..' parts stay, and fail the allow-list."""
    path = path.strip()
    return posixpath.normpath(path) if path else ""


def tidy_path(path, stack):
    """A planned path made to fit the layout without a model call: 'calculatePace.js' -> 'app/calculate_pace.js'."""
    path = norm(path)
    folder, _, name = path.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", stem).replace("-", "_").lower()
    ext = ext.lower() or stack.example.rsplit(".", 1)[1]
    folder = folder.lower().replace("-", "_")
    if not (folder == "app" or folder.startswith("app/")):
        folder = "app" + ("/" + folder if folder and folder not in (".", "src", "lib") else "")
    return f"{folder}/{stem}.{ext}"


def cut_off_note(path):
    return f"{path} was cut off before it ended: it ran past the length limit. Write it shorter."


def check_code(stack, code, path, exports=()):
    """Return (problem, guarded): a problem to send back, or "" when the file may be written and run."""
    blocked = stack.guard(code)
    if blocked:
        return f"rejected by the safety guard, not run: it {'; '.join(blocked)}. Remove that.", True
    problem = stack.syntax_error(code, path)
    if problem:
        return problem, False
    missing = sorted({e["name"].split(".")[0] for e in exports} - stack.defined_names(code))
    if missing:
        return f"the file doesn't define these exports from its contract: {', '.join(missing)}", False
    return stack.code_problem(code, path), False


class Round:
    def __init__(self, hub, llm, echo=None, cancel=None):
        self.hub = hub
        self.cfg = hub.local_config()
        self.stack = hub.stack
        self.events = hub.events
        self.events.echo = echo
        self.sched = Scheduler(llm, self.cfg, self.events, cancel)
        self.runner = Runner(hub.project, self.stack, self.cfg["test_timeout"], self.cfg["sandbox"], self.events)
        self.requests = []  # (task id, contract request), applied between tasks
        self.number = None
        self.page_checked = False  # a page passed its check this round, so the test users try it
        self.cancel = cancel
        self.examples_asked = None  # when this round's plan asked you to check examples (time.monotonic())

    # The round

    async def run(self):
        hub = self.hub
        self.number = hub.config()["round"]
        self.events.round = self.number
        idea = (hub.root / "idea.md").read_text(encoding="utf-8").strip()
        self.events.emit("round_start", idea=idea[:300])
        if not self.runner.sandbox:
            self.events.emit("note", text="sandbox-exec isn't available: tests run with limits but without the sandbox")
        hub.release()
        await self.revisit_stuck()
        self.apply_example_answers()
        for task_id in hub.retry_failed():
            self.events.emit("note", text=f"retrying {task_id}, which failed last round")

        self.events.emit("phase", name="plan")
        complete = await self.plan(idea)
        self.commit(f"Round {self.number} plan")

        self.events.emit("phase", name="build")
        await self.build_pass(idea)
        if self.recheck_page():
            await self.build_pass(idea)

        self.events.emit("phase", name="verify")
        passed, failing, output = await self.verify()
        if failing and self.reopen(failing):
            self.events.emit("phase", name="fix")
            await self.build_pass(idea)
            passed, failing, output = await self.verify()
            self.reopen(failing)  # left for the next round

        inventory = (getattr(self.runner, "last_check", None) or {}).get("inventory")
        if self.page_checked and inventory and self.cfg["test_users"]:
            self.events.emit("phase", name="try")
            await TestUsers(self).run(project.brief_text(hub), inventory)

        self.events.emit("phase", name="questions")
        notes = await self.report_notes(passed, output)
        hub.render()
        hub.contracts.render()
        report = hub.report(notes)
        project.render(hub, notes)
        self.commit(f"Round {self.number} questions")
        status = hub.status()
        screen = hub.root / "screens" / f"round-{self.number}.png"
        summary = {
            "round": self.number,
            "tasks": status["tasks"],
            "tests_passed": passed,
            "open_questions": status["open_questions"],
            "guesses_to_confirm": len(status["guesses_to_confirm"]),
            "complete": bool(complete) and passed,  # the planner's word alone isn't enough
            "report": report["path"],
            "screenshot": str(screen) if screen.exists() else None,
        }
        hub.next_round()
        self.events.emit("round_end", summary=summary)
        return summary

    # Plan

    async def plan(self, idea):
        hub = self.hub
        self.untangle()  # also repairs a project an earlier version left with circular imports
        first = not hub.contracts.all()
        answers = [q for q in hub.answered_since(self.number) if not is_stuck_question(q)]  # revisit_stuck acts on those
        has_todo = any(t["status"] == "todo" for t in hub.tasks())
        if not (first or answers or not has_todo):
            self.events.emit("note", text="unfinished tasks and no new answers, so the plan stays as it is")
            return None
        reply = await agents.plan(
            self.sched, self.stack, idea, hub.memory.conventions(), self.contracts_md(), answers, first, self.rules()
        )
        fix = self.plan_problems(reply["files"])
        if fix:  # one more try, told why
            self.events.emit("note", text=f"the plan had problems ({'; '.join(fix)}); asking again")
            retry = [{"text": "Your last plan was rejected", "answer": f"{' '.join(fix)} Plan again."}]
            reply = await agents.plan(
                self.sched, self.stack, idea, hub.memory.conventions(), self.contracts_md(), answers + retry, first, self.rules()
            )
        reply = {**reply, "files": [self.clean_file_plan(f) for f in reply["files"]]}
        skipped = self.apply_plan(reply)
        hub.mark_planned([q["id"] for q in answers])  # after: which files may change depends on them
        if first and not hub.contracts.all():  # nothing usable: one more try, told why
            why = "; ".join(skipped) or "the plan had no files"
            self.events.emit("note", text=f"the plan had no usable files ({why}); asking again")
            retry = [{"text": "Your last plan was rejected", "answer": f"{why}. Plan again."}]
            reply = await agents.plan(self.sched, self.stack, idea, hub.memory.conventions(), "", answers + retry, first,
                                      self.rules())
            self.apply_plan({**reply, "files": [self.clean_file_plan(f) for f in reply["files"]]})
        return reply["complete"]

    def bad_examples(self, file_plans):
        """How many of a plan's examples aren't valid code, out of how many."""
        examples = [x for f in file_plans for e in f["exports"] for x in e.get("examples", [])]
        bad = sum(1 for x in examples if not self.stack.example_ok(x["call"], x["returns"]))
        return bad, len(examples)

    def plan_problems(self, file_plans):
        """What's wrong with a plan that one more try could fix, as sentences to the planner."""
        problems = []
        bad, total = self.bad_examples(file_plans)
        if bad:
            problems.append(f"{bad} of your {total} examples weren't literal values: each example's returns must be a "
                            f"plain value in {self.stack.lang_name}, like 15 or [58, 57], never a calculation, a call "
                            "or a sentence.")
        seen = {}
        for f in file_plans:
            for e in f["exports"]:
                cases = tuple(sorted((x["call"].split("(", 1)[-1], x["returns"]) for x in e.get("examples", [])))
                if cases and cases in seen:
                    problems.append(f"{e['name']} and {seen[cases]} have the same examples, so they'd do the same thing: "
                                    "give each the inputs its feature needs from the user (a list of percentages, say), "
                                    "or plan one function.")
                seen.setdefault(cases, e["name"])
        return problems

    def clean_file_plan(self, file_plan):
        """A planned file made safe to use: examples that aren't values dropped, parameters that hide imports renamed."""
        exports = []
        for e in file_plan["exports"]:
            examples = [x for x in e.get("examples", []) if self.stack.example_ok(x["call"], x["returns"])]
            exports.append({**e, "examples": examples})
        hidden = self.import_names(file_plan)
        return {**file_plan, "exports": [{**e, "signature": unshadow(e["signature"], hidden)} for e in exports]}

    def import_names(self, file_plan):
        """Every name a planned file's imports export, as far as the contracts know."""
        contracts = self.hub.contracts
        names = set()
        for path in file_plan.get("imports", []):
            path = tidy_path(path, self.stack) if path.strip() else ""
            if path and contracts.exists(path):
                names.update(e["name"].split(".")[0] for e in contracts.get(path)["exports"])
        return names

    def rules(self):
        """Your settled answers and the brief's rules, as rules every role follows. Not just this round's: all of them."""
        brief = project.load_brief(self.hub) or {}
        lines = [f"- {rule}" for rule in brief.get("rules", []) if rule.strip()]
        for q in self.hub.questions():
            if q["task"] != HUB_OWNER or q["status"] != "answered" or q["answer"] == REPLAN:
                continue
            if q["text"].startswith("What should change next?"):
                continue  # a change request, handled by the plan it was asked for
            if q.get("example") and q["answer"] == RIGHT:
                continue  # nothing to follow; a wrong example is worth saying, so the planner doesn't repeat it
            lines.append(f"- {q['text']} -> {q['answer']}")
        return "\n".join(lines[-MAX_RULES:])

    def contracts_md(self):
        return "\n\n".join(self.hub.contracts.describe(c) for c in self.hub.contracts.all())

    def apply_plan(self, reply):
        hub, contracts = self.hub, self.hub.contracts
        hub.memory.set_conventions(reply["conventions"])
        asked = {q["text"] for q in hub.questions()}
        fresh = [g for g in reply["guesses"] if g["text"].strip() and g["text"].strip() not in asked
                 and not self.already_settled(g)]
        for guess in fresh[:MAX_GUESSES]:
            self.decide(HUB_OWNER, guess, critical=True)
        planned, skipped = [], []
        protected, verdicts, checks = self.protected(), self.example_verdicts(), []
        for file_plan in reply["files"]:
            tidy = {**file_plan, "path": tidy_path(file_plan["path"], self.stack),
                    "imports": [tidy_path(p, self.stack) for p in file_plan["imports"] if p.strip()]}
            tidy["exports"] = [with_verdicts(e, verdicts) for e in tidy["exports"]]
            if tidy["path"] != file_plan["path"]:
                self.events.emit("note", text=f"renamed {file_plan['path']} to {tidy['path']} to fit the layout")
            stem = posixpath.splitext(posixpath.basename(tidy["path"]))[0]
            if self.stack.name == "web" and stem in UI_NAMES:
                why = f"{tidy['path']} would be UI code, which belongs in the page's own script (the designer writes it)"
                self.events.emit("note", text=f"skipped a planned file: {why}")
                skipped.append(why)
                continue
            if tidy["path"] in protected and contracts.would_change(tidy):
                self.events.emit("note", text=f"kept {tidy['path']} as it is: it's done, and nothing you've said "
                                              "since is about it")
                continue
            had = examples_of(contracts.get(tidy["path"])) if contracts.exists(tidy["path"]) else set()
            try:
                contract, what = contracts.upsert(tidy, self.number, f"round {self.number} plan")
            except (ContractError, ValueError) as e:
                self.events.emit("note", text=f"skipped a planned file: {e}")
                skipped.append(str(e))
                continue
            planned.append((contract, what))
            checks += [(contract, e, x) for e in contract["exports"] for x in e.get("examples", [])
                       if (e["name"], x["call"], x["returns"]) not in had and self.stack.example_ok(x["call"], x["returns"])]
        known = {c["path"] for c in contracts.all()}
        for i, (contract, what) in enumerate(planned):
            unknown = [p for p in contract["imports"] if p not in known]
            if unknown:
                contract = contracts.set_imports(
                    contract["path"],
                    [p for p in contract["imports"] if p in known],
                    self.number,
                    f"dropped imports with no contract: {', '.join(unknown)}",
                )
                planned[i] = (contract, what)
        self.untangle([c["path"] for c, _ in planned])
        changed = False
        for contract, what in planned:
            contract = contracts.get(contract["path"])  # untangle may have changed its imports
            if what == "same":
                continue
            changed = True
            self.events.emit("contract", path=contract["path"], version=contract["version"], reason=what)
            self.ensure_tasks(contract, what)
            if what == "changed":
                for path in contracts.dependents(contract["path"]):
                    self.redo(file_id(path), f"{contract['path']} changed to contract v{contract['version']}")
        self.check_examples(checks)
        if self.stack.name == "web" and contracts.all():
            self.ensure_design_task(changed)
        self.write_packages()
        errors = hub.check()
        if errors:
            raise HubError("the plan's task graph is invalid: " + "; ".join(errors))
        return skipped

    def protected(self):
        """Files a plan may not change: done, and nothing you've said since the last plan is about them.

        A small planner rewrites contracts it was only meant to repeat, which rebuilds files that
        worked (one you finished by hand, even). When you asked for changes ("What should change
        next?"), nothing is protected: those can touch any file.
        """
        hub = self.hub
        answers = [q for q in hub.answered_since(self.number) if not is_stuck_question(q)]
        if any(q["text"].startswith("What should change next?") for q in answers):
            return set()
        done = {t["id"] for t in hub.tasks() if t["kind"] == "build" and t["status"] == "done"}
        kept = set()
        for c in hub.contracts.all():
            if c["id"] not in done or self.is_placeholder(c["path"]):
                continue
            names = [c["path"], posixpath.basename(c["path"])] + [e["name"] for e in c["exports"]]
            about = any(
                q["task"] in (c["id"], c["id"] + "-tests")
                or (q["example"]["path"] == c["path"] if q.get("example")  # an example names its own file
                    else any(n.lower() in f"{q['text']} {q['answer'] or ''}".lower() for n in names))
                for q in answers
            )
            if not about:
                kept.add(c["path"])
        return kept

    def example_verdicts(self):
        """Your answers about examples: (export, call, returns) -> RIGHT, WRONG or the value it should be."""
        return {(q["example"]["name"], q["example"]["call"], q["example"]["returns"]): q["answer"].strip()
                for q in self.hub.questions() if q.get("example") and q["status"] == "answered"}

    def check_examples(self, checks):
        """Ask you about the plan's new examples. Each is taken as right until you say otherwise."""
        asked = {q["text"] for q in self.hub.questions()}
        for contract, export, example in checks[:MAX_EXAMPLE_CHECKS]:
            text = f"Is this right? {example['call']} returns {example['returns']}"
            if text in asked:
                continue
            q = self.hub.ask(
                HUB_OWNER, text, critical=True, choice=RIGHT, options=EXAMPLE_OPTIONS,
                why=f"From {contract['path']}: {export['description']}",
                meta={"example": {"path": contract["path"], "name": export["name"], "call": example["call"],
                                  "returns": example["returns"]}},
            )
            self.events.emit("question", id=q["id"], text=q["text"], blocking=False)
            self.examples_asked = time.monotonic()

    def held_tests(self):
        """Tests tasks that wait for you to check their file's examples, for at most example_wait seconds."""
        wait = self.cfg.get("example_wait", 120)
        if self.examples_asked is None or time.monotonic() - self.examples_asked > wait:
            return set()
        paths = {q["example"]["path"] for q in self.hub.questions() if q.get("example") and q["status"] != "answered"}
        return {self.hub.contracts.get(p)["id"] + "-tests" for p in paths if self.hub.contracts.exists(p)}

    def apply_example_answers(self):
        """At the start of a round: an example you said is wrong is dropped, or corrected to your value."""
        hub = self.hub
        for q in hub.questions():
            if not q.get("example") or q["status"] != "answered" or q.get("acted"):
                continue
            hub.mark_acted(q["id"])
            answer, ex = q["answer"].strip(), q["example"]
            if answer == RIGHT:
                continue
            value = None if answer.lower() == WRONG.lower() else answer
            if value is not None and not self.stack.example_ok(ex["call"], value):
                value = literal_prefix(value) or value  # "[58, 57] (either order)" -> "[58, 57]"
            if value is not None and not self.stack.example_ok(ex["call"], value):
                self.events.emit("note", text=f"{value!r} isn't a value in {self.stack.lang_name}, so "
                                              f"{ex['call']} is dropped instead")
                value = None
            contract = hub.contracts.set_example(ex["path"], ex["name"], ex["call"], ex["returns"], value, self.number,
                                                 "you said an example was wrong")
            if contract is None:
                continue  # already changed since
            self.events.emit("note", text=f"{ex['path']}: {ex['call']} " + (
                f"now returns {value}, as you said" if value is not None else "is dropped, as you said it's wrong"))
            self.events.emit("contract", path=contract["path"], version=contract["version"], reason="an example you corrected")
            self.ensure_tasks(contract, "changed")

    def untangle(self, order=()):
        """Break circular imports. Returns the (file, import) pairs dropped.

        Imports must go one way, but small planners sometimes have two files import each
        other. Rather than refuse the whole plan, the import that closes each circle is
        dropped (the file keeps its purpose and exports), and the build tasks' dependencies
        are made to match the contracts again. Planners list the files others build on
        first, so of two files in a circle, the later one keeps its import of the earlier
        (`order` is the plan's order; without one, reverse alphabetical).
        """
        contracts = self.hub.contracts
        graph = {c["path"]: list(c["imports"]) for c in contracts.all()}
        dropped, state = [], {}

        def visit(path):
            state[path] = "visiting"
            for other in list(graph[path]):
                if state.get(other) == "visiting":
                    graph[path].remove(other)
                    dropped.append((path, other))
                elif other in graph and other not in state:
                    visit(other)
            state[path] = "done"

        listed = [p for p in order if p in graph]
        for path in list(reversed(listed)) + sorted((p for p in graph if p not in listed), reverse=True):
            if path not in state:
                visit(path)
        for path, other in dropped:
            contracts.set_imports(path, graph[path], self.number, f"dropped the import of {other}, which imports it back")
            self.events.emit("note", text=f"{path} and {other} imported each other, so {path} no longer imports {other}")
        if dropped:
            have = {t["id"] for t in self.hub.tasks()}
            for c in contracts.all():
                if c["id"] in have:
                    self.hub.set_depends(c["id"], [c["id"] + "-tests"] + [file_id(i) for i in c["imports"]])
        return dropped

    def ensure_tasks(self, contract, what):
        hub = self.hub
        build_id, test_id = contract["id"], contract["id"] + "-tests"
        depends = [test_id] + [file_id(p) for p in contract["imports"]]
        if what == "new":
            hub.add_task(test_id, f"Tests for {contract['path']}", "test", contract["path"], (), contract["purpose"])
            hub.add_task(build_id, f"Build {contract['path']}", "build", contract["path"], depends, contract["purpose"])
            return
        hub.set_depends(build_id, depends)
        for task_id in (test_id, build_id):
            self.redo(task_id, f"contract changed to v{contract['version']}")

    def ensure_design_task(self, changed):
        """The page starts as soon as there's a plan (it needs only the contracts), and is redone when one changes."""
        hub = self.hub
        if not any(t["id"] == DESIGN_TASK for t in hub.tasks()):
            hub.add_task(DESIGN_TASK, "Design the page", "build", "index.html", (), "index.html and styles.css")
            return
        hub.set_depends(DESIGN_TASK, [])  # projects planned before the page came first waited for every file
        if changed:
            self.redo(DESIGN_TASK, "the contracts changed")

    def logic_left(self):
        """Logic files that aren't done yet. While there are any, the page runs on placeholders."""
        return [t["module"] for t in self.hub.tasks() if t["kind"] == "build" and t["id"] != DESIGN_TASK
                and t["status"] != "done"]

    def write_placeholders(self):
        """A stand-in for every planned file that doesn't exist yet, so the page can load and be tried."""
        for contract in self.hub.contracts.all():
            path = self.hub.project / contract["path"]
            if not path.exists():
                self.hub._write_text(path, self.stack.placeholder(contract))

    def is_placeholder(self, path):
        file = self.hub.project / path
        return file.exists() and PLACEHOLDER in file.read_text(encoding="utf-8")[:300]

    def recheck_page(self):
        """Once every logic file is done, a page only checked against placeholders gets its full check."""
        if self.stack.name != "web" or self.logic_left():
            return False
        task = next((t for t in self.hub.tasks() if t["id"] == DESIGN_TASK), None)
        if not task or task["status"] != "done" or not task.get("soft"):
            return False
        self.hub.retry(DESIGN_TASK, "every logic file is built now: check the page works with them")
        return True

    def redo(self, task_id, reason):
        task = self.hub.task(task_id)
        if task.get("accepted"):
            self.hub.set_field(task_id, "accepted", False)  # a new contract is a new job
        if task["status"] == "failed":
            self.hub.restart(task_id, reason)  # a new contract is a new job: fresh tries
        elif task["status"] in ("done", "blocked"):
            self.hub.retry(task_id, reason)
        elif task["status"] == "todo":
            self.hub.note(task_id, reason)

    def write_packages(self):
        if self.stack.name != "python":
            return
        folders = {"tests"}
        for contract in self.hub.contracts.all():
            parts = contract["path"].split("/")[:-1]
            folders.update("/".join(parts[: i + 1]) for i in range(len(parts)))
        for folder in folders:
            init = self.hub.project / folder / "__init__.py"
            if not init.exists():
                self.hub._write_text(init, "")

    # Build

    async def build_pass(self, idea):
        limit = self.cfg["max_parallel_tasks"]
        running = {}
        while True:
            self.apply_example_answers()  # an example you checked while this runs counts before its tests are written
            self.apply_stuck_answers()
            hold = self.held_tests()
            free = limit - len(running)
            if free > 0:
                for task in self.hub.start_tasks(free, first=(DESIGN_TASK,), hold=hold):
                    running[task["id"]] = asyncio.ensure_future(self.run_task(task, idea))
            if not running:
                if hold and any(t["id"] in hold for t in self.hub.ready()):
                    if self.cancel and self.cancel():
                        raise Interrupted("stopped while waiting for you to check examples")
                    await asyncio.sleep(1)
                    continue
                break
            done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED, timeout=2 if hold else None)
            if not done:
                continue
            for task_id in [k for k, fut in running.items() if fut in done]:
                running.pop(task_id).result()  # run_task handles its own errors; this surfaces bugs
            self.apply_requests()
        self.commit(f"Round {self.number} build")

    async def run_task(self, task, idea):
        hub = self.hub
        self.events.emit("task_start", task=task["id"], kind=task["kind"], path=task["module"], attempt=task["attempts"])
        then = None
        try:
            if task["id"] == DESIGN_TASK:
                status, summary = await self.design(task, idea)
            elif task["kind"] == "test":
                status, summary = await self.write_tests(task, hub.contracts.get(task["module"]))
            else:
                status, summary, then = await self.build(task, hub.contracts.get(task["module"]))
        except LLMError as e:
            status, summary = "failed", f"a model call failed: {e}"
        hub.finish(task["id"], status, summary)
        if then:
            then()
        self.events.emit("task_end", task=task["id"], status=status, summary=summary)

    async def write_tests(self, task, contract):
        notes = list(task["notes"])
        brief = self.hub.contracts.brief(contract["path"])
        problem = ""
        for _ in range(2):
            text, cut = await agents.write_tests(
                self.sched, self.stack, brief, self.hub.memory.conventions(), contract["test_path"], notes, self.rules()
            )
            code = self.stack.repair_test(clean_code(text), [e["name"].split(".")[0] for e in contract["exports"]])
            if cut:
                problem, guarded = cut_off_note(contract["test_path"]), False
            else:
                problem, guarded = check_code(self.stack, code, contract["test_path"])
            if guarded:
                self.events.emit("blocked_code", task=task["id"], path=contract["test_path"], problems=problem)
            if not problem and not self.stack.test_imports_it(code, contract["path"]):
                problem = f"the tests never import {contract['path']}"
            if not problem:
                problem = self.stack.test_problem(code)
            if not problem:
                break
            notes.append(f"Your last test file was rejected: {problem}")
        else:
            return "failed", f"no usable test file: {problem}"
        code, added = self.with_examples(code, contract)
        self.hub._write_text(self.hub.project / contract["test_path"], code)
        return "done", f"wrote {contract['test_path']}" + (f", plus {added} contract example{'s' if added != 1 else ''}" if added else "")

    def with_examples(self, code, contract):
        """Add the contract's examples to the tests, so tester and builder are held to the same cases.

        Returns (code, how many examples were added). Examples that aren't valid code are left
        out, with a note, rather than breaking a test file that works.
        """
        cases = example_cases(contract)
        good = [c for c in cases if self.stack.example_ok(*c)]
        if len(good) < len(cases):
            self.events.emit("note", text=f"{contract['path']}: left out {len(cases) - len(good)} contract example(s) "
                                          "that aren't valid code")
        extra = self.stack.example_tests(contract, good)
        if not extra:
            return code, 0
        joined = code.rstrip() + "\n\n" + extra
        problem = self.stack.syntax_error(joined, contract["test_path"]) or "; ".join(self.stack.guard(joined))
        if problem:
            self.events.emit("note", text=f"{contract['path']}: its contract examples aren't valid code, so the tests "
                                          f"go without them ({problem[:160]})")
            return code, 0
        return joined, extra.count("assert")

    async def build(self, task, contract):
        """Run the builder agent for one file. Returns (status, summary, then).

        `then` runs after the task is finished, to send it back for another attempt.
        """
        hub, stack = self.hub, self.stack
        path, test_path = contract["path"], contract["test_path"]
        target = hub.project / path
        readable = [path, test_path] + contract["imports"]
        steps = self.cfg["agent_steps"]
        notes = list(task["notes"])
        if target.exists():
            notes.append(f"{path} already has a version from an earlier attempt; read it before rewriting it.")
        messages = agents.builder_conversation(
            stack, hub.contracts.brief(path), path, test_path, hub.memory.conventions(), hub.memory.lessons(),
            notes, readable, steps, self.rules(),
        )
        shim = self.pass_on(contract)
        if shim:
            hub._write_text(target, shim)
            passed, _ = await self.runner.run(test_path)
            if passed:
                return "done", f"{path} passes its tests by passing on its parts' work", None
        code = target.read_text(encoding="utf-8") if target.exists() else ""
        if code and not self.is_placeholder(path) and not check_code(stack, code, path, contract["exports"])[0]:
            passed, _ = await self.runner.run(test_path)  # sent back by a change elsewhere: maybe it still works
            if passed:
                return "done", f"{path} already passes its tests, so it's kept as it is", None
        passed, failure, writes = False, "", 0
        for step_no in range(1, steps + 1):
            step = await agents.next_step(self.sched, messages, f"step:{path}")
            action, arg, note = step["action"], norm(step["path"]), step["note"].strip()
            self.events.emit("agent_step", task=task["id"], step=step_no, action=action, path=arg, note=note[:200])
            messages.append({"role": "assistant", "content": json.dumps(step)})
            if action == "read":
                seen = self.read_for(arg, readable)
            elif action == "write":
                text, cut = await agents.write_owned_file(self.sched, stack, messages, path, f"build:{path}")
                new = clean_code(text)
                problem, guarded = (cut_off_note(path), False) if cut else check_code(stack, new, path, contract["exports"])
                if guarded:
                    self.events.emit("blocked_code", task=task["id"], path=path, problems=problem)
                if problem:
                    seen = f"Not written: {problem}"
                    failure = problem
                else:
                    hub._write_text(target, new)
                    code, writes = new, writes + 1
                    passed, output = await self.runner.run(test_path)  # every good write is tested at once
                    failure = "" if passed else output
                    seen = "Written, and all its tests pass." if passed else self.failed_tests(output, code)
            elif action == "test":
                if not code:
                    seen = f"Nothing to test yet: write {path} first."
                else:
                    passed, output = await self.runner.run(test_path)
                    failure = "" if passed else output
                    seen = "All tests pass." if passed else self.failed_tests(output, code)
            elif action == "finish":
                seen = "" if passed else f"You can't finish while the tests fail. {failure_digest(failure, 1500) or 'Write your file first.'}"
            else:  # ask: nobody waits for an answer; the file is left for you with the agent's question
                return (*self.hand_over(task, contract, code, note or "the builder says the contract can't work", failure),
                        None)
            if passed:
                break  # don't spend a model call on "finish" once the tests pass
            messages.append({"role": "user", "content": seen + "\nChoose your next action."})
            messages[2:] = messages[2:][-KEEP_TURNS:]
        await self.take_notes(task, contract, code, failure, learned=passed and writes > 1)
        if passed:
            return "done", f"{path} passes its tests" + (f" after {writes} writes" if writes > 1 else ""), None
        if not code:
            return "failed", "the agent never wrote a usable file", None
        return await self.after_failure(task, contract, code, failure)

    def pass_on(self, contract):
        """For a file split into parts that export everything it promises under the same names: the file that re-exports them."""
        pieces = [self.hub.contracts.get(p) for p in contract.get("split_into") or [] if self.hub.contracts.exists(p)]
        sources = {}
        for piece in pieces:
            for e in piece["exports"]:
                sources.setdefault(e["name"].split(".")[0], piece["path"])
        wanted = [e["name"].split(".")[0] for e in contract["exports"]]
        if not pieces or any(name not in sources for name in wanted):
            return None
        return self.stack.reexport(contract["path"], {name: sources[name] for name in wanted})

    def read_for(self, path, readable):
        if path not in readable:
            return f"You may not read {path or 'that'}. You may read: {', '.join(readable)}."
        file = self.hub.project / path
        if not file.exists():
            return f"{path} doesn't exist yet."
        return f"{path}:\n```\n{file.read_text(encoding='utf-8')[:6000]}\n```"

    @staticmethod
    def failed_tests(output, code):
        return f"Its tests fail:\n{failure_digest(output, 1800)}\n\nYour file now:\n```\n{code[:5000]}\n```"

    async def take_notes(self, task, contract, code, failure, learned):
        """Ask the builder what it decided and needs. Optional: a failed call only loses the notes."""
        if not code:
            return
        try:
            notes = await agents.file_notes(self.sched, self.hub.contracts.brief(contract["path"]), contract["path"], code, failure)
        except LLMError as e:
            self.events.emit("note", text=f"no notes for {contract['path']}: {e}")
            return
        if not failure:  # a failing file's "decisions" are its bugs, not choices you'd want to change
            had = sum(1 for q in self.hub.questions() if q["task"] == task["id"])
            self.record(task["id"], notes["decisions"][: max(0, MAX_DECISIONS - had)])
        self.requests.extend((task["id"], r) for r in notes["contract_requests"])
        if learned:
            self.hub.memory.add_lesson(notes["lesson"], contract["path"])

    async def after_failure(self, task, contract, code, failure):
        hub = self.hub
        test_file = hub.project / contract["test_path"]
        test_code = test_file.read_text(encoding="utf-8") if test_file.exists() else ""
        verdict, how = blame.settle(self.stack, contract, test_code, code, failure), "settled by the facts"
        if verdict is None:
            brief = hub.contracts.brief(contract["path"])
            verdict, how = await agents.review(self.sched, brief, test_code, code, failure_digest(failure, 2500),
                                               self.rules()), "reviewer"
            if verdict["blame"] == "contract" and not blame.quoted(verdict.get("quote", ""), brief):
                verdict = {"blame": "code", "reason": f"{verdict['reason'].strip()} (the reviewer blamed the contract "
                                                      "but quoted no line of it, so the code gets another try)"}
        reason = verdict["reason"].strip() or "no reason given"
        self.events.emit("note", text=f"review of {contract['path']} ({how}): blame {verdict['blame']}: {reason}")
        hub.set_field(task["id"], "blame", verdict["blame"])
        max_attempts = hub.config()["max_attempts"]
        test_id = contract["id"] + "-tests"
        test_task = hub.task(test_id)
        if verdict["blame"] == "test" and test_task["status"] == "done" and test_task["attempts"] < max_attempts:
            hub.reopen(test_id, f"review: {reason}")
            return "failed", f"its tests look wrong: {reason}", lambda: hub.retry(task["id"], f"tests redone: {reason}")
        if verdict["blame"] == "code":
            blamed = task.get("code_blamed", 0) + 1
            same = failure_signature(failure) == task.get("last_failure")
            hub.set_field(task["id"], "code_blamed", blamed)
            hub.set_field(task["id"], "last_failure", failure_signature(failure))
            if (blamed >= SPLIT_AFTER or same) and self.can_split(contract):
                split = await self.split(task, contract, code, failure, test_code)
                if split:
                    return split
            if task["attempts"] < max_attempts:
                return "failed", f"still failing: {reason}", lambda: hub.retry(task["id"], f"review: {reason}", use_attempt=True)
        if verdict["blame"] == "contract":  # the next plan revises it, and then the file gets fresh tries
            ask = hub.ask(HUB_OWNER, f"{contract['path']}'s contract can't work as written: {reason}", critical=True,
                          choice=REPLAN)
            hub.answer(ask["id"], REPLAN)
            self.events.emit("note", text=f"{contract['path']}: its contract goes back to the planner: {reason}")
        what = "its contract can't work as written" if verdict["blame"] == "contract" else "it keeps failing its tests"
        return (*self.hand_over(task, contract, code, f"{what}: {reason}", failure), None)

    # Splitting a file the builder can't get right

    def can_split(self, contract):
        """A file is split at most once, and a piece of a split file is never split again."""
        return not contract.get("split_into") and not contract.get("split_from")

    async def split(self, task, contract, code, failure, test_code):
        """Break a stuck file into 2 to 4 smaller files. Returns (status, summary, then), or None if it can't.

        The file keeps its exact contract (so nothing that uses it changes) and its tests,
        and is rebuilt as a thin file that imports the pieces. Each piece is a new file with
        its own contract, examples, tests and builder, in a folder named after the file (app/board_parts/).
        """
        hub, contracts = self.hub, self.hub.contracts
        path = contract["path"]
        folder = posixpath.splitext(path)[0] + "_parts"  # app/board.js -> app/board_parts (a folder named like
        # the file itself would hide it from Python's imports)
        try:
            reply = await agents.split(self.sched, self.stack, contracts.brief(path), folder, test_code, code, failure,
                                       self.rules())
        except LLMError as e:
            self.events.emit("note", text=f"couldn't split {path}: {e}")
            return None
        pieces = []
        for plan in reply["pieces"][:MAX_PIECES]:
            piece_path = tidy_path(plan["path"], self.stack)
            if not piece_path.startswith(folder + "/"):
                piece_path = f"{folder}/{posixpath.basename(piece_path)}"
            if piece_path == path or contracts.exists(piece_path):
                continue
            pieces.append({**plan, "path": piece_path, "imports": [tidy_path(i, self.stack) for i in plan["imports"]]})
        names = {e["name"].split(".")[0] for p in pieces for e in p["exports"]}
        pieces = [{**p, "exports": [{**e, "signature": unshadow(e["signature"], names)} for e in p["exports"]]}
                  for p in pieces]
        if len(pieces) < 2:
            self.events.emit("note", text=f"couldn't split {path}: the planner gave fewer than 2 usable pieces")
            return None
        made = []
        for plan in pieces:
            names = {p["path"] for p in pieces}
            plan["imports"] = [i for i in plan["imports"] if i in names and i != plan["path"]]
            try:
                piece, _ = contracts.upsert(plan, self.number, f"split from {path}")
            except (ContractError, ValueError) as e:
                self.events.emit("note", text=f"skipped a piece of {path}: {e}")
                continue
            contracts.mark(piece["path"], split_from=path)
            made.append(piece)
        if len(made) < 2:
            for piece in made:
                contracts.remove(piece["path"])
            return None
        self.untangle([p["path"] for p in made])
        for piece in made:
            self.ensure_tasks(contracts.get(piece["path"]), "new")
        self.write_packages()
        piece_paths = [p["path"] for p in made]
        contracts.set_imports(path, contract["imports"] + piece_paths, self.number, f"split into {', '.join(piece_paths)}")
        contracts.mark(path, split_into=piece_paths)
        self.events.emit("split", path=path, pieces=piece_paths, why=reply["why"].strip())
        why = reply["why"].strip() or "it was too much for one go"

        def rebuild():
            hub.restart(task["id"], f"split into {', '.join(piece_paths)} because {why}. Keep your contract exactly, "
                                    "and do the work by importing and calling the pieces.")
            hub.set_depends(task["id"], hub.task(task["id"])["depends_on"] + [p["id"] for p in made])

        return "failed", f"split into {len(made)} smaller files: {', '.join(piece_paths)}", rebuild

    # Files left for you

    def hand_over(self, task, contract, code, why, failure):
        """Leave a stuck file for you, and record that as a guess. Returns (status, summary).

        The file keeps its best attempt, with a comment at the top saying what it must do,
        how it's tested and why the models got stuck. Nothing waits: the task is marked
        failed (so only the files that need it wait), and the run goes on.
        """
        hub, path = self.hub, contract["path"]
        note = self.stack.comment(handover_lines(hub.contracts.brief(path), contract, why, failure, self.stack))
        body = strip_handover(code) if code else self.stack.comment(["Nothing here yet."])
        hub._write_text(hub.project / path, note + "\n" + body)
        question = hub.ask(
            task["id"], f"{path} is left for you: {why}", critical=True, choice=I_GOT_IT,
            why="The models couldn't get it to pass. Everything that doesn't need it was built anyway.",
            options=STUCK_OPTIONS,
        )
        self.events.emit("question", id=question["id"], text=question["text"], blocking=False)
        return "failed", f"left for you: {why}"

    async def revisit_stuck(self):
        """At the start of a round: files left for you that pass now are done; your answers take effect.

        Answers about a stuck file count whatever its task is doing now, so "I got it" on a
        file that went back to todo still keeps the builders off it.
        """
        hub = self.hub
        for task in hub.tasks():
            if task["kind"] != "build" or task["module"] == "index.html":
                continue
            contract = hub.contracts.get(task["module"])
            if task["status"] == "failed" and (hub.project / contract["path"]).exists() \
                    and (hub.project / contract["test_path"]).exists() and not self.is_placeholder(contract["path"]):
                passed, _ = await self.runner.run(contract["test_path"])
                if passed:
                    hub.mark_done(task["id"], f"{contract['path']} passes its tests now (finished by you)")
                    self.events.emit("note", text=f"{contract['path']} passes its tests now: marked done")
                    for q in hub.questions():  # what you said about it while it was stuck no longer applies
                        if q["task"] == task["id"] and q["status"] == "answered" and not q.get("acted"):
                            hub.mark_acted(q["id"])
        self.apply_stuck_answers()

    def apply_stuck_answers(self):
        """Your answers about stuck files take effect: at the start of a round, and between tasks while one runs."""
        hub = self.hub
        limit = hub.config()["max_attempts"]
        for task in hub.tasks():
            if task["kind"] != "build" or task["module"] == "index.html" or task["status"] == "in_progress":
                continue
            contract = hub.contracts.get(task["module"])
            for q in hub.questions():
                if q["task"] != task["id"] or q["status"] != "answered" or q.get("acted"):
                    continue
                hub.mark_acted(q["id"])
                answer = q["answer"]
                if answer == IT_WORKS:
                    hub.mark_done(task["id"], f"{contract['path']} works as it is (you said so)")
                    hub.set_field(task["id"], "accepted", True)
                    self.events.emit("note", text=f"{contract['path']}: done, because you said it works as it is")
                    break
                if answer in KEEP_ANSWERS:
                    hub.set_field(task["id"], "status", "failed")
                    hub.set_field(task["id"], "summary", f"left for you: you said {answer}")
                    hub.set_field(task["id"], "attempts", limit)  # no builder takes it up again
                    self.events.emit("note", text=f"{contract['path']}: left for you, as you said")
                    continue
                contract_wrong = answer == SIMPLIFY or (answer == TRY_AGAIN and task.get("blame") == "contract")
                if contract_wrong:  # the plan changes the contract, and the tests are written again
                    how = ("Simplify it: trim what it promises so it can work and be tested as written"
                           if answer == SIMPLIFY else "Its contract was to blame: fix the contract so it can work")
                    ask = hub.ask(HUB_OWNER, f"{contract['path']} is stuck: {q['text'][:300]}", critical=True, choice=how)
                    hub.answer(ask["id"], how)
                    self.redo(contract["id"] + "-tests", f"you said: {answer}")
                hub.restart(task["id"], f"you said: {answer}")
                self.events.emit("note", text=f"{contract['path']}: " + (
                    "back to the planner, with new tests, because you said: " if contract_wrong else "trying again, because you said: ")
                    + answer)

    # Design (web stack)

    async def design(self, task, idea):
        """Make the page, or change a working one. Every fix after the first draft rewrites one part only."""
        hub = self.hub
        for name, text in self.stack.scaffold(hub.project).items():
            if not (hub.project / name).exists():
                hub._write_text(hub.project / name, text)  # projects made before the kit existed
        notes = list(task["notes"])
        tries = self.cfg["design_tries"]
        self.write_placeholders()
        html, css = self.read_page()
        if html and not self.page_problem(html) and task.get("soft") and not self.logic_left():
            return await self.settle_page(html, css, notes, tries)  # check it first; fix only what fails
        if html and not self.page_problem(html):
            why = "; ".join(n.removeprefix("retry: ") for n in notes[-3:]) or "make the page better"
            picked = await agents.pick_page_part(self.sched, html, why)
            if picked["part"] != "whole":
                self.events.emit("page_fix", part=picked["part"], reason=f"{why} ({picked['why']})")
                status, summary = await self.settle_page(html, css, notes, tries, part=picked["part"], why=why)
                if status != "done":
                    hub._write_text(hub.project / "index.html", html)  # put the working page back
                    hub._write_text(hub.project / "styles.css", css)
                return status, summary
            self.events.emit("page_fix", part="whole", reason=f"{why} ({picked['why']})")
        problem = ""
        for attempt in range(1, tries + 1):
            text, cut = await agents.design_page(self.sched, idea, self.contracts_md(), hub.memory.conventions(), notes)
            html = clean_code(text)
            problem = cut_off_note("index.html") if cut else self.page_problem(html)
            if problem:
                self.events.emit("note", text=f"page draft {attempt} rejected: {problem}")
                notes.append(f"Draft {attempt} was rejected: {problem}")
                continue
            text, cut = await agents.design_styles(self.sched, html, notes)
            css, problem = self.checked_styles(text, cut)
            if problem:
                self.events.emit("note", text=f"styles.css left empty: {problem}")  # the kit still styles the page
                css = ""
            return await self.settle_page(html, css, notes, tries - attempt)
        return "failed", f"no working page after {tries} tries: {problem}"

    async def settle_page(self, html, css, notes, fixes, part=None, why=""):
        """Write the page and check it; after a failure, rewrite only the part the check flagged.

        With `part`, that part is rewritten first, for `why`. Problems the check can name
        exactly (hidden results, example numbers) are repaired without a model call.
        """
        hub = self.hub
        screen = hub.root / "screens" / f"round-{self.number}.png"
        problem = why
        for attempt in range(fixes + 1):
            if part:
                html, css, bad = await self.fix_part(part, html, css, problem, notes)
                if bad:
                    problem = bad
                    notes.append(f"Your {part} was rejected: {bad}")
                    self.events.emit("note", text=f"page {part} fix rejected: {bad}")
                    continue
            self.write_page(html, css)
            ok, problem = await self.runner.check_page(screenshot=screen)
            check = getattr(self.runner, "last_check", None) or {}
            if not ok:
                repaired, what = pages.repair(html, check)
                if what:
                    self.events.emit("page_fix", part="markup", reason=f"repaired without a model: {what}")
                    html = repaired
                    self.write_page(html, css)
                    ok, problem = await self.runner.check_page(screenshot=screen)
                    check = getattr(self.runner, "last_check", None) or {}
            left = self.logic_left()
            if not ok and left and check.get("probe") and check["probe"].get("ready"):
                # It loads; what it shows can't be judged until the files behind it are built.
                self.hub.set_field(DESIGN_TASK, "soft", True)
                self.events.emit("note", text=f"the page loads; it gets its full check once these are built: {', '.join(left)}")
                return "done", "index.html loads, running on placeholders until the logic files are built"
            if ok:
                unused = self.unused_files(html)
                if unused:
                    self.events.emit("note", text=f"the page doesn't use {', '.join(unused)}; if that's a feature, "
                                                  "it isn't on the page yet")
                self.hub.set_field(DESIGN_TASK, "soft", bool(left))
                self.page_checked = not left
                fixed = f" after {attempt} fix{'es' if attempt != 1 else ''}" if attempt else ""
                return "done", "index.html and styles.css work in the browser" + fixed + (f" ({problem})" if problem else "")
            part = check.get("part") or "script"
            notes.append(f"The page check failed: {problem}")
            self.events.emit("note", text=f"page check failed: {problem}")
            if attempt < fixes:
                self.events.emit("page_fix", part=part, reason=problem)
        return "failed", f"the page still fails its check: {problem}"

    async def fix_part(self, part, html, css, problem, notes):
        """Rewrite one part. Returns (html, css, problem): the page unchanged and a problem if the fix is unusable."""
        if pages.get(part, html, css) is None:
            return html, css, f"the page has no {part} in the expected shape"
        text, cut = await agents.fix_page_part(self.sched, part, html, css, problem, self.contracts_md(), notes)
        if part == "styles":
            new_css, bad = self.checked_styles(text, cut)
            return html, (css if bad else new_css), bad
        if cut:
            return html, css, cut_off_note(part)
        candidate = pages.put(part, html, clean_code(text))
        if candidate is None:
            return html, css, f"write {agents.PART_WHAT[part]}"
        bad = self.page_problem(candidate)
        return (html, css, bad) if bad else (candidate, css, "")

    def checked_styles(self, text, cut):
        css, dropped = filter_styles(clean_code(text))
        if dropped:
            self.events.emit("note", text=f"styles.css: dropped rules that restyled the kit: {', '.join(dropped[:8])}")
        if cut:
            return css, cut_off_note("styles.css")
        guarded = self.stack.guard(css)
        return css, (f"styles.css was rejected by the safety guard: {'; '.join(guarded)}" if guarded else "")

    def read_page(self):
        project = self.hub.project
        read = lambda name: (project / name).read_text(encoding="utf-8") if (project / name).exists() else ""
        return read("index.html"), read("styles.css")

    def write_page(self, html, css):
        self.hub._write_text(self.hub.project / "index.html", html)
        self.hub._write_text(self.hub.project / "styles.css", css)

    def page_problem(self, html):
        guarded = self.stack.guard(html)
        if guarded:
            return f"rejected by the safety guard: {'; '.join(guarded)}"
        if "kit.css" not in html:
            return 'the page must link kit.css: <link rel="stylesheet" href="kit.css">'
        if 'type="module"' not in html:
            return 'the page needs one <script type="module">'
        if "dataset.ready" not in html:
            return 'the script must end with document.body.dataset.ready = "1";'
        inline = sorted(set(INLINE_HANDLER.findall(html)))
        if inline:
            return (f"the page uses inline handlers ({', '.join(inline)}=\"...\"), which can't reach functions in a "
                    "<script type=\"module\">: give the element an id and use addEventListener in the script")
        missing = pages.missing_targets(html)
        if missing:
            return (f"the script looks for {', '.join(missing[:5])}, but nothing in the markup matches: add the "
                    "element, or look up one that's there by its id")
        exports = {c["path"]: {e["name"] for e in c["exports"]} for c in self.hub.contracts.all()}
        imports = PAGE_IMPORT.findall(html)
        if not imports:
            return "the page script imports nothing from ./app/"
        for names, path in imports:
            if path not in exports:
                return f"the page imports ./{path}, which doesn't exist; files: {', '.join(sorted(exports))}"
            wanted = {n.split(" as ")[0].strip() for n in names.split(",") if n.strip()}
            missing = sorted(wanted - exports[path])
            if missing:
                return f"./{path} doesn't export {', '.join(missing)}; it exports {', '.join(sorted(exports[path]))}"
        return ""

    def unused_files(self, html):
        """Logic files nothing uses: not the page, not another file. Said to the designer, not held against the page,
        since a plan can have two files that do the same job."""
        used = {p for _, p in PAGE_IMPORT.findall(html)}
        contracts = self.hub.contracts.all()
        return sorted(c["path"] for c in contracts if not c.get("split_from") and c["path"] not in used
                      and not any(c["path"] in other["imports"] for other in contracts))

    # Notes, decisions and contract requests

    def record(self, task_id, decisions):
        for decision in decisions[:MAX_DECISIONS]:
            if decision["text"].strip() and decision["choice"].strip() and not self.already_settled(decision):
                self.decide(task_id, decision)

    def already_settled(self, guess):
        """Whether a guess or decision repeats one of your answers or the brief's rules, in other words."""
        answered = [q for q in self.hub.questions() if q["status"] == "answered" and not q.get("example")]
        brief = project.load_brief(self.hub) or {}
        options = [o.get("label", "") if isinstance(o, dict) else o for o in guess.get("options", [])]
        repeat = settled(guess["text"], options, answered, brief.get("rules", []))
        choice = (guess.get("choice") or "").strip()
        if not repeat and choice:  # "Rounding Rule" says little; its pick "Round to the Nearest Dollar" says it all
            repeat = settled(choice, [], answered, brief.get("rules", []))
        if repeat:
            self.events.emit("note", text=f"not asking \u201c{guess['text'][:80]}\u201d: you settled it already")
        return bool(repeat)

    def decide(self, owner, decision, critical=False):
        try:
            q = self.hub.ask(
                owner,
                decision["text"],
                critical=critical,
                choice=decision["choice"].strip() or "(not given)",
                why=decision.get("why", ""),
                options=[o for o in decision.get("options", []) if (o.get("label", "") if isinstance(o, dict) else o).strip()],
            )
        except HubError as e:
            self.events.emit("note", text=f"couldn't record a decision: {e}")
            return
        self.events.emit("question", id=q["id"], text=q["text"], blocking=False)

    def apply_requests(self):
        """Apply what builders asked other files to export. Adding is safe, so it happens right away."""
        hub, contracts = self.hub, self.hub.contracts
        requests, self.requests = self.requests, []
        for task_id, request in requests:
            target = norm(request["path"])
            names = [e for e in request["add_exports"] if e["name"].strip()]
            if not names or not contracts.exists(target):
                continue
            asker = contracts.get(hub.task(task_id)["module"])
            if target == asker["path"]:
                continue
            if target in contracts.dependents(asker["path"]):
                self.events.emit("note", text=f"{task_id} asked {target} for exports, but that would make a circle")
                continue
            contract, added = contracts.add_exports(target, names, self.number, f"{task_id} asked: {request['why']}")
            if not added:
                continue
            self.events.emit("contract", path=target, version=contract["version"], reason=f"{task_id} asked for {', '.join(added)}")
            self.decide(task_id, {"text": f"Add {', '.join(added)} to {target}", "choice": "added", "why": request["why"]})
            for redo_id in (contract["id"] + "-tests", contract["id"]):
                self.redo(redo_id, f"contract v{contract['version']} adds {', '.join(added)}")
            if target not in asker["imports"]:
                contracts.set_imports(asker["path"], asker["imports"] + [target], self.number, f"now uses {target}")
                hub.set_depends(asker["id"], hub.task(asker["id"])["depends_on"] + [contract["id"]])

    # Verify and report

    async def verify(self):
        """Run every built file's tests, a few at a time. Returns (all passed, failing contracts, output)."""
        project = self.hub.project
        built = [c for c in self.hub.contracts.all() if (project / c["path"]).exists() and (project / c["test_path"]).exists()]
        gate = asyncio.Semaphore(VERIFY_PARALLEL)

        async def one(contract):
            async with gate:
                return contract, await self.runner.run(contract["test_path"])

        results = await asyncio.gather(*(one(c) for c in built))
        for c, (ok, _) in results:  # a file left for you that passes now (you finished it) is done
            if ok and self.hub.task(c["id"])["status"] == "failed" and not self.is_placeholder(c["path"]):
                self.hub.mark_done(c["id"], f"{c['path']} passes its tests now (finished by you)")
        accepted = {t["id"] for t in self.hub.tasks() if t.get("accepted")}  # you said they work as they are
        failing = [c for c, (ok, _) in results if not ok and c["id"] not in accepted]
        output = "\n".join(f"{c['test_path']}:\n{failure_digest(out, 600)}" for c, (ok, out) in results
                            if not ok and c["id"] not in accepted)
        everything = len(built) == len(self.hub.contracts.all())
        waived = sum(1 for c, (ok, _) in results if not ok and c["id"] in accepted)
        self.events.emit("note", text=f"verify: {len(built) - len(failing) - waived} of {len(self.hub.contracts.all())} "
                                      "files pass" + (f", and {waived} you said work as they are" if waived else ""))
        return bool(built) and not failing and everything, failing, output

    def reopen(self, failing):
        reopened = []
        for contract in failing:
            if self.hub.task(contract["id"])["status"] == "done":
                self.hub.reopen(contract["id"], f"{contract['test_path']} fails when run on its own")
                reopened.append(contract["id"])
        return reopened

    async def report_notes(self, passed, output):
        tasks = self.hub.tasks()
        facts = "\n".join(f"- {t['id']} ({t['kind']}): {t['status']}. {t['summary'] or ''}" for t in tasks)
        facts += f"\n\nAll tests: {'pass' if passed else 'some fail or are missing'}."
        if output:
            facts += "\nFailures:\n" + output[-800:]
        try:
            return (await agents.report(self.sched, facts))["notes"]
        except LLMError:
            done = sum(t["status"] == "done" for t in tasks)
            return f"{done} of {len(tasks)} tasks are done. All tests {'pass' if passed else 'do not pass yet'}."

    def commit(self, message):
        commit(self.hub.project, message)


def run_rounds(hub, llm, rounds, echo=None, stop=lambda: False, cancel=None):
    """Run up to `rounds` rounds in a row, keeping the Mac awake. Returns (summaries, why it stopped).

    Nothing stops to ask you: stuck files are left for you and recorded as guesses, and
    questions pile up for whenever you look. It stops when the planner says the idea is
    covered and every test passes, when the next round would have nothing to work on (what's
    left waits on a file left for you, or the round made no progress), when stop() says so
    (checked between rounds), or partway through a round when cancel() does; the files a
    stopped round was on go back to todo. It sends one notification saying how it went.
    """
    summaries, why = [], f"ran the {rounds} round{'s' if rounds != 1 else ''} asked for"
    with awake():
        for _ in range(rounds):
            before = sum(t["status"] == "done" for t in hub.tasks())
            try:
                summary = asyncio.run(Round(hub, llm, echo=echo, cancel=cancel).run())
            except Interrupted:
                hub.release()
                hub.events.emit("round_end", summary={"round": hub.config()["round"], "stopped": True})
                why = "you stopped it partway through a round"
                break
            summaries.append(summary)
            if summary["complete"] and not workable(hub) and summary["tests_passed"]:
                why = "the planner says the idea is covered and every test passes"
                break
            waiting = any(t["status"] == "todo" for t in hub.tasks())  # todo but not ready: waits on a stuck file
            progress = sum(t["status"] == "done" for t in hub.tasks()) > before
            if not workable(hub) and (waiting or not progress):  # with neither, the next plan may add files
                stuck = [t["module"] for t in hub.tasks() if t["status"] == "failed" and t["kind"] == "build"]
                why = "nothing is left that it can work on" + (
                    f": what's left waits on {', '.join(stuck)}, left for you" if stuck else "")
                break
            if stop():
                why = "you asked it to stop"
                break
    notify(run_summary(hub, summaries, why), f"Roughcut Local: {hub.project.name}")
    return summaries, why


def workable(hub):
    """Whether a next round has something to do: a task that's ready, or a failed one with tries left."""
    limit = hub.config()["max_attempts"]
    return bool(hub.ready()) or any(t["status"] == "failed" and t["attempts"] < limit for t in hub.tasks())


def run_summary(hub, summaries, why):
    """One line on how a run went, for the notification and the studio's chat."""
    left = [t["module"] for t in hub.tasks() if t["status"] == "failed" and t["kind"] == "build"]
    status = hub.status()
    passed = bool(summaries) and summaries[-1]["tests_passed"]
    parts = [f"{len(summaries)} round{'s' if len(summaries) != 1 else ''}, stopped because {why}.",
             "Every test passes." if passed else "Not every test passes yet."]
    if left:
        parts.append(f"Left for you: {', '.join(left)}.")
    if status["guesses_to_confirm"]:
        parts.append(f"{len(status['guesses_to_confirm'])} guess(es) to look at.")
    return " ".join(parts)


def handover_lines(brief, contract, why, failure, stack):
    """What a file left for you must do, as comment lines."""
    tail = [l for l in failure_digest(failure or "", 1200).splitlines() if l.strip()][:10]
    lines = [HANDOVER, "The models couldn't get this file to pass its tests, so it's yours to finish.", ""]
    lines += brief.splitlines() + [""]
    lines += [f"Its tests: {contract['test_path']}   (run: {stack.test_hint(contract['test_path'])})"]
    lines += [f"Why it's stuck: {why}"]
    if tail:
        lines += ["Last failure:"] + [f"  {l[:160]}" for l in tail]
    lines += ["", "Once its tests pass, the next round marks it done (or answer \"It works\" if it does what you want",
              "as it is). Delete this note whenever you like.", HANDOVER_END]
    return lines


HANDOVER = "ROUGHCUT: LEFT FOR YOU"
HANDOVER_END = "END OF ROUGHCUT NOTE"


def failure_signature(output):
    """What failed, without the noise: the names of the failing tests, so the same failure twice is noticed."""
    names = re.findall(r"^\s*(?:✖|not ok \d+ -|FAIL:|ERROR:)\s*(.+?)\s*(?:\(\d[\d.]*m?s\))?$", output or "", re.M)
    return "; ".join(sorted(set(n.strip() for n in names))) or (output or "").strip()[-200:]


def strip_handover(code):
    """The code without an earlier hand-over note (first line to last line of it), so notes don't pile up."""
    lines = code.splitlines()
    first = next((i for i, l in enumerate(lines) if HANDOVER in l), None)
    last = next((i for i, l in reversed(list(enumerate(lines))) if HANDOVER_END in l), None)
    if first is not None and last is not None and last >= first:
        lines = lines[:first] + lines[last + 1:]
    return "\n".join(lines).strip() + "\n"


def commit(folder, message):
    """Commit everything in the project, so every step can be looked at and undone."""
    git = ["git", "-C", str(folder)]
    subprocess.run(git + ["add", "-A"], capture_output=True)
    result = subprocess.run(git + ["commit", "-q", "-m", message], capture_output=True, text=True)
    if result.returncode != 0 and "user.email" in (result.stderr + result.stdout):
        identity = ["-c", "user.name=roughcut-local", "-c", "user.email=roughcut-local@localhost"]
        subprocess.run(git[:3] + identity + ["commit", "-q", "-m", message], capture_output=True)


def unshadow(signature, names):
    """A signature whose parameters don't hide `names` (functions it imports): splitByPercentage -> useSplitByPercentage.

    A parameter with an imported function's name makes every call of that function inside
    call the parameter instead, and small builders follow a contract's names exactly.
    """
    m = re.match(r"^(\s*(?:async\s+)?(?:function|def)\s+[A-Za-z_$][\w$]*\s*\()([^)]*)(\).*)$", signature, re.S)
    if not m or not names:
        return signature
    python = signature.lstrip().startswith("def")

    def rename(part):
        name = re.match(r"\s*([A-Za-z_$][\w$]*)", part)
        if not name or name.group(1) not in names:
            return part
        new = f"use_{name.group(1)}" if python else "use" + name.group(1)[:1].upper() + name.group(1)[1:]
        return part.replace(name.group(1), new, 1)

    return m.group(1) + ",".join(rename(p) for p in m.group(2).split(",")) + m.group(3)


def is_stuck_question(q):
    """A question about a file left for you: its answer is acted on by revisit_stuck, not taken in by a plan."""
    return q["task"] != HUB_OWNER and {I_GOT_IT, IT_WORKS} <= set(q.get("options") or [])
