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
contract is wrong. On the web stack, a designer then makes index.html and styles.css,
and the page is loaded in headless Chrome to check that it works.
"""

from __future__ import annotations

import asyncio
import json
import posixpath
import re
import subprocess
import time

from . import agents, pages, project
from .contracts import ContractError, file_id
from .hub_local import HUB_OWNER, HubError
from .llm import LLMError
from .notify import awake, notify
from .runner import Runner
from .scheduler import Scheduler
from .stacks import filter_styles
from .users import TestUsers

OPEN_FENCE = re.compile(r"^\s*```[a-zA-Z]*[ \t]*\n")
CLOSE_FENCE = re.compile(r"\n?```\s*$")
PAGE_IMPORT = re.compile(r"""import\s*\{([^}]*)\}\s*from\s*['"]\./(app/[^'"]+)['"]""")
MAX_DECISIONS = 2  # per task; small models over-report
MAX_GUESSES = 3  # per plan, so the questions you get are few and real
# What you can say when a file is stuck. The first is what happens if you don't answer in time.
STUCK_OPTIONS = [
    {"label": "Simplify it", "detail": "Make this file do less, so it can pass: the planner trims what it promises next round."},
    {"label": "Change the contract", "detail": "Tell the planner what this file should promise instead, in your own words."},
    {"label": "Drop the file", "detail": "Take it out of the plan; anything that needs it is planned again without it."},
]
KEEP_TURNS = 12  # agent messages kept after the opening two, so its context stays small
DESIGN_TASK = "page-design"
# On the web stack the page's own script is the UI; a logic file by one of these names would need the DOM.
UI_NAMES = {"ui", "dom", "view", "views", "page", "index", "main", "app", "display", "render", "renderer", "interface", "screen"}
VERIFY_PARALLEL = 3


def clean_code(text):
    """The file inside a code reply. Generation stops at the closing fence, so it is usually gone already."""
    text = OPEN_FENCE.sub("", text, count=1)
    return CLOSE_FENCE.sub("", text).strip() + "\n"


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
    return "", False


class Round:
    def __init__(self, hub, llm, echo=None):
        self.hub = hub
        self.cfg = hub.local_config()
        self.stack = hub.stack
        self.events = hub.events
        self.events.echo = echo
        self.sched = Scheduler(llm, self.cfg, self.events)
        self.runner = Runner(hub.project, self.stack, self.cfg["test_timeout"], self.cfg["sandbox"], self.events)
        self.requests = []  # (task id, contract request), applied between tasks
        self.number = None
        self.page_checked = False  # a page passed its check this round, so the test users try it

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
        for task_id in hub.retry_failed():
            self.events.emit("note", text=f"retrying {task_id}, which failed last round")

        self.events.emit("phase", name="plan")
        complete = await self.plan(idea)
        self.commit(f"Round {self.number} plan")

        self.events.emit("phase", name="build")
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
            "complete": complete,
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
        answers = hub.answered_since(self.number)
        has_todo = any(t["status"] == "todo" for t in hub.tasks())
        if not (first or answers or not has_todo):
            self.events.emit("note", text="unfinished tasks and no new answers, so the plan stays as it is")
            return None
        reply = await agents.plan(
            self.sched, self.stack, idea, hub.memory.conventions(), self.contracts_md(), answers, first
        )
        skipped = self.apply_plan(reply)
        if first and not hub.contracts.all():  # nothing usable: one more try, told why
            why = "; ".join(skipped) or "the plan had no files"
            self.events.emit("note", text=f"the plan had no usable files ({why}); asking again")
            retry = [{"text": "Your last plan was rejected", "answer": f"{why}. Plan again."}]
            reply = await agents.plan(self.sched, self.stack, idea, hub.memory.conventions(), "", answers + retry, first)
            self.apply_plan(reply)
        return reply["complete"]

    def contracts_md(self):
        return "\n\n".join(self.hub.contracts.describe(c) for c in self.hub.contracts.all())

    def apply_plan(self, reply):
        hub, contracts = self.hub, self.hub.contracts
        hub.memory.set_conventions(reply["conventions"])
        asked = {q["text"] for q in hub.questions()}
        for guess in [g for g in reply["guesses"] if g["text"].strip() and g["text"].strip() not in asked][:MAX_GUESSES]:
            self.decide(HUB_OWNER, guess, critical=True)
        planned, skipped = [], []
        for file_plan in reply["files"]:
            tidy = {**file_plan, "path": tidy_path(file_plan["path"], self.stack),
                    "imports": [tidy_path(p, self.stack) for p in file_plan["imports"] if p.strip()]}
            if tidy["path"] != file_plan["path"]:
                self.events.emit("note", text=f"renamed {file_plan['path']} to {tidy['path']} to fit the layout")
            stem = posixpath.splitext(posixpath.basename(tidy["path"]))[0]
            if self.stack.name == "web" and stem in UI_NAMES:
                why = f"{tidy['path']} would be UI code, which belongs in the page's own script (the designer writes it)"
                self.events.emit("note", text=f"skipped a planned file: {why}")
                skipped.append(why)
                continue
            try:
                contract, what = contracts.upsert(tidy, self.number, f"round {self.number} plan")
            except (ContractError, ValueError) as e:
                self.events.emit("note", text=f"skipped a planned file: {e}")
                skipped.append(str(e))
                continue
            planned.append((contract, what))
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
        if self.stack.name == "web" and contracts.all():
            self.ensure_design_task(changed)
        self.write_packages()
        errors = hub.check()
        if errors:
            raise HubError("the plan's task graph is invalid: " + "; ".join(errors))
        return skipped

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
        """The page waits for every logic file, and is redone when any contract changes."""
        hub = self.hub
        build_ids = [c["id"] for c in hub.contracts.all()]
        if not any(t["id"] == DESIGN_TASK for t in hub.tasks()):
            hub.add_task(DESIGN_TASK, "Design the page", "build", "index.html", build_ids, "index.html and styles.css")
            return
        hub.set_depends(DESIGN_TASK, build_ids)
        if changed:
            self.redo(DESIGN_TASK, "the contracts changed")

    def redo(self, task_id, reason):
        task = self.hub.task(task_id)
        if task["status"] in ("done", "failed", "blocked"):
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
            free = limit - len(running)
            if free > 0:
                for task in self.hub.start_tasks(free):
                    running[task["id"]] = asyncio.ensure_future(self.run_task(task, idea))
            if not running:
                break
            done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
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
                self.sched, self.stack, brief, self.hub.memory.conventions(), contract["test_path"], notes
            )
            code = self.stack.repair_test(clean_code(text))
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
        extra = self.stack.example_tests(contract)
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
            notes, readable, steps,
        )
        code = target.read_text(encoding="utf-8") if target.exists() else ""
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
                seen = "" if passed else f"You can't finish while the tests fail. {failure[-1500:] or 'Write your file first.'}"
            else:  # ask
                question = hub.ask(
                    task["id"], note or f"{path} can't keep its contract. What should change?", critical=True,
                    options=STUCK_OPTIONS, suggested=STUCK_OPTIONS[0]["label"],
                )
                self.events.emit("question", id=question["id"], text=question["text"], blocking=True)
                return "blocked", f"asked you: {question['text']}", None
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

    def read_for(self, path, readable):
        if path not in readable:
            return f"You may not read {path or 'that'}. You may read: {', '.join(readable)}."
        file = self.hub.project / path
        if not file.exists():
            return f"{path} doesn't exist yet."
        return f"{path}:\n```\n{file.read_text(encoding='utf-8')[:6000]}\n```"

    @staticmethod
    def failed_tests(output, code):
        return f"Its tests fail:\n{output[-1800:]}\n\nYour file now:\n```\n{code[:5000]}\n```"

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
        verdict = await agents.review(self.sched, hub.contracts.brief(contract["path"]), test_code, code, failure)
        reason = verdict["reason"].strip() or "no reason given"
        self.events.emit("note", text=f"review of {contract['path']}: blame {verdict['blame']}: {reason}")
        max_attempts = hub.config()["max_attempts"]
        test_id = contract["id"] + "-tests"
        test_task = hub.task(test_id)
        if verdict["blame"] == "test" and test_task["status"] == "done" and test_task["attempts"] < max_attempts:
            hub.reopen(test_id, f"review: {reason}")
            return "failed", f"its tests look wrong: {reason}", lambda: hub.retry(task["id"], f"tests redone: {reason}")
        if verdict["blame"] == "code" and task["attempts"] < max_attempts:
            return "failed", f"still failing: {reason}", lambda: hub.retry(task["id"], f"review: {reason}", use_attempt=True)
        what = "its contract can't work as written" if verdict["blame"] == "contract" else "it keeps failing its tests"
        question = hub.ask(
            task["id"],
            f"{contract['path']}: {what} ({reason}). What should change?",
            critical=True,
            options=STUCK_OPTIONS, suggested=STUCK_OPTIONS[0]["label"],
        )
        self.events.emit("question", id=question["id"], text=question["text"], blocking=True)
        return "blocked", f"needs your answer: {reason}", None

    # Design (web stack)

    async def design(self, task, idea):
        """Make the page, or change a working one. Every fix after the first draft rewrites one part only."""
        hub = self.hub
        for name, text in self.stack.scaffold(hub.project).items():
            if not (hub.project / name).exists():
                hub._write_text(hub.project / name, text)  # projects made before the kit existed
        notes = list(task["notes"])
        tries = self.cfg["design_tries"]
        html, css = self.read_page()
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
            if ok:
                self.page_checked = True
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

    # Notes, decisions and contract requests

    def record(self, task_id, decisions):
        for decision in decisions[:MAX_DECISIONS]:
            if decision["text"].strip() and decision["choice"].strip():
                self.decide(task_id, decision)

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
        failing = [c for c, (ok, _) in results if not ok]
        output = "\n".join(f"{c['test_path']}:\n{out[-600:]}" for c, (ok, out) in results if not ok)
        everything = len(built) == len(self.hub.contracts.all())
        self.events.emit("note", text=f"verify: {len(built) - len(failing)} of {len(self.hub.contracts.all())} files pass")
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


def run_rounds(hub, llm, rounds, echo=None, stop=lambda: False, wait=None):
    """Run up to `rounds` rounds in a row, keeping the Mac awake. Returns (summaries, why it stopped).

    When a round ends with questions that block work, you get a notification and the run
    waits up to `wait` seconds (the config's `answer_wait` if None; 0 stops instead) for
    your answers, then takes each question's suggested answer and goes on. It also stops
    when the planner says the idea is covered and every test passes, when a round makes no
    progress, or when stop() says so (checked between rounds and while waiting).
    """
    wait = hub.local_config()["answer_wait"] if wait is None else wait
    summaries = []
    with awake():
        for n in range(rounds):
            before = sum(t["status"] == "done" for t in hub.tasks())
            summary = asyncio.run(Round(hub, llm, echo=echo).run())
            summaries.append(summary)
            after = sum(t["status"] == "done" for t in hub.tasks())
            todo = any(t["status"] == "todo" for t in hub.tasks())
            if summary["open_questions"]:
                if not wait or n == rounds - 1:
                    return summaries, "some questions need your answer"
                how = wait_for_answers(hub, summary["open_questions"], wait, stop)
                if how == "stopped":
                    return summaries, "you asked it to stop"
                continue
            if summary["complete"] and not todo and summary["tests_passed"]:
                return summaries, "the planner says the idea is covered and every test passes"
            if after == before and not todo:
                return summaries, "the last round made no progress"
            if stop():
                return summaries, "you asked it to stop"
    return summaries, f"ran the {rounds} round{'s' if rounds != 1 else ''} asked for"


def wait_for_answers(hub, question_ids, seconds, stop=lambda: False, poll=2.0):
    """Wait for blocking questions to be answered. Returns "answered", "timed_out" or "stopped".

    On time out, each question still open gets its suggested answer (or its first option),
    so an overnight run loses minutes, not the night.
    """
    events = hub.events
    texts = [hub.question(q)["text"] for q in question_ids]
    minutes = max(1, round(seconds / 60))
    notify(f"{texts[0][:120]} I'll go with my suggestion in {minutes} min unless you answer.", "Roughcut Local needs you")
    events.emit("waiting", questions=list(question_ids), until=time.time() + seconds)
    deadline = time.time() + seconds
    while time.time() < deadline:
        still = [q for q in question_ids if hub.question(q)["status"] == "open"]
        if not still:
            events.emit("waited", how="answered")
            return "answered"
        if stop():
            events.emit("waited", how="stopped")
            return "stopped"
        time.sleep(poll)
    for qid in question_ids:
        question = hub.question(qid)
        if question["status"] != "open":
            continue
        pick = question.get("suggested") or (question["options"] or ["Simplify it"])[0]
        hub.answer(qid, pick)
        events.emit("note", text=f"no answer in {minutes} min to {qid}: went with \"{pick}\"")
    hub.render()
    events.emit("waited", how="timed_out")
    return "timed_out"


def commit(folder, message):
    """Commit everything in the project, so every step can be looked at and undone."""
    git = ["git", "-C", str(folder)]
    subprocess.run(git + ["add", "-A"], capture_output=True)
    result = subprocess.run(git + ["commit", "-q", "-m", message], capture_output=True, text=True)
    if result.returncode != 0 and "user.email" in (result.stderr + result.stdout):
        identity = ["-c", "user.name=roughcut-local", "-c", "user.email=roughcut-local@localhost"]
        subprocess.run(git[:3] + identity + ["commit", "-q", "-m", message], capture_output=True)
