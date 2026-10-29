import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_HOME = tempfile.TemporaryDirectory()  # the project list and projects folder, kept out of your real ones
os.environ["ROUGHCUT_LOCAL_HOME"] = str(Path(_HOME.name) / "home")
os.environ["ROUGHCUT_PROJECTS"] = str(Path(_HOME.name) / "projects")
os.environ["ROUGHCUT_QUIET"] = "1"  # no Mac notifications from tests

from rclocal import pages, project, protocol  # noqa: E402
from rclocal.brainstorm import Brainstorm  # noqa: E402
from rclocal.contracts import ContractError, file_id  # noqa: E402
from rclocal.events import Events  # noqa: E402
from rclocal.hub_local import LocalHub  # noqa: E402
from rclocal.llm import FakeLLM, OllamaClient  # noqa: E402
from hub import HubError  # noqa: E402
from rclocal.rounds import DESIGN_TASK, Round, check_code, clean_code, run_rounds  # noqa: E402
from rclocal.runner import Runner  # noqa: E402
from rclocal.scheduler import Scheduler  # noqa: E402
from rclocal.stacks import PLACEHOLDER, STACKS, js_guard, py_guard  # noqa: E402
from rclocal.status import snapshot  # noqa: E402
from rclocal.users import TestUsers, browser_facts  # noqa: E402

PY, WEB = STACKS["python"], STACKS["web"]
HAS_NODE = bool(shutil.which("node"))


def export(name, signature, description="does it", examples=()):
    return {"name": name, "signature": signature, "description": description, "examples": list(examples)}


def plan_of(files, complete=True):
    return {
        "name": "temps",
        "summary": "temperature converter",
        "files": files,
        "conventions": ["Round to one decimal place in text."],
        "guesses": [{"text": "Which interface?", "choice": "functions only", "why": "smallest",
                     "options": [{"label": "functions only", "detail": "No command line."}, {"label": "CLI", "detail": "A command."}]}],
        "complete": complete,
    }


# Python fixtures

CONVERT = {
    "path": "app/convert.py",
    "purpose": "Convert temperatures",
    "exports": [export("to_celsius", "def to_celsius(f: float) -> float")],
    "imports": [],
}
REPORT = {
    "path": "app/report.py",
    "purpose": "Format a reading",
    "exports": [export("describe", "def describe(f: float) -> str")],
    "imports": ["app/convert.py"],
}
PY_TESTS = {
    "tests/test_app_convert.py": (
        "import unittest\nfrom app.convert import to_celsius\n\n"
        "class ConvertTest(unittest.TestCase):\n"
        "    def test_freezing(self):\n        self.assertEqual(to_celsius(32), 0)\n\n"
        "    def test_boiling(self):\n        self.assertEqual(to_celsius(212), 100)\n"
    ),
    "tests/test_app_report.py": (
        "import unittest\nfrom app.report import describe\n\n"
        "class ReportTest(unittest.TestCase):\n"
        "    def test_describe(self):\n        self.assertEqual(describe(212), '100.0 C')\n"
    ),
}
CONVERT_BAD = "def to_celsius(f: float) -> float:\n    return f - 32\n"
CONVERT_GOOD = "def to_celsius(f: float) -> float:\n    return (f - 32) * 5 / 9\n"
CONVERT_EVIL = "import subprocess\n\ndef to_celsius(f: float) -> float:\n    subprocess.run(['rm', '-rf', '/'])\n    return 0\n"
REPORT_GOOD = "from app.convert import to_celsius\n\ndef describe(f: float) -> str:\n    return f'{to_celsius(f):.1f} C'\n"

# Web fixtures

TIP = {
    "path": "app/tip.js",
    "purpose": "Work out tips",
    "exports": [export("tipFor", "function tipFor(bill, percent) -> number")],
    "imports": [],
}
WEB_TESTS = {
    "tests/app_tip.test.mjs": (
        "import { test } from 'node:test';\nimport assert from 'node:assert';\n"
        "import { tipFor } from '../app/tip.js';\n\n"
        "test('tipFor gives the percent of the bill', () => assert.equal(tipFor(100, 15), 15));\n"
    ),
}
TIP_GOOD = "export function tipFor(bill, percent) {\n  return bill * percent / 100;\n}\n"
PAGE = (
    '<!doctype html><html><head><link rel="stylesheet" href="kit.css"><link rel="stylesheet" href="styles.css">'
    '</head><body>'
    '<main class="card"><h1>Tips</h1><output id="out"></output></main>'
    '<script type="module">\nimport { tipFor } from "./app/tip.js";\n'
    'document.getElementById("out").textContent = tipFor(50, 20);\n'
    'document.body.dataset.ready = "1";\n</script></body></html>'
)
CSS = ":root { --accent: #2f6fde; }\n.card { padding: 24px; }\n"


def written(user):
    return re.search(r"Write (\S+?)( now|\n)", user).group(1) if "Write " in user else None


class Script:
    """A fake team. Builders are agents: they always choose to write, and each write
    returns the next version of their file from `builds` (the last one repeats)."""

    def __init__(self, builds, tests, plan, verdict="code", steps=None, split=None):
        self.builds = builds
        self.tests = tests
        self.plan = plan
        self.split = split or {"why": "", "pieces": []}  # no pieces: the file can't be split, so it's left for you
        self.verdict = verdict
        self.steps = steps or {}  # path -> list of actions to take before writing
        self.tries = {}
        self.taken = {}

    def _owner(self, system):
        return re.search(r"You own exactly one file, (\S+),", system).group(1)

    def __call__(self, role, model, system, user, schema):
        if role == "planner":
            return self.split if schema is protocol.SPLIT else self.plan
        if role == "tester":
            return "```\n" + self.tests[re.search(r"## Write\n(\S+)", user).group(1)] + "\n```\nDone."
        if role == "builder" and schema is protocol.STEP:
            path = self._owner(system)
            queue = self.steps.get(path, [])
            n = self.taken.get(path, 0)
            self.taken[path] = n + 1
            if n < len(queue):
                action, arg = queue[n]
                return {"action": action, "path": arg, "note": "a reason"}
            return {"action": "write", "path": path, "note": ""}
        if role == "builder":
            path = written(user)
            n = self.tries.get(path, 0)
            self.tries[path] = n + 1
            versions = self.builds[path]
            return "```\n" + versions[min(n, len(versions) - 1)] + "\n```"
        if role == "notes":
            path = re.search(r"## (app/\S+)\n", user).group(1)
            return {
                "decisions": [{"text": f"How {path} rounds", "choice": "no rounding", "why": "simple"}],
                "contract_requests": [],
                "lesson": "Convert with (f - 32) * 5 / 9." if path == "app/convert.py" else "",
            }
        if role == "designer":
            return "```html\n" + PAGE + "\n```" if "## Idea" in user else "```css\n" + CSS + "\n```"
        if role == "reviewer":
            return {"blame": self.verdict, "reason": "it subtracts but never scales",
                    "quote": "to_celsius(f: float) -> float" if self.verdict == "contract" else ""}
        if role == "reporter":
            return {"notes": "Built the converter."}
        raise AssertionError(f"unexpected role {role}")


def py_script(builds, **kw):
    return Script(builds, PY_TESTS, plan_of([CONVERT, REPORT]), **kw)


class HubCase(unittest.TestCase):
    stack = "python"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "proj"
        self.hub = LocalHub(self.project)
        self.hub.init("A temperature converter", {"stack": self.stack, "sandbox": False, "agent_steps": 3, "example_wait": 0})
        subprocess.run(["git", "-C", str(self.project), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.project), "config", "user.name", "test"], check=True)

    def tearDown(self):
        self._tmp.cleanup()

    def run_round(self, script, llm=None, echo=None):
        return asyncio.run(Round(self.hub, llm or FakeLLM(script), echo=echo).run())

    def statuses(self):
        return {t["id"]: t["status"] for t in self.hub.tasks()}


class ProtocolTest(unittest.TestCase):
    def test_every_role_replies_with_data_or_code(self):
        data_roles = {"brainstormer", "planner", "surveyor", "user", "reviewer", "reporter"}
        self.assertEqual(set(protocol.ROLES), data_roles | set(protocol.CODE_ROLES))
        kinds = {"brainstorm", "planner", "split", "survey", "step", "notes", "page_part", "user_plan", "user_verdict",
                 "checklist", "reviewer", "reporter"}
        self.assertEqual(kinds, set(protocol.REPLIES))
        self.assertTrue(all(protocol.ROLE_JOBS.values()))

    def test_questions_always_carry_options_and_a_pick(self):
        self.assertEqual(set(protocol.QUESTION["required"]), {"text", "options", "default", "why"})

    def test_steps_only_name_known_actions_and_carry_no_code(self):
        protocol.validate({"action": "read", "path": "app/a.py", "note": ""}, protocol.STEP)
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate({"action": "shell", "path": "", "note": "rm -rf /"}, protocol.STEP)
        self.assertNotIn("content", protocol.STEP["properties"])

    def test_validate_reports_the_missing_field(self):
        with self.assertRaisesRegex(protocol.ProtocolError, "lesson"):
            protocol.validate({"decisions": [], "contract_requests": []}, protocol.NOTES)

    def test_unknown_or_incomplete_events_are_refused(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.check_event({"type": "gossip"})
        with self.assertRaises(protocol.ProtocolError):
            protocol.check_event({"type": "task_end", "task": "x"})


class GuardTest(unittest.TestCase):
    def test_python_guard_blocks_processes_network_deletes_and_secrets(self):
        for code in (
            "import subprocess", "from socket import socket", "import urllib.request", "import shutil",
            "import os\nos.system('ls')", "import os\nos.remove('x')", "import os\nos.environ['TOKEN']",
            "eval('1')", "__import__('os')",
        ):
            self.assertTrue(py_guard(code), code)

    def test_python_guard_allows_ordinary_code(self):
        self.assertEqual(py_guard("import math, json, os.path\nfrom dataclasses import dataclass\nx = math.pi\n"), [])

    def test_js_guard_blocks_node_system_modules_network_and_eval(self):
        for code in (
            "import fs from 'node:fs'", "const cp = require('child_process')", "fetch('https://x.y')",
            "import x from 'lodash'", "eval('1')", "new Function('return 1')", "process.env.KEY",
            "const m = await import('./x.js')", '<script src="https://cdn.x/y.js"></script>',
        ):
            self.assertTrue(js_guard(code), code)

    def test_js_guard_allows_tests_and_relative_imports(self):
        code = "import { test } from 'node:test';\nimport assert from 'node:assert/strict';\nimport { a } from '../app/a.js';\n"
        self.assertEqual(js_guard(code), [])


class ContractsTest(HubCase):
    def test_paths_map_to_ids_and_tests(self):
        self.assertEqual(file_id("app/geo_tools.py"), "app-geo-tools-py")
        self.assertEqual(PY.test_path("app/geo/core.py"), "tests/test_app_geo_core.py")
        self.assertEqual(WEB.test_path("app/tip.js"), "tests/app_tip.test.mjs")

    def test_bad_paths_are_refused(self):
        for path in ("../x.py", "/abs.py", "app/Geo.py", "tests/test_x.py", "app/geo.js", "geo.py"):
            with self.assertRaises(ContractError, msg=path):
                self.hub.contracts.upsert({**CONVERT, "path": path}, 0, "test")

    def test_changes_bump_the_version_and_keep_history(self):
        c = self.hub.contracts
        self.assertEqual(c.upsert(CONVERT, 0, "plan")[1], "new")
        self.assertEqual(c.upsert(CONVERT, 0, "plan again")[1], "same")
        changed = {**CONVERT, "exports": CONVERT["exports"] + [export("to_f", "def to_f(c: float) -> float")]}
        contract, what = c.upsert(changed, 1, "answer")
        self.assertEqual((what, contract["version"]), ("changed", 2))
        self.assertEqual([h["reason"] for h in contract["history"]], ["plan", "answer"])

    def test_add_exports_only_adds_new_names(self):
        c = self.hub.contracts
        c.upsert(CONVERT, 0, "plan")
        _, added = c.add_exports("app/convert.py", [export("to_celsius", "x"), export("to_f", "def to_f()")], 0, "ask")
        self.assertEqual(added, ["to_f"])
        self.assertEqual(c.get("app/convert.py")["version"], 2)

    def test_dependents_are_transitive(self):
        c = self.hub.contracts
        c.upsert(CONVERT, 0, "plan")
        c.upsert(REPORT, 0, "plan")
        c.upsert({**REPORT, "path": "app/main.py", "imports": ["app/report.py"]}, 0, "plan")
        self.assertEqual(c.dependents("app/convert.py"), ["app/main.py", "app/report.py"])

    def test_brief_includes_imported_contracts_only(self):
        c = self.hub.contracts
        c.upsert(CONVERT, 0, "plan")
        c.upsert(REPORT, 0, "plan")
        self.assertIn("to_celsius", c.brief("app/report.py"))
        self.assertNotIn("describe", c.brief("app/convert.py"))

    def test_a_web_brief_says_how_the_file_itself_imports_each_one(self):
        self.assertEqual(WEB.import_from("app/bill_parts/split_bill.js", "app/bill_parts/math.js"), "./math.js")
        self.assertEqual(WEB.import_from("app/bill_parts/split_bill.js", "app/round.js"), "../round.js")
        self.assertIsNone(PY.import_from("app/report.py", "app/convert.py"))


class CodeChecksTest(unittest.TestCase):
    def test_fences_are_removed(self):
        self.assertEqual(clean_code("```python\nx = 1\n```"), "x = 1\n")
        self.assertEqual(clean_code("```python\nx = 1"), "x = 1\n")
        self.assertEqual(clean_code("x = 1\n"), "x = 1\n")

    def test_syntax_errors_missing_exports_and_guarded_code_are_caught(self):
        self.assertIn("SyntaxError", check_code(PY, "def f(:\n", "a.py")[0])
        self.assertIn("to_f", check_code(PY, "def g(): pass\n", "a.py", [export("to_f", "def to_f()")])[0])
        problem, guarded = check_code(PY, CONVERT_EVIL, "a.py")
        self.assertTrue(guarded)
        self.assertIn("subprocess", problem)
        self.assertEqual(check_code(PY, "X: int = 1\nclass Y: pass\n", "a.py", [export("X", "X"), export("Y", "Y")]), ("", False))

    def test_styles_cannot_restyle_the_kit(self):
        from rclocal.stacks import filter_styles

        css, dropped = filter_styles(
            "body { background: red }\n.card { border-radius: 0 }\n.share-list li { color: var(--accent); }\n"
            "@media (max-width: 480px) { .stat { padding: 0 } .share-list { gap: 4px } }\n@import url('x.css');\n"
        )
        self.assertEqual(dropped, ["body", ".card", ".stat"])
        self.assertIn(".share-list li", css)
        self.assertIn("@media (max-width: 480px)", css)
        self.assertNotIn("import", css)

    def test_forgotten_test_imports_are_added(self):
        fixed = WEB.repair_test("import { a } from '../app/a.js';\ntest('a', () => assert.ok(a()));\n")
        self.assertEqual(WEB.test_problem(fixed), "")
        self.assertTrue(fixed.startswith("import { test } from 'node:test';\nimport assert from 'node:assert/strict';"))
        self.assertTrue(PY.repair_test("class T(unittest.TestCase): pass\n").startswith("import unittest\n"))
        whole = WEB.test_header + "\ntest('a', () => assert.ok(1));\n"
        self.assertEqual(WEB.repair_test(whole), whole)

    @unittest.skipUnless(HAS_NODE, "needs node")
    def test_js_exports_and_syntax(self):
        self.assertEqual(WEB.defined_names("export function a() {}\nexport const B = 1;\nexport { c as d };"), {"a", "B", "d"})
        self.assertIn("SyntaxError", WEB.syntax_error("export function (", "app/x.js"))
        self.assertEqual(WEB.syntax_error(TIP_GOOD, "app/tip.js"), "")


class RoundTest(HubCase):
    def test_a_round_builds_every_file_and_passes(self):
        summary = self.run_round(py_script({"app/convert.py": [CONVERT_GOOD], "app/report.py": [REPORT_GOOD]}))
        self.assertTrue(summary["tests_passed"])
        self.assertEqual(set(self.statuses().values()), {"done"})
        self.assertEqual((self.project / "app/convert.py").read_text(), CONVERT_GOOD)
        self.assertEqual(summary["guesses_to_confirm"], 1)
        self.assertEqual(self.hub.config()["round"], 1)
        for name in ("QUESTIONS.md", "CONTRACTS.md", ".hub/rounds/0.md"):
            self.assertTrue((self.project / name).exists(), name)
        self.assertIn("Round to one decimal", self.hub.memory.conventions())
        log = subprocess.run(["git", "-C", str(self.project), "log", "--format=%s"], capture_output=True, text=True)
        self.assertIn("Round 0 questions", log.stdout)

    def test_tests_come_before_the_file_they_check(self):
        events = []
        self.run_round(py_script({"app/convert.py": [CONVERT_GOOD], "app/report.py": [REPORT_GOOD]}), echo=events.append)
        order = [e["task"] for e in events if e["type"] == "task_end"]
        self.assertLess(order.index("app-convert-py-tests"), order.index("app-convert-py"))
        self.assertLess(order.index("app-convert-py"), order.index("app-report-py"))

    def test_the_agent_fixes_its_file_and_leaves_a_lesson(self):
        script = py_script({"app/convert.py": [CONVERT_BAD, CONVERT_GOOD], "app/report.py": [REPORT_GOOD]})
        self.run_round(script)
        self.assertEqual(script.tries["app/convert.py"], 2)
        self.assertEqual(self.hub.task("app-convert-py")["summary"], "app/convert.py passes its tests after 2 writes")
        self.assertIn("(f - 32) * 5 / 9", self.hub.memory.lessons())

    def test_guarded_code_is_never_written_or_run(self):
        events = []
        script = py_script({"app/convert.py": [CONVERT_EVIL, CONVERT_GOOD], "app/report.py": [REPORT_GOOD]})
        self.run_round(script, echo=events.append)
        blocked = [e for e in events if e["type"] == "blocked_code"]
        self.assertEqual([e["path"] for e in blocked], ["app/convert.py"])
        self.assertNotIn("subprocess", (self.project / "app/convert.py").read_text())
        self.assertEqual(self.hub.task("app-convert-py")["status"], "done")

    def test_the_agent_can_only_read_what_its_file_needs(self):
        steps = {"app/report.py": [("read", "app/convert.py"), ("read", ".hub/local.json"), ("read", "../../.ssh/id_rsa")]}
        script = py_script({"app/convert.py": [CONVERT_GOOD], "app/report.py": [REPORT_GOOD]}, steps=steps)
        llm = FakeLLM(script)
        self.hub._write(self.hub.root / "local.json", {**self.hub.local_config(), "agent_steps": 5})
        self.run_round(script, llm=llm)
        seen = [m["content"] for c in llm.calls if "You own exactly one file, app/report.py" in c["system"]
                for m in c["messages"] if m["role"] == "user"]
        self.assertTrue(any(s.startswith("app/convert.py:") for s in seen))
        self.assertTrue(any("You may not read .hub/local.json" in s for s in seen))
        self.assertTrue(any("You may not read ../../.ssh/id_rsa" in s for s in seen))
        self.assertEqual(self.hub.task("app-report-py")["status"], "done")

    def test_a_stuck_file_is_left_for_you_and_the_run_goes_on(self):
        from rclocal.rounds import HANDOVER, I_GOT_IT

        summary = self.run_round(py_script({"app/convert.py": [CONVERT_BAD], "app/report.py": [REPORT_GOOD]}))
        task = self.hub.task("app-convert-py")
        self.assertEqual((task["status"], task["attempts"]), ("failed", 3))
        self.assertTrue(task["summary"].startswith("left for you"))
        self.assertEqual(summary["open_questions"], [])  # nothing waits on you
        left = (self.project / "app/convert.py").read_text()
        self.assertIn(HANDOVER, left)
        self.assertIn("def to_celsius(f: float) -> float", left)  # the contract, in the note
        self.assertIn("python3 -m unittest tests.test_app_convert", left)
        self.assertIn("return f - 32", left)  # and its best attempt, kept
        guess = next(q for q in self.hub.questions() if q["task"] == "app-convert-py")
        self.assertEqual((guess["status"], guess["choice"]), ("decided", I_GOT_IT))
        self.assertEqual(self.statuses()["app-report-py"], "todo")  # only what needs it waits

    def test_simplify_sends_a_stuck_file_back_to_the_planner_with_new_tests(self):
        from rclocal.hub_local import HUB_OWNER
        from rclocal.rounds import SIMPLIFY

        self.run_round(py_script({"app/convert.py": [CONVERT_BAD], "app/report.py": [REPORT_GOOD]}))
        stuck = next(q for q in self.hub.questions() if q["task"] == "app-convert-py")
        self.hub.answer(stuck["id"], SIMPLIFY)
        asyncio.run(Round(self.hub, FakeLLM(lambda *a: {})).revisit_stuck())
        self.assertEqual(self.hub.task("app-convert-py")["status"], "todo")
        self.assertEqual(self.hub.task("app-convert-py-tests")["status"], "todo")  # its tests are written again
        to_plan = [q for q in self.hub.answered_since(self.hub.config()["round"]) if q["task"] == HUB_OWNER]
        self.assertTrue(any("Simplify it" in q["answer"] and "app/convert.py" in q["text"] for q in to_plan))

    def test_an_answer_given_during_a_round_reaches_the_next_plan(self):
        from rclocal.hub_local import HUB_OWNER

        q = self.hub.ask(HUB_OWNER, "Round to whole dollars?", critical=True, choice="yes")
        self.hub.answer(q["id"], "yes")
        self.hub.next_round()
        self.hub.next_round()  # two rounds go by before a plan runs
        self.assertIn(q["id"], [a["id"] for a in self.hub.answered_since(self.hub.config()["round"])])
        self.hub.mark_planned([q["id"]])
        self.assertNotIn(q["id"], [a["id"] for a in self.hub.answered_since(self.hub.config()["round"])])
        self.hub.answer(q["id"], "no")  # a changed answer is planned with again
        self.assertIn(q["id"], [a["id"] for a in self.hub.answered_since(self.hub.config()["round"])])

    def test_i_got_it_keeps_builders_off_a_file_that_went_back_to_todo(self):
        from rclocal.rounds import I_GOT_IT

        self.run_round(py_script({"app/convert.py": [CONVERT_BAD], "app/report.py": [REPORT_GOOD]}))
        stuck = next(q for q in self.hub.questions() if q["task"] == "app-convert-py")
        self.hub.restart("app-convert-py", "something else sent it back")
        self.hub.answer(stuck["id"], I_GOT_IT)
        asyncio.run(Round(self.hub, FakeLLM(lambda *a: {})).revisit_stuck())
        task = self.hub.task("app-convert-py")
        self.assertEqual((task["status"], task["attempts"]), ("failed", self.hub.config()["max_attempts"]))

    def test_stop_cuts_a_round_off_and_puts_its_files_back(self):
        script = py_script({"app/convert.py": [CONVERT_GOOD], "app/report.py": [REPORT_GOOD]})
        stop = []

        def respond(role, *rest):
            if role == "builder":
                stop.append(True)  # you press Stop while the first builder is writing
            return script(role, *rest)

        summaries, why = run_rounds(self.hub, FakeLLM(respond), 3, cancel=lambda: bool(stop))
        self.assertEqual((summaries, why), ([], "you stopped it partway through a round"))
        self.assertNotIn("in_progress", self.statuses().values())
        self.assertEqual(self.hub.config()["round"], 0)  # the round runs again next time

    def test_a_run_ends_when_all_that_is_left_waits_on_a_stuck_file(self):
        script = py_script({"app/convert.py": [CONVERT_BAD], "app/report.py": [REPORT_GOOD]})
        summaries, why = run_rounds(self.hub, FakeLLM(script), 4)
        self.assertEqual(len(summaries), 1)
        self.assertIn("waits on app/convert.py", why)

    def test_a_file_you_finish_is_picked_up(self):
        script = py_script({"app/convert.py": [CONVERT_BAD], "app/report.py": [REPORT_GOOD]})
        self.run_round(script)
        (self.project / "app/convert.py").write_text(CONVERT_GOOD)  # you finish it in your editor
        script.plan = {**script.plan, "files": [], "guesses": []}
        summary = self.run_round(script)
        self.assertIn("finished by you", self.hub.task("app-convert-py")["summary"])
        self.assertTrue(summary["tests_passed"])
        self.assertEqual(set(self.statuses().values()), {"done"})

    def test_it_works_counts_a_stuck_file_as_done(self):
        script = py_script({"app/convert.py": [CONVERT_BAD], "app/report.py": [REPORT_GOOD]})
        self.run_round(script)
        guess = next(q for q in self.hub.questions() if q["task"] == "app-convert-py")
        self.assertIn("It works", guess["options"])
        self.hub.answer(guess["id"], "It works")
        script.plan = {**script.plan, "files": [], "guesses": []}
        self.run_round(script)
        self.assertEqual(self.statuses()["app-convert-py"], "done")
        self.assertTrue(self.hub.task("app-convert-py")["accepted"])
        self.assertNotEqual(self.statuses()["app-report-py"], "todo")  # what needs it gets built
        self.assertIn("you said work as they are", " ".join(e.get("text", "") for e in self.hub.events.read()))

    def test_a_done_file_keeps_its_contract_unless_you_said_something_about_it(self):
        from rclocal.hub_local import HUB_OWNER

        script = py_script({"app/convert.py": [CONVERT_GOOD], "app/report.py": [REPORT_GOOD]})
        self.run_round(script)
        version = self.hub.contracts.get("app/convert.py")["version"]
        script.plan = {**script.plan, "files": [{**CONVERT, "purpose": "Convert temperatures, put another way"}, REPORT],
                       "guesses": []}
        self.run_round(script)  # the planner only reworded it
        self.assertEqual(self.hub.contracts.get("app/convert.py")["version"], version)
        q = self.hub.ask(HUB_OWNER, "Anything about to_celsius?", critical=True, choice="no")
        self.hub.answer(q["id"], "to_celsius should explain itself better")
        self.run_round(script)
        self.assertEqual(self.hub.contracts.get("app/convert.py")["version"], version + 1)
        self.assertIn("already passes its tests", self.hub.task("app-convert-py")["summary"])  # not rebuilt

    def test_try_again_gives_the_builder_fresh_tries(self):
        script = py_script({"app/convert.py": [CONVERT_BAD], "app/report.py": [REPORT_GOOD]})
        self.run_round(script)
        guess = next(q for q in self.hub.questions() if q["task"] == "app-convert-py")
        self.hub.answer(guess["id"], "Try again")
        script.builds["app/convert.py"] = [CONVERT_GOOD]
        script.plan = {**script.plan, "files": [], "guesses": []}
        summary = self.run_round(script)
        self.assertTrue(summary["tests_passed"])
        self.assertNotIn("LEFT FOR YOU", (self.project / "app/convert.py").read_text())
        self.assertTrue(self.hub.question(guess["id"])["acted"])

    def test_a_file_that_keeps_failing_is_split_into_smaller_ones(self):
        from rclocal.rounds import failure_signature

        halves = {"why": "two jobs at once", "pieces": [
            {"path": "app/convert_parts/offset.py", "purpose": "Remove the offset", "imports": [],
             "exports": [export("minus_32", "def minus_32(f: float) -> float", examples=[{"call": "minus_32(212)", "returns": "180"}])]},
            {"path": "convert_parts/scale.py", "purpose": "Scale to Celsius", "imports": ["app/convert_parts/offset.py"],
             "exports": [export("scale", "def scale(x: float) -> float", examples=[{"call": "scale(180)", "returns": "100.0"}])]},
        ]}
        # two full attempts of bad writes (3 steps each in these tests), then good once it's built from pieces
        builds = {"app/convert.py": [CONVERT_BAD] * 6 + [CONVERT_GOOD], "app/report.py": [REPORT_GOOD],
                  "app/convert_parts/offset.py": ["def minus_32(f: float) -> float:\n    return f - 32\n"],
                  "app/convert_parts/scale.py": ["def scale(x: float) -> float:\n    return x * 5 / 9\n"]}
        tests = {**PY_TESTS,
                 "tests/test_app_convert_parts_offset.py": "import unittest\nfrom app.convert_parts.offset import minus_32\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(minus_32(32), 0)\n",
                 "tests/test_app_convert_parts_scale.py": "import unittest\nfrom app.convert_parts.scale import scale\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(scale(0), 0)\n"}
        events = []
        summary = self.run_round(Script(builds, tests, plan_of([CONVERT, REPORT]), split=halves), echo=events.append)
        split = [e for e in events if e["type"] == "split"]
        self.assertEqual(split[0]["pieces"], ["app/convert_parts/offset.py", "app/convert_parts/scale.py"])
        contract = self.hub.contracts.get("app/convert.py")
        self.assertEqual(contract["split_into"], ["app/convert_parts/offset.py", "app/convert_parts/scale.py"])
        self.assertEqual(self.hub.contracts.get("app/convert_parts/scale.py")["split_from"], "app/convert.py")
        self.assertEqual(self.hub.contracts.get("app/convert_parts/scale.py")["imports"], ["app/convert_parts/offset.py"])
        self.assertTrue(summary["tests_passed"])  # the pieces were built, then the file again, from them
        self.assertEqual(set(self.statuses().values()), {"done"})
        self.assertEqual(failure_signature("✖ to_celsius rounds (1.2ms)\nnoise\n✖ freezing"), "freezing; to_celsius rounds")

    def test_your_answers_reach_every_role_as_rules(self):
        q = self.hub.ask("hub", "Tip rounding?", critical=True, choice="Exact")
        self.hub.answer(q["id"], "Round to the nearest dollar")
        change = self.hub.ask("hub", "What should change next?", critical=True, choice="(in your words)")
        self.hub.answer(change["id"], "add a dark mode")
        llm = FakeLLM(py_script({"app/convert.py": [CONVERT_GOOD], "app/report.py": [REPORT_GOOD]}))
        self.run_round(None, llm=llm)
        for role in ("Planner:", "Tester:", "Builder:"):
            prompts = [c["user"] if role != "Builder:" else c["messages"][1]["content"] for c in llm.calls
                       if c["system"].startswith(role)]
            self.assertTrue(prompts, role)
            self.assertIn("Tip rounding? -> Round to the nearest dollar", prompts[0], role)
            self.assertNotIn("add a dark mode", prompts[0].split("New answers")[0], role)

    def test_a_contract_that_cannot_work_goes_back_to_the_planner(self):
        from rclocal.rounds import REPLAN

        self.run_round(py_script({"app/convert.py": [CONVERT_BAD], "app/report.py": [REPORT_GOOD]}, verdict="contract"))
        replan = [q for q in self.hub.questions() if q["answer"] == REPLAN]
        self.assertEqual(len(replan), 1)
        self.assertIn("app/convert.py's contract can't work", replan[0]["text"])
        self.assertEqual(self.hub.task("app-convert-py")["status"], "failed")
        rnd = Round(self.hub, FakeLLM(None))
        rnd.number = 1
        rnd.redo("app-convert-py", "contract v2")  # a new contract: fresh tries
        self.assertEqual((self.hub.task("app-convert-py")["status"], self.hub.task("app-convert-py")["attempts"]), ("todo", 0))
        self.assertNotIn("contract can't work", rnd.rules())

    def test_hand_over_notes_do_not_pile_up(self):
        from rclocal.rounds import strip_handover

        for stack, code in ((PY, CONVERT_GOOD), (WEB, TIP_GOOD)):
            note = stack.comment(["ROUGHCUT: LEFT FOR YOU", "a */ b", "", "END OF ROUGHCUT NOTE"])
            self.assertEqual(strip_handover(note + "\n" + strip_handover(note + code)), code)
        self.assertNotIn("*/ b", WEB.comment(["ROUGHCUT: LEFT FOR YOU", "a */ b", "END OF ROUGHCUT NOTE"]))

    def test_blaming_the_test_rewrites_it(self):
        self.run_round(py_script({"app/convert.py": [CONVERT_BAD], "app/report.py": [REPORT_GOOD]}, verdict="test"))
        self.assertGreaterEqual(self.hub.task("app-convert-py-tests")["attempts"], 2)
        self.assertTrue(any("tests redone" in n for n in self.hub.task("app-convert-py")["notes"]))

    def test_python_examples_become_a_test_case(self):
        convert = {**CONVERT, "exports": [export("to_celsius", "def to_celsius(f: float) -> float",
                                                 examples=[{"call": "to_celsius(212)", "returns": "100.0"}])]}
        builds = {"app/convert.py": [CONVERT_GOOD], "app/report.py": [REPORT_GOOD]}
        summary = self.run_round(Script(builds, PY_TESTS, plan_of([convert, REPORT])))
        self.assertTrue(summary["tests_passed"])
        tests = (self.project / "tests/test_app_convert.py").read_text()
        self.assertIn("self.assertEqual(_contract.to_celsius(212), 100.0)", tests)

    def test_planned_paths_are_tidied_to_fit_the_layout(self):
        from rclocal.rounds import tidy_path

        self.assertEqual(tidy_path("app/calculatePace.js", WEB), "app/calculate_pace.js")
        self.assertEqual(tidy_path("src/unit-converter", WEB), "app/unit_converter.js")
        self.assertEqual(tidy_path("Report.py", PY), "app/report.py")
        self.assertIn("..", tidy_path("../x.py", PY))  # still refused by the contract check
        camel = {**CONVERT, "path": "app/Convert.py"}
        report = {**REPORT, "path": "report.py", "imports": ["app/Convert.py"]}
        builds = {"app/convert.py": [CONVERT_GOOD], "app/report.py": [REPORT_GOOD]}
        summary = self.run_round(Script(builds, PY_TESTS, plan_of([camel, report])))
        self.assertTrue(summary["tests_passed"])
        self.assertEqual(self.hub.contracts.get("app/report.py")["imports"], ["app/convert.py"])

    def test_files_that_import_each_other_are_untangled(self):
        circle = {**CONVERT, "imports": ["app/report.py"]}
        summary = self.run_round(Script({"app/convert.py": [CONVERT_GOOD], "app/report.py": [REPORT_GOOD]},
                                        PY_TESTS, plan_of([circle, REPORT])))
        self.assertTrue(summary["tests_passed"])
        self.assertEqual(self.hub.contracts.get("app/convert.py")["imports"], [])
        self.assertEqual(self.hub.contracts.get("app/report.py")["imports"], ["app/convert.py"])

    def test_contract_requests_extend_the_other_file(self):
        script = py_script({"app/convert.py": [CONVERT_GOOD], "app/report.py": [REPORT_GOOD]})
        rnd = Round(self.hub, FakeLLM(script))
        rnd.number = 0
        rnd.apply_plan(script.plan)
        self.hub.start_tasks()
        rnd.requests.append(("app-report-py", {
            "path": "app/convert.py",
            "add_exports": [export("to_fahrenheit", "def to_fahrenheit(c: float) -> float")],
            "why": "to show both units",
        }))
        rnd.apply_requests()
        contract = self.hub.contracts.get("app/convert.py")
        self.assertIn("to_fahrenheit", [e["name"] for e in contract["exports"]])
        self.assertEqual(contract["version"], 2)


@unittest.skipUnless(HAS_NODE, "needs node")
class WebRoundTest(HubCase):
    stack = "web"

    def test_ui_files_are_left_to_the_page(self):
        rnd = Round(self.hub, FakeLLM(None))
        rnd.number = 0
        ui = {"path": "app/ui.js", "purpose": "Draw the board", "exports": [export("draw", "function draw(board)")],
              "imports": ["app/tip.js"]}
        skipped = rnd.apply_plan(plan_of([TIP, ui]))
        self.assertEqual([c["path"] for c in self.hub.contracts.all()], ["app/tip.js"])
        self.assertIn("page's own script", skipped[0])

    def test_a_web_round_makes_the_page_first_and_checks_it_once_the_logic_is_built(self):
        checked = []

        async def fake_check(screenshot=None):
            checked.append(screenshot)
            return True, ""

        script = Script({"app/tip.js": [TIP_GOOD]}, WEB_TESTS, plan_of([TIP]))
        rnd = Round(self.hub, FakeLLM(script))
        rnd.runner.check_page = fake_check
        summary = asyncio.run(rnd.run())
        self.assertTrue(summary["tests_passed"])
        starts = [e["task"] for e in self.hub.events.read() if e["type"] == "task_start"]
        self.assertEqual(starts[0], DESIGN_TASK)
        self.assertEqual(self.statuses()[DESIGN_TASK], "done")
        self.assertFalse(self.hub.task(DESIGN_TASK).get("soft"))  # checked in full by the end
        self.assertEqual((self.project / "index.html").read_text(), PAGE + "\n")
        self.assertTrue((self.project / "package.json").exists())
        self.assertIn("--accent", (self.project / "kit.css").read_text())
        self.assertIn(len(checked), (1, 2))  # once, or again after tip.js if the page was done first
        self.assertTrue(rnd.page_checked)

    def test_a_page_on_placeholders_only_has_to_load(self):
        script = Script({"app/tip.js": [TIP_GOOD]}, WEB_TESTS, plan_of([TIP]))
        rnd = Round(self.hub, FakeLLM(script))
        rnd.number = 0
        rnd.apply_plan(script.plan)
        rnd.write_placeholders()
        stand_in = (self.project / "app/tip.js").read_text()
        self.assertIn(PLACEHOLDER, stand_in)
        self.assertEqual(WEB.syntax_error(stand_in, "app/tip.js"), "")
        self.assertTrue(rnd.is_placeholder("app/tip.js"))

        async def fake_check(screenshot=None):  # it loads, but the numbers can't be right yet
            rnd.runner.last_check = {"part": "script", "probe": {"ready": True}, "inventory": None, "hidden": []}
            return False, "after filling in bill, nothing on the page changed"

        rnd.runner.check_page = fake_check
        self.hub.start_tasks(first=(DESIGN_TASK,))
        status, _ = asyncio.run(rnd.settle_page(PAGE, CSS, [], 0))
        self.assertEqual(status, "done")
        self.assertTrue(self.hub.task(DESIGN_TASK)["soft"])
        self.assertFalse(rnd.page_checked)  # so the test users wait too

    def tip_round(self, examples):
        tip = {**TIP, "exports": [export("tipFor", "function tipFor(bill, percent) -> number", examples=examples)]}
        script = Script({"app/tip.js": [TIP_GOOD]}, WEB_TESTS, plan_of([tip]))
        rnd = Round(self.hub, FakeLLM(script))

        async def fake_check(screenshot=None):
            return True, ""

        rnd.runner.check_page = fake_check
        return asyncio.run(rnd.run())

    def test_contract_examples_become_tests(self):
        summary = self.tip_round([{"call": "tipFor(100, 15)", "returns": "15"}, {"call": "tipFor(0, 20)", "returns": "0"}])
        self.assertTrue(summary["tests_passed"])
        tests = (self.project / "tests/app_tip.test.mjs").read_text()
        self.assertIn("assert.deepStrictEqual(__contract.tipFor(100, 15), 15);", tests)
        self.assertIn("plus 2 contract examples", self.hub.task("app-tip-js-tests")["summary"])
        self.assertIn("e.g. tipFor(100, 15) returns 15", self.hub.contracts.brief("app/tip.js"))
        self.assertIn("`tipFor(100, 15)` returns `15`", (self.project / "CONTRACTS.md").read_text())

    def test_a_wrong_example_holds_the_code_to_it(self):
        summary = self.tip_round([{"call": "tipFor(100, 15)", "returns": "16"}])
        self.assertFalse(summary["tests_passed"])  # the examples are the shared truth, even when wrong

    def test_you_check_new_examples_and_your_correction_wins(self):
        summary = self.tip_round([{"call": "tipFor(100, 15)", "returns": "16"}])
        self.assertFalse(summary["tests_passed"])
        q = next(q for q in self.hub.questions() if q.get("example"))
        self.assertEqual((q["text"], q["choice"], q["status"]), ("Is this right? tipFor(100, 15) returns 16", "Right", "decided"))
        self.hub.answer(q["id"], "15")  # what it should return
        summary = self.tip_round([{"call": "tipFor(100, 15)", "returns": "16"}])  # the planner repeats its mistake
        examples = self.hub.contracts.get("app/tip.js")["exports"][0]["examples"]
        self.assertEqual(examples, [{"call": "tipFor(100, 15)", "returns": "15"}])
        self.assertTrue(summary["tests_passed"])
        self.assertEqual(sum(1 for q in self.hub.questions() if q.get("example")), 1)  # not asked again

    def test_an_example_you_call_wrong_is_dropped(self):
        self.tip_round([{"call": "tipFor(100, 15)", "returns": "16"}, {"call": "tipFor(0, 20)", "returns": "0"}])
        wrong = next(q for q in self.hub.questions() if q.get("example") and "16" in q["text"])
        self.hub.answer(wrong["id"], "Wrong")
        summary = self.tip_round([{"call": "tipFor(100, 15)", "returns": "16"}, {"call": "tipFor(0, 20)", "returns": "0"}])
        self.assertEqual(self.hub.contracts.get("app/tip.js")["exports"][0]["examples"],
                         [{"call": "tipFor(0, 20)", "returns": "0"}])
        self.assertTrue(summary["tests_passed"])

    def test_examples_that_are_not_code_are_left_out(self):
        summary = self.tip_round([{"call": "tipFor(100, 15", "returns": "15"}, {"call": "new Tip()", "returns": "{}"}])
        self.assertTrue(summary["tests_passed"])
        self.assertNotIn("__contract", (self.project / "tests/app_tip.test.mjs").read_text())

    def test_bad_examples_are_left_out_one_by_one(self):
        summary = self.tip_round([{"call": "tipFor(100, 15)", "returns": "15"},
                                  {"call": "tipFor(50, 10)", "returns": "path: app/tip.js, line: 2"}])
        self.assertTrue(summary["tests_passed"])
        tests = (self.project / "tests/app_tip.test.mjs").read_text()
        self.assertIn("__contract.tipFor(100, 15), 15", tests)
        self.assertNotIn("path: app", tests)

    def test_a_plan_of_junk_examples_is_asked_for_again(self):
        junk = {**TIP, "exports": [export("tipFor", "function tipFor(bill, percent) -> number",
                                          examples=[{"call": "tipFor(100, 15)", "returns": "path: app/tip.js, line: 1"}])]}
        good = {**TIP, "exports": [export("tipFor", "function tipFor(bill, percent) -> number",
                                          examples=[{"call": "tipFor(100, 15)", "returns": "15"}])]}
        script = Script({"app/tip.js": [TIP_GOOD]}, WEB_TESTS, plan_of([junk]))
        plans = iter([plan_of([junk]), plan_of([good])])

        def respond(role, model, system, user, schema):
            return next(plans) if role == "planner" else script(role, model, system, user, schema)

        rnd = Round(self.hub, FakeLLM(respond))
        rnd.number = 0
        asyncio.run(rnd.plan("tips"))
        examples = self.hub.contracts.get("app/tip.js")["exports"][0]["examples"]
        self.assertEqual(examples, [{"call": "tipFor(100, 15)", "returns": "15"}])

    def test_the_page_may_only_import_real_exports(self):
        rnd = Round(self.hub, FakeLLM(None))
        rnd.number = 0
        rnd.apply_plan(plan_of([TIP]))
        self.assertEqual(rnd.page_problem(PAGE), "")
        self.assertIn("doesn't export tipTotal", rnd.page_problem(PAGE.replace("{ tipFor }", "{ tipFor, tipTotal }")))
        self.assertIn("doesn't exist", rnd.page_problem(PAGE.replace("./app/tip.js", "./app/split.js")))
        self.assertIn("dataset.ready", rnd.page_problem(PAGE.replace('document.body.dataset.ready = "1";', "")))
        self.assertIn("kit.css", rnd.page_problem(PAGE.replace('<link rel="stylesheet" href="kit.css">', "")))
        self.assertIn("safety guard", rnd.page_problem(PAGE.replace("<main", '<script src="https://cdn.x/y.js"></script><main')))


FIXED_SCRIPT = (
    "import { tipFor } from './app/tip.js';\n"
    "document.getElementById('out').textContent = tipFor(10, 10);\n"
    'document.body.dataset.ready = "1";'
)
NEW_MAIN = '<main class="app"><h1>Bigger tips</h1><output id="out" class="big"></output></main>'


def designer(part="markup"):
    """A fake designer: drafts PAGE, and for a fix rewrites only the part it is asked for."""
    calls = []

    def respond(role, model, system, user, schema):
        assert role == "designer", role
        if schema is protocol.PAGE_PART:
            calls.append("pick")
            return {"part": part, "why": "only the layout changes"}
        if "by rewriting only" in system:
            fixing = "script" if "<script type=\"module\"> (without" in system else "markup" if "<main> element" in system else "styles"
            calls.append(f"fix-{fixing}")
            return {"script": "```js\n" + FIXED_SCRIPT + "\n```", "markup": "```html\n" + NEW_MAIN + "\n```"}.get(
                fixing, "```css\n.big { font-size: 40px; }\n```")
        if "Write index.html" in system:
            calls.append("page")
            return "```html\n" + PAGE + "\n```"
        calls.append("styles")
        return "```css\n" + CSS + "\n```"

    return respond, calls


class PagesTest(unittest.TestCase):
    def test_parts_come_out_and_go_back_in(self):
        self.assertIn("tipFor(50, 20)", pages.get("script", PAGE))
        self.assertTrue(pages.get("markup", PAGE).startswith("<main"))
        swapped = pages.put("script", PAGE, FIXED_SCRIPT)
        self.assertIn("tipFor(10, 10)", swapped)
        self.assertEqual(pages.get("markup", swapped), pages.get("markup", PAGE))
        marked = pages.put("markup", PAGE, "Here it is:\n" + NEW_MAIN)
        self.assertEqual(pages.get("markup", marked), NEW_MAIN)
        self.assertEqual(pages.get("script", marked), pages.get("script", PAGE))
        self.assertIsNone(pages.put("markup", PAGE, "<div>no main</div>"))

    def test_hidden_results_and_example_numbers_are_repaired_without_a_model(self):
        html = ('<main><div class="stats" hidden><div class="stat"><div class="stat-value">$36.00</div></div></div>'
                '<p class="note" hidden>tip</p></main>')
        fixed, what = pages.repair(html, {"hidden": ["div.stats"], "probe": {"prefilled": ["$36.00"]}})
        self.assertNotIn('class="stats" hidden', fixed)
        self.assertIn('<p class="note" hidden>', fixed)  # not a result: left alone
        self.assertIn('<div class="stat-value">—</div>', fixed)
        self.assertIn("hidden", what)
        self.assertEqual(pages.repair(PAGE, {}), (PAGE, ""))


@unittest.skipUnless(HAS_NODE, "needs node")
class PageFixTest(HubCase):
    stack = "web"

    def design_round(self, respond, checks):
        """A round with tip.js built and the page task started; check_page answers from `checks`."""
        rnd = Round(self.hub, FakeLLM(respond))
        rnd.number = 0
        rnd.apply_plan(plan_of([TIP]))
        (self.project / "app").mkdir(exist_ok=True)
        (self.project / "app/tip.js").write_text(TIP_GOOD)
        for task in self.hub.tasks():
            if task["id"] != DESIGN_TASK:
                self.hub.mark_done(task["id"], "given")

        async def fake_check(screenshot=None):
            ok, part = checks.pop(0)
            rnd.runner.last_check = {"part": part, "probe": {}, "inventory": None, "hidden": []}
            return ok, "" if ok else "the page threw: x is not defined"

        rnd.runner.check_page = fake_check
        return rnd, self.hub.start_tasks()[0]

    def test_a_failed_check_rewrites_only_the_flagged_part(self):
        respond, calls = designer()
        rnd, task = self.design_round(respond, [(False, "script"), (True, None)])
        status, summary = asyncio.run(rnd.design(task, "tips"))
        self.assertEqual(status, "done", summary)
        self.assertEqual(calls, ["page", "styles", "fix-script"])  # no second whole page
        html = (self.project / "index.html").read_text()
        self.assertIn("tipFor(10, 10)", html)
        self.assertEqual(pages.get("markup", html), pages.get("markup", PAGE))
        self.assertIn("after 1 fix", summary)

    def test_a_change_to_a_working_page_keeps_the_rest(self):
        (self.project / "index.html").write_text(PAGE)
        (self.project / "styles.css").write_text(CSS)
        respond, calls = designer(part="markup")
        rnd, task = self.design_round(respond, [(True, None)])
        self.hub.note(task["id"], "retry: make the result bigger")
        task = self.hub.task(task["id"])
        status, _ = asyncio.run(rnd.design(task, "tips"))
        self.assertEqual(status, "done")
        self.assertEqual(calls, ["pick", "fix-markup"])
        html = (self.project / "index.html").read_text()
        self.assertEqual(pages.get("markup", html), NEW_MAIN)
        self.assertEqual(pages.get("script", html), pages.get("script", PAGE))
        self.assertEqual((self.project / "styles.css").read_text(), CSS)

    def test_a_change_that_never_works_puts_the_old_page_back(self):
        (self.project / "index.html").write_text(PAGE)
        (self.project / "styles.css").write_text(CSS)
        respond, _ = designer(part="markup")
        rnd, task = self.design_round(respond, [(False, "markup")] * 4)
        status, _ = asyncio.run(rnd.design(task, "tips"))
        self.assertEqual(status, "failed")
        self.assertEqual((self.project / "index.html").read_text(), PAGE)


@unittest.skipUnless(HAS_NODE, "needs node")
class TestUsersTest(HubCase):
    stack = "web"
    INVENTORY = {"controls": [{"id": "bill", "kind": "number", "label": "Bill", "value": ""}], "results": ["Each —"]}

    def respond(self, role, model, system, user, schema):
        if schema is protocol.USER_PLAN:
            return {"goal": "split a bill", "steps": [{"do": "fill", "target": "bill", "value": "50", "expect": "25"}]}
        if "Checker:" in system:
            return {"checks": [{"item": "Split by percentage", "seen_as": "control", "words": ["percent"]},
                               {"item": "Rounds to whole dollars", "seen_as": "not_on_page", "words": []}]}
        return {"works": False, "wish": "a share button",
                "problems": [{"what": "each share is the whole bill", "severity": "high", "where": "logic"}]}

    def users(self, plan=True):
        rnd = Round(self.hub, FakeLLM(self.respond))
        rnd.number = 0
        if plan:
            rnd.apply_plan(plan_of([TIP]))
            for task in self.hub.tasks():
                self.hub.mark_done(task["id"], "given")

        async def fake_try(steps, viewport=(1280, 900), screenshot=None):
            entry = {**steps[0], "result": "done", "shows": ["Each $NaN"], "saw_expected": False}
            return {"ready": True, "errors": [], "log": [entry], "controls": []}

        rnd.runner.try_as_user = fake_try
        return TestUsers(rnd)

    def test_the_page_check_sends_back_a_page_without_a_must_have(self):
        project.save_brief(self.hub, {**BRAINSTORM_REPLY["brief"], "look": "light and calm",
                                      "must_have": ["Split by percentage", "Rounds to whole dollars"]})
        rnd = self.users().round
        self.hub.mark_done(DESIGN_TASK, "given")
        rnd.page_checked = True
        rnd.runner.last_check = {"inventory": {**self.INVENTORY, "text": "Bill Each", "tone": "dark"}}
        self.assertTrue(asyncio.run(rnd.check_must_haves()))
        note = self.hub.task(DESIGN_TASK)["notes"][-1]
        self.assertIn("Split by percentage (no control mentions percent)", note)
        self.assertIn("the look is light, but the page starts dark", note)
        self.assertNotIn("whole dollars", note)  # the page can't show it
        calls = len(rnd.sched.llm.calls)
        rnd.runner.last_check["inventory"] = {"controls": [{"id": "mode", "kind": "select", "label": "Split",
                                                            "options": ["Equal", "By percentage"]}],
                                              "results": [], "text": "", "tone": "light"}
        self.assertFalse(asyncio.run(rnd.check_must_haves()))
        self.assertEqual(len(rnd.sched.llm.calls), calls)  # the checklist is made once per set of must-haves

    def test_test_users_send_the_page_back_and_ask_about_results(self):
        results = asyncio.run(self.users().run("Split a bill", self.INVENTORY))
        self.assertEqual([r["persona"] for r in results], ["Sam", "Riley", "Alex"])
        self.assertTrue(all(not r["works"] for r in results))
        self.assertEqual(self.hub.task(DESIGN_TASK)["status"], "todo")
        self.assertTrue(any("NaN" in n for n in self.hub.task(DESIGN_TASK)["notes"]))
        asked = [q for q in self.hub.questions() if "says a result is wrong" in q["text"]]
        self.assertEqual(len(asked), 3)
        report = (self.hub.root / "rounds/0.users.md").read_text()
        self.assertIn("seen by the browser", report)
        self.assertIn("a share button", report)

    def test_the_page_is_sent_back_only_so_many_times(self):
        self.hub._write(self.hub.root / "local.json", {**self.hub.local_config(), "user_fixes": 1})
        asyncio.run(self.users().run("Split a bill", self.INVENTORY))
        asyncio.run(self.users(plan=False).run("Split a bill", self.INVENTORY))
        notes = [n for n in self.hub.task(DESIGN_TASK)["notes"] if "test users found" in n]
        self.assertEqual(len(notes), 1)
        self.assertEqual(len([q for q in self.hub.questions() if "says a result is wrong" in q["text"]]), 3)  # not asked twice

    def test_browser_facts_need_no_model(self):
        facts = browser_facts({"ready": True, "errors": ["boom"], "log": [
            {"do": "fill", "target": "tip", "result": "there is no such control on the page"},
            {"do": "fill", "target": "bill", "result": "done", "shows": ["Total undefined"]},
        ]})
        self.assertEqual(len(facts), 3)


BRAINSTORM_REPLY = {
    "brief": {"name": "Book Club Picker", "pitch": "Pick the next book fairly.", "must_have": ["add books", "vote"],
              "nice_to_have": ["history"], "out_of_scope": ["accounts"], "look": "warm and calm"},
    "pushback": ["I'd leave out accounts: everyone uses one shared screen."],
    "questions": [
        {"text": "How do people vote?", "default": "one vote each", "why": "",
         "options": [{"label": "one vote each", "detail": "Everyone picks one book."},
                     {"label": "rank them", "detail": "Everyone orders their favourites."}]},
        {"text": "Keep a history?", "default": "no", "why": "smaller",
         "options": [{"label": "yes", "detail": "Past picks are kept."}, {"label": "no", "detail": "Only this month."}]},
    ],
    "ready": True,
}


class BrainstormTest(HubCase):
    stack = "web"

    def test_a_turn_saves_the_brief_and_your_answers(self):
        session = Brainstorm(self.hub)
        session.start("something for my book club")
        sched = Scheduler(FakeLLM(lambda *a: BRAINSTORM_REPLY), self.hub.local_config(), self.hub.events)
        reply = asyncio.run(session.turn(sched, "something for my book club", 3))
        self.assertEqual(session.answer(reply["questions"][0], ""), "one vote each")  # Enter takes the pick
        self.assertEqual(session.answer(reply["questions"][1], "a"), "yes")
        self.assertEqual(project.load_brief(self.hub)["name"], "Book Club Picker")
        answered = {q["text"]: q["answer"] for q in self.hub.answered_since(0)}
        self.assertEqual(answered, {"How do people vote?": "one vote each", "Keep a history?": "yes"})
        idea = (self.hub.root / "idea.md").read_text()
        project.set_idea_from_brief(self.hub, idea)
        project.set_idea_from_brief(self.hub, (self.hub.root / "idea.md").read_text())
        idea = (self.hub.root / "idea.md").read_text()
        self.assertEqual(idea.count("Agreed in the brainstorm"), 1)
        self.assertIn("vote", idea)
        self.assertEqual(len(Brainstorm(self.hub).talk), 4)  # kept for next time

    def test_project_notes_hold_the_brief_files_and_decisions(self):
        project.save_brief(self.hub, BRAINSTORM_REPLY["brief"])
        rnd = Round(self.hub, FakeLLM(None))
        rnd.number = 0
        rnd.apply_plan(plan_of([TIP]))
        q = self.hub.ask("hub", "Keep a history?", critical=True, choice="no")
        self.hub.answer(q["id"], "yes")
        text = project.render(self.hub).read_text()
        for part in ("# Book Club Picker", "**Must have:**", "`app/tip.js`: Work out tips", "Keep a history? → **yes**",
                     "## Conventions", "## How to run it"):
            self.assertIn(part, text)
        self.assertEqual(project.find("Book Club Picker"), self.hub.project)
        self.assertEqual(project.find(str(self.project)), self.hub.project)


class BringInTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name) / "tips"
        (self.folder / "app").mkdir(parents=True)
        (self.folder / "app/tip.js").write_text(TIP_GOOD)
        (self.folder / "scripts").mkdir()
        (self.folder / "scripts/deploy.py").write_text("print('hi')\n")
        (self.folder / "package.json").write_text('{"name": "tips", "type": "module"}\n')
        (self.folder / "README.md").write_text("# Tips\n\nA tip calculator for the whole table.\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_copy_is_surveyed_into_contracts(self):
        copy = project.copy_folder(self.folder)
        self.assertNotEqual(copy, self.folder)
        self.assertEqual(project.guess_stack(copy), "web")
        hub = LocalHub(copy)
        hub.init(project.first_words(copy), {"stack": "web", "sandbox": False})
        self.assertIn("tip calculator", (hub.root / "idea.md").read_text())
        self.assertEqual((copy / "package.json").read_text(), '{"name": "tips", "type": "module"}\n')  # kept
        plan = {"path": "tip.js", "purpose": "Tips", "exports": TIP["exports"], "imports": ["app/nowhere.js"]}
        sched = Scheduler(FakeLLM(lambda *a: plan), hub.local_config(), hub.events)
        found = asyncio.run(project.survey(hub, sched, hub.events))
        self.assertEqual(found["surveyed"], ["app/tip.js"])
        self.assertEqual(hub.contracts.get("app/tip.js")["imports"], [])
        self.assertEqual(hub.task("app-tip-js")["status"], "done")
        self.assertEqual(hub.task("app-tip-js-tests")["status"], "todo")
        self.assertIn("scripts/deploy.py", (hub.root / "reference.md").read_text())
        self.assertFalse((self.folder / ".hub").exists())  # the original is untouched

    def test_github_urls(self):
        self.assertTrue(project.is_github("https://github.com/fellobello/roughcut"))
        self.assertTrue(project.is_github("https://github.com/a/b.git"))
        self.assertFalse(project.is_github("https://example.com/a/b"))
        self.assertFalse(project.is_github("https://github.com/a/b; rm -rf ~"))


@unittest.skipUnless(HAS_NODE, "needs node")
class TrialTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def trial(self, role, respond):
        from rclocal import trial

        t = trial.Trial(Path(tempfile.mkdtemp(dir=self._tmp.name)) / role, role, "fake:1b")
        t.client = FakeLLM(respond)
        return t, asyncio.run(t.run())

    def test_a_tester_is_scored_on_catching_the_planted_bug(self):
        from rclocal.trial import TESTS

        _, (checks, _) = self.trial("tester", lambda *a: "```js\n" + TESTS + "```")
        self.assertEqual(checks, {"writes a usable test file": True, "passes on correct code": True,
                                  "catches the planted bug": True})
        weak = TESTS.split("test('perPerson splits")[0]  # never tests the share, so misses the bug
        _, (checks, _) = self.trial("tester", lambda *a: "```js\n" + weak + "```")
        self.assertFalse(checks["catches the planted bug"])

    def test_a_brainstormer_is_scored_on_its_questions(self):
        _, (checks, detail) = self.trial("brainstormer", lambda *a: BRAINSTORM_REPLY)
        self.assertTrue(all(checks.values()), checks)
        self.assertIn("How do people vote?", detail)


class KeepAwakeTest(HubCase):
    @unittest.skipUnless(sys.platform == "darwin", "caffeinate is macOS only")
    def test_the_mac_is_kept_awake_only_while_building(self):
        from rclocal.notify import awake

        with awake() as on:
            self.assertTrue(on)
            listed = subprocess.run(["pgrep", "-f", f"caffeinate -i -s -w {os.getpid()}"], capture_output=True)
            self.assertEqual(listed.returncode, 0)
        listed = subprocess.run(["pgrep", "-f", f"caffeinate -i -s -w {os.getpid()}"], capture_output=True)
        self.assertNotEqual(listed.returncode, 0)


class DeskTest(unittest.TestCase):
    """The studio's go-between, with a fake model: no AI is run."""

    def setUp(self):
        from rclocal.desk import Desk

        self.desk = Desk(llm_for=lambda hub: FakeLLM(lambda *a: BRAINSTORM_REPLY))

    def tearDown(self):
        self.desk.stop_app()

    def settle(self):
        for _ in range(200):
            if not self.desk.busy:
                return
            time.sleep(0.02)
        self.fail("the desk never finished")

    def test_a_new_project_starts_a_talk_with_questions(self):
        self.desk.new("something for my book club")
        self.settle()
        state = self.desk.state()
        self.assertEqual(state["project"]["name"], "Book Club Picker")
        self.assertEqual([p["question"]["text"] for p in state["pending"]], ["How do people vote?", "Keep a history?"])
        self.desk.answer(1, "yes")
        self.assertEqual(self.desk.state()["pending"][1]["answer"], "yes")
        self.desk.keep_talking()  # the unanswered one takes the pick
        self.settle()
        answered = {q["text"]: q["answer"] for q in self.desk.hub.questions() if q["status"] == "answered"}
        self.assertEqual(answered, {"How do people vote?": "one vote each", "Keep a history?": "yes"})
        vote = next(q for q in self.desk.hub.questions() if q["text"] == "How do people vote?")
        self.assertEqual(vote["details"]["rank them"], "Everyone orders their favourites.")
        self.assertIsNone(self.desk.stage())
        self.desk.busy = "building"
        self.assertEqual(self.desk.stage({"phase": "build", "tasks": [{"status": "in_progress", "module": "index.html"}]}), "page")
        self.assertEqual(self.desk.stage({"phase": "fix", "tasks": []}), "verify")
        self.desk.busy = None
        whos = [m["who"] for m in self.desk.chat()]
        self.assertEqual(whos[:3], ["desk", "you", "brainstormer"])
        self.assertTrue((self.desk.hub.project / "PROJECT.md").exists())

    def test_an_answer_can_be_changed(self):
        self.desk.new("something for my book club")
        self.settle()
        self.desk.answer(0, "rank them")
        self.desk.answer(0, "one vote each")  # changed your mind before sending it back
        votes = [q for q in self.desk.hub.questions() if q["text"] == "How do people vote?"]
        self.assertEqual([(q["status"], q["answer"]) for q in votes], [("answered", "one vote each")])
        self.assertEqual(self.desk.state()["pending"][0]["answer"], "one vote each")
        mine = [m for m in self.desk.chat() if m.get("ref")]
        self.assertEqual(mine[-1]["ref"]["id"], votes[0]["id"])
        self.assertIn("rank them", mine[-1]["ref"]["options"])
        self.assertTrue(mine[-1]["text"].startswith("Changed my answer"))

    def test_stop_ends_a_brainstorm_turn_partway(self):
        self.desk.new("something for my book club")
        self.settle()
        self.desk.llm_for = lambda hub: FakeLLM(lambda *a: BRAINSTORM_REPLY, seconds=0.3)
        self.desk.say("and a dark mode")
        self.desk.stop()
        self.settle()
        self.assertIn("Stopped", self.desk.chat()[-1]["text"])
        self.assertIsNone(self.desk.state()["stopping"])

    def test_one_job_at_a_time_and_a_project_first(self):
        with self.assertRaises(HubError):
            self.desk.say("hello")
        self.desk.new("something for my book club")
        if self.desk.busy:
            with self.assertRaises(HubError):
                self.desk.say("and another thing")
        self.settle()

    def test_the_file_manager_stays_inside_the_project(self):
        self.desk.new("something for my book club")
        self.settle()
        root = self.desk.hub.project
        (root / "app").mkdir(exist_ok=True)
        (root / "app/votes.js").write_text("export const x = 1;\n")
        listed = {f["path"]: f["dir"] for f in self.desk.files()["files"]}
        self.assertTrue(listed["app"])
        self.assertIn("app/votes.js", listed)
        self.assertFalse(any(p == ".git" or p.startswith(".git/") for p in listed))
        opened = self.desk.read_file("app/votes.js")
        self.assertEqual((opened["language"], opened["read_only"]), ("JavaScript", False))
        self.desk.write_file("app/votes.js", "export const x = 2;\n")
        self.assertIn("2", (root / "app/votes.js").read_text())
        self.assertTrue(self.desk.read_file(".hub/idea.md")["read_only"])
        for bad in ("../outside.txt", "/etc/hosts", "app/../../x"):
            with self.assertRaises(HubError, msg=bad):
                self.desk.write_file(bad, "no")
        for blocked in (".hub/idea.md", ".git/config"):
            with self.assertRaises(HubError, msg=blocked):
                self.desk.write_file(blocked, "no")
        with self.assertRaises(HubError):
            self.desk.read_file("../../.zshrc")

    def test_models_can_be_changed_per_role(self):
        self.desk.new("something for my book club")
        self.settle()
        self.desk.set_model("designer", "qwen2.5-coder:14b")
        self.assertEqual(self.desk.hub.local_config()["models"]["designer"], "qwen2.5-coder:14b")
        self.assertEqual(self.desk.hub.local_config()["models"]["planner"], "qwen3:8b")  # others unchanged
        with self.assertRaises(HubError):
            self.desk.set_model("wizard", "x")


class StudioTest(unittest.TestCase):
    def setUp(self):
        from rclocal.desk import Desk
        from rclocal.studio import make_server

        self.desk = Desk(llm_for=lambda hub: FakeLLM(lambda *a: BRAINSTORM_REPLY))
        self.server = make_server(self.desk, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, path, body=None, token=True, host=None):
        import urllib.error
        import urllib.request

        headers = {"Host": host or f"127.0.0.1:{self.port}", "Content-Type": "application/json"}
        if token:
            headers["X-Token"] = self.server.token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_changes_need_the_token_and_the_right_host(self):
        self.assertEqual(self.call("/api/new", {"idea": "x"}, token=False)[0], 403)
        self.assertEqual(self.call("/api/state", host="evil.example:80")[0], 403)
        status, page = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), page)
        status, body = self.call("/api/state")
        self.assertIsNone(json.loads(body)["project"])
        self.assertEqual(self.call("/api/say", {"text": "hi"})[0], 400)  # no project yet: refused, not crashed

    def test_open_a_project_and_change_a_model(self):
        hub = LocalHub(Path(os.environ["ROUGHCUT_PROJECTS"]) / "studio-test")
        hub.init("A tip calculator", {"stack": "web"})
        self.assertEqual(self.call("/api/open", {"path": str(hub.project)})[0], 200)
        state = json.loads(self.call("/api/state")[1])
        self.assertEqual(state["project"]["path"], str(hub.project))
        self.assertIn("tasks", state["status"])
        self.assertEqual(self.call("/api/model", {"role": "user", "model": "llama3.2:3b"})[0], 200)
        self.assertEqual(hub.local_config()["models"]["user"], "llama3.2:3b")


def ollama_up():
    try:
        OllamaClient().installed()
        return True
    except Exception:
        return False


@unittest.skipUnless(ollama_up(), "Ollama isn't running")
class OllamaReachTest(unittest.TestCase):
    """Only checks Ollama answers and lists models; it runs no model."""

    def test_the_studio_sees_ollama_and_its_models(self):
        from rclocal.desk import Desk

        models = Desk().models()
        self.assertTrue(models["reachable"], models["error"])
        self.assertTrue(models["installed"])
        self.assertEqual([r["role"] for r in models["roles"]], list(protocol.ROLE_JOBS))


CHROME = Runner(Path("."), WEB).chrome()


@unittest.skipUnless(HAS_NODE and CHROME, "needs node and Chrome")
class BrowserTest(unittest.TestCase):
    """The probe and the test-user script in a real headless Chrome."""

    PAGE = (
        '<!doctype html><html><head></head><body><main><label for="bill">Bill</label>'
        '<input id="bill" type="number" data-sample="50"><output id="out"></output></main>'
        '<script type="module">import { tipFor } from "./app/tip.js";\n'
        'const b = document.getElementById("bill");\n'
        'b.addEventListener("input", () => { document.getElementById("out").textContent = tipFor(Number(b.value), 20); });\n'
        'document.body.dataset.ready = "1";</script></body></html>'
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name).resolve()
        (self.project / "app").mkdir()
        (self.project / "app/tip.js").write_text(TIP_GOOD)
        (self.project / "package.json").write_text('{"type": "module"}\n')
        (self.project / "index.html").write_text(self.PAGE)
        self.runner = Runner(self.project, WEB)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_probe_lists_the_controls(self):
        ok, problem = asyncio.run(self.runner.check_page())
        self.assertTrue(ok, problem)
        controls = self.runner.last_check["inventory"]["controls"]
        self.assertEqual([(c["id"], c["label"]) for c in controls], [("bill", "Bill")])

    def test_a_test_user_s_steps_really_happen(self):
        steps = [{"do": "fill", "target": "Bill", "value": "50", "expect": "10"},
                 {"do": "click", "target": "Share", "value": "", "expect": ""}]
        report = asyncio.run(self.runner.try_as_user(steps))
        first, second = report["log"]
        self.assertEqual(first["shows"], ["10"])
        self.assertTrue(first["saw_expected"])
        self.assertIn("no such control", second["result"])
        self.assertFalse((self.project / ".probe.html").exists())


class SchedulerTest(unittest.TestCase):
    def test_one_call_at_a_time_per_model(self):
        live, peak, lock = [0], [0], threading.Lock()

        def respond(role, model, system, user, schema):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.05)
            with lock:
                live[0] -= 1
            return {"notes": "x"}

        with tempfile.TemporaryDirectory() as tmp:
            events = Events(Path(tmp) / "e.jsonl")
            cfg = {"models": {"reporter": "m1"}, "max_parallel_calls": 4}

            async def go():
                sched = Scheduler(FakeLLM(respond), cfg, events)
                chat = [{"role": "system", "content": "Reporter: x"}, {"role": "user", "content": "y"}]
                await asyncio.gather(*(sched.ask("reporter", chat, f"c{i}", reply="reporter") for i in range(4)))

            asyncio.run(go())
            ends = [e for e in events.read() if e["type"] == "llm_end"]
        self.assertEqual(peak[0], 1)
        self.assertEqual(len(ends), 4)


class FindingsTest(unittest.TestCase):
    """What the SplitTip test run turned up, checked without a model."""

    def test_the_same_question_in_other_words_is_noticed(self):
        from rclocal.likeness import same_question, settled

        self.assertTrue(same_question("Should the app support splitting by percentage or equal amounts?", [],
                                      "Split by Percentage or Equal Amounts"))
        self.assertTrue(same_question("Should the app include a dark mode?", [], "Dark Mode Toggle"))
        self.assertFalse(same_question("Who is the target audience?", [], "Should the app include a dark mode toggle?"))
        self.assertTrue(same_question("Colour scheme?", ["Light", "Dark"], "Which theme?", ["dark", "light"]))
        self.assertEqual(settled("The default tip percentage is not set.", [], [], ["Default tip is 15%"]),
                         "Default tip is 15%")

    def test_what_you_said_to_skip_leaves_the_brief(self):
        from rclocal.brainstorm import left_out

        brief = {"must_have": ["No account or server required"], "out_of_scope": [],
                 "nice_to_have": ["Dark mode toggle", "Printable receipt option"]}
        out = left_out(brief, ["nice to have: a dark mode toggle. skip the printable receipt."])
        self.assertEqual(out["nice_to_have"], ["Dark mode toggle"])
        self.assertEqual(out["out_of_scope"], ["Printable receipt option"])
        self.assertEqual(out["must_have"], ["No account or server required"])  # itself a no: kept

    def test_examples_must_be_values(self):
        from rclocal.stacks import literal_prefix

        web, py = STACKS["web"], STACKS["python"]
        self.assertTrue(web.example_ok("splitBill(115, 2)", "[58, 57]"))
        self.assertTrue(web.example_ok("f()", "{ total: 115, shares: [58, 57] }"))
        self.assertFalse(web.example_ok("calculateTotal(100, 15)", "math.round(100 * 1.15)"))
        self.assertFalse(web.example_ok("splitBill(115, 2, true)", "[115 * 0.5, 115 * 0.5]"))
        self.assertFalse(py.example_ok("total(100, 15)", "round(100 * 1.15)"))
        self.assertEqual(literal_prefix("[58, 57] (whole dollars; [57, 58] is fine too)"), "[58, 57]")

    def test_a_parameter_may_not_hide_an_import(self):
        from rclocal.rounds import unshadow

        self.assertEqual(unshadow("function splitBill(total, people, splitByPercentage) -> number[]", {"splitByPercentage"}),
                         "function splitBill(total, people, useSplitByPercentage) -> number[]")
        code = ("import { splitByPercentage } from './split_by_percentage.js';\n"
                "export function splitBill(total, people, splitByPercentage) { return splitByPercentage(total, people); }\n")
        self.assertIn("hides the imported function", STACKS["web"].code_problem(code, "app/bill_parts/split_bill.js"))

    def test_logic_files_and_their_tests_stay_off_the_page(self):
        web = STACKS["web"]
        self.assertIn("logic file", web.code_problem("export function toggle(el) { el.classList.toggle('dark'); }", "app/theme.js"))
        self.assertEqual(web.code_problem("export function next(theme) { return theme === 'dark' ? 'light' : 'dark'; }", "app/theme.js"), "")
        self.assertIn("run in node", web.test_problem("import { test } from 'node:test';\nconst el = document.body;"))

    def test_the_page_check_names_what_the_script_cant_find(self):
        html = ('<main><button aria-pressed="true">A</button><button>B</button><p id="out"></p></main>'
                '<script type="module">document.getElementById("out"); document.getElementById("total");'
                "document.querySelector('button[aria-pressed=\"false\"]'); // getElementById('in a comment')\n</script>")
        self.assertEqual(pages.missing_targets(html), ["#total", 'button[aria-pressed="false"]'])

    def test_test_files_are_held_to_nodes_own_runner(self):
        web = STACKS["web"]
        fixed = web.repair_test("import calculateTotal from '../app/x.js';\ntest('a', () => {});", ["calculateTotal"])
        self.assertIn("import { calculateTotal } from '../app/x.js';", fixed)
        self.assertIn("t.throwsAsync", web.test_problem(
            "import { test } from 'node:test';\nimport assert from 'node:assert';\ntest('a', async (t) => { await t.throwsAsync(f); });"))

    def test_answers_about_stuck_files_are_not_planned_with(self):
        from rclocal.rounds import STUCK_OPTIONS, is_stuck_question

        labels = [o["label"] for o in STUCK_OPTIONS]
        self.assertTrue(is_stuck_question({"task": "app-x-js", "options": labels}))
        self.assertFalse(is_stuck_question({"task": "app-x-js", "options": ["Right", "Wrong"]}))

    def test_a_split_file_passes_its_parts_on(self):
        web = STACKS["web"]
        shim = web.reexport("app/bill.js", {"splitBill": "app/bill_parts/split_bill.js"})
        self.assertIn("export { splitBill } from './bill_parts/split_bill.js';", shim)
        self.assertEqual(web.syntax_error(shim, "app/bill.js"), "")


class BlameTest(unittest.TestCase):
    """Who is wrong when a file fails, settled without the reviewer where the facts decide it."""

    TIP_CONTRACT = {"path": "app/tip.js", "test_path": "tests/app_tip.test.mjs", "imports": [], "exports": [
        export("tipFor", "function tipFor(bill, percent) -> number",
               examples=[{"call": "tipFor(100, 15)", "returns": "15"}])]}
    TESTS = ("import { test } from 'node:test';\nimport assert from 'node:assert';\nimport { tipFor } from '../app/tip.js';\n"
             "test('tips', () => { assert.strictEqual(tipFor(200, 10), 20); });\n")
    EXAMPLES = ("\n// The examples in the contract, exactly as written there.\ntest('the contract examples hold', () => {\n"
                "  assert.deepStrictEqual(__contract.tipFor(100, 15), 15);\n});\n")
    CODE = "export function tipFor(bill, percent) { return bill * percent / 100; }\n"

    def settle(self, tests, failure, contract=None, code=None):
        from rclocal.blame import settle

        return settle(STACKS["web"], contract or self.TIP_CONTRACT, tests, code or self.CODE, failure)

    @unittest.skipUnless(HAS_NODE, "needs node")
    def test_the_facts_decide_where_they_can(self):
        tests = self.TESTS + self.EXAMPLES
        self.assertEqual(self.settle(tests, "✔ tips (0.4ms)\n✖ the contract examples hold (0.9ms)\n")["blame"], "code")
        self.assertEqual(self.settle(tests, "✖ tips (0.4ms)\n✔ the contract examples hold (0.9ms)\n✖ failing tests:\n✖ tips\n")["blame"], "test")
        self.assertIsNone(self.settle(tests, "✖ tips (0.4ms)\nTypeError: bill.toFixed is not a function\n"))
        self.assertIsNone(self.settle(self.TESTS, "✖ tips (0.4ms)\n"))  # no examples to go by: the reviewer decides
        wrong = tests.replace("tipFor(200, 10), 20", "tipFor(100, 15), 16")
        self.assertIn("expects 16 for tipFor(100, 15)", self.settle(wrong, "✖ tips\n")["reason"])
        extra = tests.replace("{ tipFor }", "{ tipFor, roundTip }")
        self.assertIn("roundTip, which the contract doesn't have", self.settle(extra, "")["reason"])
        missing = "SyntaxError: The requested module '../app/tip.js' does not provide an export named 'tipFor'"
        self.assertEqual(self.settle(tests, missing, code="export const x = 1;\n")["blame"], "code")
        clash = {**self.TIP_CONTRACT, "exports": [export("tipFor", "function tipFor(bill, percent) -> number", examples=[
            {"call": "tipFor(100, 15)", "returns": "15"}, {"call": "tipFor(100,15)", "returns": "15.5"}])]}
        self.assertEqual(self.settle(tests, "✖ tips\n", contract=clash)["blame"], "contract")

    def test_the_reviewer_must_quote_the_contract_to_blame_it(self):
        from rclocal.blame import failed_tests, quoted

        brief = "app/tip.js\n- function tipFor(bill, percent) -> number: the tip for a bill"
        self.assertTrue(quoted("tipFor(bill, percent) -> number", brief))
        self.assertFalse(quoted("tipFor(bill, people)", brief))
        self.assertFalse(quoted("", brief))
        self.assertEqual(failed_tests("FAIL: test_x (tests.test_app_a.ATest.test_x)\n"), ["test_x (tests.test_app_a.ATest.test_x)"])


class SharedFunctionTest(HubCase):
    """A function two files both define: one copy goes, if the tests still pass."""

    def test_a_copied_function_is_imported_from_the_file_that_has_it(self):
        rnd = Round(self.hub, FakeLLM(None))
        rnd.number = 0
        piece = {"path": "app/parts.py", "purpose": "Parts", "imports": [],
                 "exports": [export("to_celsius", "def to_celsius(f: float) -> float"), export("half", "def half(x: float) -> float")]}
        rnd.apply_plan(plan_of([CONVERT, piece]))
        self.hub._write_text(self.project / "app/convert.py", CONVERT_GOOD)
        self.hub._write_text(self.project / "app/parts.py", CONVERT_GOOD + "\n\ndef half(x: float) -> float:\n    return x / 2\n")
        self.hub._write_text(self.project / "tests/test_app_parts.py", "import unittest\nfrom app.parts import to_celsius, half\n\n"
                             "class T(unittest.TestCase):\n    def test_it(self):\n        self.assertEqual(half(to_celsius(212)), 50)\n")
        rnd.write_packages()
        asyncio.run(rnd.share_duplicates())
        parts = (self.project / "app/parts.py").read_text()
        self.assertIn("from app.convert import to_celsius", parts)
        self.assertNotIn("def to_celsius", parts)
        self.assertIn("app/convert.py", self.hub.contracts.get("app/parts.py")["imports"])
        self.assertIn("def to_celsius", (self.project / "app/convert.py").read_text())

    def test_web_files_share_a_function_the_same_way(self):
        web = STACKS["web"]
        code = ("import { a } from './a.js';\n\nexport function tipFor(bill, pct = { x: 1 }) {\n  const s = '}';\n"
                "  return bill * pct / 100;\n}\n\nexport function other() { return 1; }\n")
        shared = web.share(code, "tipFor", "app/parts/x.js", "app/tip.js")
        self.assertIn("import { tipFor } from '../tip.js';\nexport { tipFor };", shared)
        self.assertNotIn("function tipFor", shared)
        self.assertIn("export function other()", shared)
        self.assertEqual(web.definitions(shared), {"other"})


class FailureDigestTest(unittest.TestCase):
    def test_the_values_are_kept_and_the_stack_frames_dropped(self):
        from rclocal.rounds import failure_digest

        out = ("✖ splits (1.7ms)\nℹ tests 1\n  AssertionError [ERR_ASSERTION]: Expected values to be strictly deep-equal:\n"
               "      at TestContext.<anonymous> (file:///x/a.test.mjs:3:31)\n      at Test.run (node:internal/test_runner/test:1382:25) {\n"
               "    generatedMessage: true,\n    actual: [ 50, 49 ],\n    expected: [ 49, 50 ],\n  }\n")
        digest = failure_digest(out, 1000)
        self.assertIn("actual: [ 50, 49 ]", digest)
        self.assertIn("expected: [ 49, 50 ]", digest)
        self.assertNotIn("node:internal", digest)
        self.assertNotIn("ℹ tests", digest)
        self.assertLessEqual(len(failure_digest(out * 50, 300)), 300)


class LiveTest(unittest.TestCase):
    def test_what_a_model_writes_is_kept_for_the_studio(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = Events(Path(tmp) / "e.jsonl")
            sched = Scheduler(FakeLLM(lambda *a: {"notes": "all built"}), {"models": {"reporter": "m1"},
                              "max_parallel_calls": 1}, events)
            chat = [{"role": "system", "content": "Reporter: x"}, {"role": "user", "content": "y"}]
            asyncio.run(sched.ask("reporter", chat, "report", reply="reporter"))
            from rclocal.scheduler import Live
            live = Live.read(Path(tmp) / "live.json")
        self.assertEqual(live["running"], [])
        self.assertEqual((live["recent"][0]["role"], live["recent"][0]["outcome"]), ("reporter", "done"))
        self.assertIn("all built", live["recent"][0]["text"])


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name).resolve()
        (self.project / "tests").mkdir()
        (self.project / "tests/__init__.py").write_text("")

    def tearDown(self):
        self._tmp.cleanup()

    def run_file(self, name, body, **kw):
        (self.project / f"tests/{name}.py").write_text(body)
        return asyncio.run(Runner(self.project, PY, **kw).run(f"tests/{name}.py"))

    def test_endless_tests_are_stopped(self):
        passed, output = self.run_file("test_loop", "while True:\n    pass\n", timeout=1, sandbox=False)
        self.assertFalse(passed)
        self.assertIn("stopped", output)

    def test_your_environment_is_not_passed_on(self):
        os.environ["ROUGHCUT_FAKE_SECRET"] = "hunter2"
        try:
            passed, output = self.run_file(
                "test_env",
                "import os, unittest\nclass T(unittest.TestCase):\n"
                "    def test_no_secret(self):\n        self.assertNotIn('ROUGHCUT_FAKE_SECRET', os.environ)\n",
                sandbox=False,
            )
        finally:
            del os.environ["ROUGHCUT_FAKE_SECRET"]
        self.assertTrue(passed, output)

    @unittest.skipUnless(sys.platform == "darwin", "sandbox-exec is macOS only")
    def test_the_sandbox_blocks_internet_outside_writes_and_home_reads(self):
        outside = Path.home() / ".roughcut-local-sandbox-probe"
        passed, output = self.run_file(
            "test_escape",
            "import socket, unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_internet(self):\n"
            "        with self.assertRaises(OSError):\n"
            "            socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
            "    def test_write_outside(self):\n"
            "        with self.assertRaises(OSError):\n"
            f"            open({str(outside)!r}, 'w')\n"
            "    def test_read_home(self):\n"
            "        with self.assertRaises(OSError):\n"
            f"            open({str(Path.home() / '.zshrc')!r}).read()\n"
            "    def test_localhost_works(self):\n"
            "        s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
            "        socket.create_connection(s.getsockname(), timeout=3).close()\n",
            sandbox=True,
        )
        self.assertTrue(passed, output)
        self.assertFalse(outside.exists())


class StatusTest(HubCase):
    def test_snapshot_after_a_round(self):
        self.run_round(py_script({"app/convert.py": [CONVERT_GOOD], "app/report.py": [REPORT_GOOD]}))
        state = snapshot(self.hub, FakeLLM(None))
        self.assertEqual(state["phase"], "finished")
        self.assertFalse(state["running"])
        self.assertEqual(len(state["tasks"]), 4)
        self.assertEqual(state["active"], [])
        self.assertIn("load", state["resources"])
        self.assertIn("sandbox", state["box"])
        json.dumps(state)  # the page fetches it as JSON


if __name__ == "__main__":
    unittest.main()
