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

from rclocal import protocol  # noqa: E402
from rclocal.contracts import ContractError, file_id  # noqa: E402
from rclocal.events import Events  # noqa: E402
from rclocal.hub_local import LocalHub  # noqa: E402
from rclocal.llm import FakeLLM  # noqa: E402
from rclocal.rounds import DESIGN_TASK, Round, check_code, clean_code  # noqa: E402
from rclocal.runner import Runner  # noqa: E402
from rclocal.scheduler import Scheduler  # noqa: E402
from rclocal.stacks import STACKS, js_guard, py_guard  # noqa: E402
from rclocal.status import snapshot  # noqa: E402

PY, WEB = STACKS["python"], STACKS["web"]
HAS_NODE = bool(shutil.which("node"))


def export(name, signature, description="does it"):
    return {"name": name, "signature": signature, "description": description}


def plan_of(files, complete=True):
    return {
        "name": "temps",
        "summary": "temperature converter",
        "files": files,
        "conventions": ["Round to one decimal place in text."],
        "guesses": [{"text": "Which interface?", "choice": "functions only", "why": "smallest", "options": ["CLI"]}],
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
    '<!doctype html><html><head><link rel="stylesheet" href="styles.css"></head><body>'
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

    def __init__(self, builds, tests, plan, verdict="code", steps=None):
        self.builds = builds
        self.tests = tests
        self.plan = plan
        self.verdict = verdict
        self.steps = steps or {}  # path -> list of actions to take before writing
        self.tries = {}
        self.taken = {}

    def _owner(self, system):
        return re.search(r"You own exactly one file, (\S+),", system).group(1)

    def __call__(self, role, model, system, user, schema):
        if role == "planner":
            return self.plan
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
            return {"blame": self.verdict, "reason": "it subtracts but never scales"}
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
        self.hub.init("A temperature converter", {"stack": self.stack, "sandbox": False, "agent_steps": 3})
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
        data_roles = {"planner", "reviewer", "reporter"}
        self.assertEqual(set(protocol.ROLES), data_roles | set(protocol.CODE_ROLES))
        self.assertLessEqual(data_roles | {"step", "notes"}, set(protocol.REPLIES))

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

    def test_failing_code_is_retried_then_asked_about(self):
        summary = self.run_round(py_script({"app/convert.py": [CONVERT_BAD], "app/report.py": [REPORT_GOOD]}))
        task = self.hub.task("app-convert-py")
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["attempts"], 3)
        self.assertEqual(self.statuses()["app-report-py"], "todo")  # waits on convert
        self.assertEqual(len(summary["open_questions"]), 1)

    def test_blaming_the_test_rewrites_it(self):
        self.run_round(py_script({"app/convert.py": [CONVERT_BAD], "app/report.py": [REPORT_GOOD]}, verdict="test"))
        self.assertGreaterEqual(self.hub.task("app-convert-py-tests")["attempts"], 2)
        self.assertTrue(any("tests redone" in n for n in self.hub.task("app-convert-py")["notes"]))

    def test_answering_rebuilds_on_the_next_round(self):
        script = py_script({"app/convert.py": [CONVERT_BAD], "app/report.py": [REPORT_GOOD]})
        self.run_round(script)
        self.hub.answer(self.hub.status()["open_questions"][0], text="Use (f - 32) * 5 / 9")
        script.builds["app/convert.py"] = [CONVERT_GOOD]
        script.plan = {**script.plan, "files": [], "guesses": []}
        summary = self.run_round(script)
        self.assertTrue(summary["tests_passed"])
        self.assertEqual(set(self.statuses().values()), {"done"})

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

    def test_a_web_round_builds_logic_then_a_checked_page(self):
        checked = []

        async def fake_check(screenshot=None):
            checked.append(screenshot)
            return True, ""

        script = Script({"app/tip.js": [TIP_GOOD]}, WEB_TESTS, plan_of([TIP]))
        rnd = Round(self.hub, FakeLLM(script))
        rnd.runner.check_page = fake_check
        summary = asyncio.run(rnd.run())
        self.assertTrue(summary["tests_passed"])
        self.assertEqual(self.statuses()[DESIGN_TASK], "done")
        self.assertEqual((self.project / "index.html").read_text(), PAGE + "\n")
        self.assertTrue((self.project / "package.json").exists())
        self.assertEqual(len(checked), 1)

    def test_the_page_may_only_import_real_exports(self):
        rnd = Round(self.hub, FakeLLM(None))
        rnd.number = 0
        rnd.apply_plan(plan_of([TIP]))
        self.assertEqual(rnd.page_problem(PAGE), "")
        self.assertIn("doesn't export tipTotal", rnd.page_problem(PAGE.replace("{ tipFor }", "{ tipFor, tipTotal }")))
        self.assertIn("doesn't exist", rnd.page_problem(PAGE.replace("./app/tip.js", "./app/split.js")))
        self.assertIn("dataset.ready", rnd.page_problem(PAGE.replace('document.body.dataset.ready = "1";', "")))
        self.assertIn("safety guard", rnd.page_problem(PAGE.replace("<main", '<script src="https://cdn.x/y.js"></script><main')))


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
