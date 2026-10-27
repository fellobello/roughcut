"""One round: plan, build every ready file in parallel, verify, then questions for the user.

This is the only part that writes the project's files and the hub. Agents return data
(protocol.REPLIES); the round checks it, writes it, runs the tests and decides what
happens next. A task starts as soon as a slot is free and everything it depends on is
done, so one slow file doesn't hold up the rest.

Each file gets two tasks: `<id>-tests`, written from the contract alone, then `<id>`,
which writes the file and retries against those tests. When a file still fails after
`fix_tries`, a reviewer decides whether the code, the test or the contract is wrong.
"""

from __future__ import annotations

import ast
import asyncio
import re
import subprocess

from . import agents
from .contracts import ContractError, file_id
from .hub_local import HUB_OWNER, HubError
from .llm import LLMError
from .runner import Runner
from .scheduler import Scheduler

FENCE = re.compile(r"^\s*```[a-zA-Z]*\n(.*?)\n?```\s*$", re.S)
MAX_DECISIONS = 5  # per task; small models over-report


def clean_code(text):
    match = FENCE.match(text)
    return (match.group(1) if match else text).strip() + "\n"


def defined_names(code):
    names = set()
    for node in ast.parse(code).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def check_code(code, path, exports=()):
    """Return a problem to send back to the model, or "" when the file is usable."""
    try:
        compile(code, path, "exec")
    except SyntaxError as e:
        return f"SyntaxError on line {e.lineno}: {e.msg}"
    missing = sorted({e["name"].split(".")[0] for e in exports} - defined_names(code))
    if missing:
        return f"the file doesn't define these exports from its contract: {', '.join(missing)}"
    return ""


class Round:
    def __init__(self, hub, llm, echo=None):
        self.hub = hub
        self.cfg = hub.local_config()
        self.events = hub.events
        self.events.echo = echo
        self.sched = Scheduler(llm, self.cfg, self.events)
        self.runner = Runner(hub.project, self.cfg["test_timeout"], self.cfg["sandbox"], self.events)
        self.requests = []  # (task id, contract request), applied between tasks
        self.number = None

    # The round

    async def run(self):
        hub = self.hub
        self.number = hub.config()["round"]
        self.events.round = self.number
        idea = (hub.root / "idea.md").read_text(encoding="utf-8").strip()
        self.events.emit("round_start", idea=idea[:300])
        hub.release()
        for task_id in hub.retry_failed():
            self.events.emit("note", text=f"retrying {task_id}, which failed last round")

        self.events.emit("phase", name="plan")
        complete = await self.plan(idea)
        self.commit(f"Round {self.number} plan")

        self.events.emit("phase", name="build")
        await self.build_pass()

        self.events.emit("phase", name="verify")
        passed, output = await self.runner.run()
        if not passed and self.reopen_failures(output):
            self.events.emit("phase", name="fix")
            await self.build_pass()
            passed, output = await self.runner.run()
            if not passed:
                self.reopen_failures(output)  # left for the next round

        self.events.emit("phase", name="questions")
        notes = await self.report_notes(passed, output)
        hub.render()
        hub.contracts.render()
        report = hub.report(notes)
        self.commit(f"Round {self.number} questions")
        status = hub.status()
        summary = {
            "round": self.number,
            "tasks": status["tasks"],
            "tests_passed": passed,
            "open_questions": status["open_questions"],
            "guesses_to_confirm": len(status["guesses_to_confirm"]),
            "complete": complete,
            "report": report["path"],
        }
        hub.next_round()
        self.events.emit("round_end", summary=summary)
        return summary

    # Plan

    async def plan(self, idea):
        hub = self.hub
        first = not hub.contracts.all()
        answers = hub.answered_since(self.number)
        has_todo = any(t["status"] == "todo" for t in hub.tasks())
        if not (first or answers or not has_todo):
            self.events.emit("note", text="unfinished tasks and no new answers, so the plan stays as it is")
            return None
        contracts_md = "\n\n".join(hub.contracts.describe(c) for c in hub.contracts.all())
        reply = await agents.plan(self.sched, idea, hub.memory.conventions(), contracts_md, answers, first)
        self.apply_plan(reply)
        return reply["complete"]

    def apply_plan(self, reply):
        hub, contracts = self.hub, self.hub.contracts
        hub.memory.set_conventions(reply["conventions"])
        for guess in reply["guesses"]:
            if guess["text"].strip():
                self.decide(HUB_OWNER, guess, critical=True)
        planned = []
        for file_plan in reply["files"]:
            try:
                contract, what = contracts.upsert(file_plan, self.number, f"round {self.number} plan")
            except (ContractError, ValueError) as e:
                self.events.emit("note", text=f"skipped a planned file: {e}")
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
        for contract, what in planned:
            if what == "same":
                continue
            self.events.emit("contract", path=contract["path"], version=contract["version"], reason=what)
            self.ensure_tasks(contract, what)
            if what == "changed":
                for path in contracts.dependents(contract["path"]):
                    self.redo(file_id(path), f"{contract['path']} changed to contract v{contract['version']}")
        self.write_packages()
        errors = hub.check()
        if errors:
            raise HubError("the plan's task graph is invalid: " + "; ".join(errors))

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

    def redo(self, task_id, reason):
        task = self.hub.task(task_id)
        if task["status"] in ("done", "failed", "blocked"):
            self.hub.retry(task_id, reason)
        elif task["status"] == "todo":
            self.hub.note(task_id, reason)

    def write_packages(self):
        folders = {"tests"}
        for contract in self.hub.contracts.all():
            parts = contract["path"].split("/")[:-1]
            folders.update("/".join(parts[: i + 1]) for i in range(len(parts)))
        for folder in folders:
            init = self.hub.project / folder / "__init__.py"
            if not init.exists():
                self.hub._write_text(init, "")

    # Build

    async def build_pass(self):
        limit = self.cfg["max_parallel_tasks"]
        running = {}
        while True:
            free = limit - len(running)
            if free > 0:
                for task in self.hub.start_tasks(free):
                    running[task["id"]] = asyncio.ensure_future(self.run_task(task))
            if not running:
                break
            done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
            for task_id in [k for k, fut in running.items() if fut in done]:
                running.pop(task_id).result()  # run_task handles its own errors; this surfaces bugs
            self.apply_requests()
        self.commit(f"Round {self.number} build")

    async def run_task(self, task):
        hub = self.hub
        contract = hub.contracts.get(task["module"])
        self.events.emit("task_start", task=task["id"], kind=task["kind"], path=task["module"], attempt=task["attempts"])
        then = None
        try:
            if task["kind"] == "test":
                status, summary = await self.write_tests(task, contract)
            else:
                status, summary, then = await self.build(task, contract)
        except LLMError as e:
            status, summary = "failed", f"a model call failed: {e}"
        hub.finish(task["id"], status, summary)
        if then:
            then()
        self.events.emit("task_end", task=task["id"], status=status, summary=summary)

    async def write_tests(self, task, contract):
        notes = list(task["notes"])
        brief = self.hub.contracts.brief(contract["path"])
        for _ in range(2):
            reply = await agents.write_tests(self.sched, brief, self.hub.memory.conventions(), contract["test_path"], notes)
            code = clean_code(reply["content"])
            problem = check_code(code, contract["test_path"])
            if not problem and contract["module"] not in code:
                problem = f"the tests never import {contract['module']}"
            if not problem:
                break
            notes.append(f"Your last test file was rejected: {problem}")
        else:
            return "failed", f"no usable test file: {problem}"
        self.hub._write_text(self.hub.project / contract["test_path"], code)
        self.record(task["id"], reply["decisions"])
        return "done", f"wrote {contract['test_path']}"

    async def build(self, task, contract):
        """Returns (status, summary, then): `then` runs after the task is finished, to send it back."""
        hub = self.hub
        path, target = contract["path"], hub.project / contract["path"]
        brief = hub.contracts.brief(path)
        previous = target.read_text(encoding="utf-8") if target.exists() else ""
        failure, passed, tries = "", False, 0
        for tries in range(1, self.cfg["fix_tries"] + 1):
            reply = await agents.build_file(
                self.sched, brief, path, hub.memory.conventions(), hub.memory.lessons(), task["notes"], previous, failure
            )
            code = clean_code(reply["content"])
            hub._write_text(target, code)
            for request in reply["contract_requests"]:
                self.requests.append((task["id"], request))
            failure = check_code(code, path, contract["exports"])
            if not failure:
                passed, output = await self.runner.run(contract["test_path"])
                if passed:
                    break
                failure = output
            previous = code
        self.record(task["id"], reply["decisions"])
        if passed:
            if tries > 1:
                hub.memory.add_lesson(reply["lesson"], path)
            return "done", f"{path} passes its tests" + (f" after {tries} tries" if tries > 1 else ""), None
        return await self.after_failure(task, contract, code, failure)

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
            options=["Change the contract", "Drop this file", "Simplify what it does"],
        )
        self.events.emit("question", id=question["id"], text=question["text"], blocking=True)
        return "blocked", f"needs your answer: {reason}", None

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
                options=[o for o in decision.get("options", []) if o.strip()],
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
            target = request["path"]
            names = [e for e in request["add_exports"] if e["name"].strip()]
            if not names or not contracts.exists(target):
                continue
            asker = hub.contracts.get(hub.task(task_id)["module"])
            if target == asker["path"]:
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

    def reopen_failures(self, output):
        by_test = {c["test_path"]: c for c in self.hub.contracts.all()}
        reopened = []
        for test_file in self.runner.failing_test_files(output):
            contract = by_test.get(test_file)
            if contract and self.hub.task(contract["id"])["status"] == "done":
                self.hub.reopen(contract["id"], f"the full test run fails in {test_file}")
                reopened.append(contract["id"])
        return reopened

    async def report_notes(self, passed, output):
        tasks = self.hub.tasks()
        facts = "\n".join(f"- {t['id']} ({t['kind']}): {t['status']}. {t['summary'] or ''}" for t in tasks)
        facts += f"\n\nFull test run: {'passed' if passed else 'failed'}."
        if not passed:
            facts += "\nEnd of the output:\n" + output[-800:]
        try:
            return (await agents.report(self.sched, facts))["notes"]
        except LLMError:
            done = sum(t["status"] == "done" for t in tasks)
            return f"{done} of {len(tasks)} tasks are done. The full test run {'passed' if passed else 'failed'}."

    def commit(self, message):
        git = ["git", "-C", str(self.hub.project)]
        subprocess.run(git + ["add", "-A"], capture_output=True)
        result = subprocess.run(git + ["commit", "-q", "-m", message], capture_output=True, text=True)
        if result.returncode != 0 and "user.email" in (result.stderr + result.stdout):
            identity = ["-c", "user.name=roughcut-local", "-c", "user.email=roughcut-local@localhost"]
            subprocess.run(git[:3] + identity + ["commit", "-q", "-m", message], capture_output=True)
