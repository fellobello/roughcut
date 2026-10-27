import asyncio
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rclocal import protocol  # noqa: E402
from rclocal.contracts import ContractError, file_id, test_path  # noqa: E402
from rclocal.events import Events  # noqa: E402
from rclocal.hub_local import LocalHub  # noqa: E402
from rclocal.llm import FakeLLM  # noqa: E402
from rclocal.rounds import Round, check_code, clean_code  # noqa: E402
from rclocal.runner import Runner  # noqa: E402
from rclocal.scheduler import Scheduler  # noqa: E402
from rclocal.status import snapshot  # noqa: E402


def export(name, signature, description="does it"):
    return {"name": name, "signature": signature, "description": description}


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

CONVERT_TESTS = """import unittest
from app.convert import to_celsius

class ConvertTest(unittest.TestCase):
    def test_freezing(self):
        self.assertEqual(to_celsius(32), 0)

    def test_boiling(self):
        self.assertEqual(to_celsius(212), 100)
"""
REPORT_TESTS = """import unittest
from app.report import describe

class ReportTest(unittest.TestCase):
    def test_describe(self):
        self.assertEqual(describe(212), "100.0 C")
"""
CONVERT_BAD = "def to_celsius(f: float) -> float:\n    return f - 32\n"
CONVERT_GOOD = "def to_celsius(f: float) -> float:\n    return (f - 32) * 5 / 9\n"
REPORT_GOOD = (
    "from app.convert import to_celsius\n\n"
    "def describe(f: float) -> str:\n    return f'{to_celsius(f):.1f} C'\n"
)


def written(user):
    return re.search(r"## Write\n(\S+)", user).group(1)


class Script:
    """A fake team: answers each role from tables, counting calls per file."""

    def __init__(self, builds, tests=None, plan=None, verdict="code"):
        self.builds = builds  # path -> list of file contents, one per try (the last repeats)
        self.tests = tests or {"tests/test_app_convert.py": CONVERT_TESTS, "tests/test_app_report.py": REPORT_TESTS}
        self.plan = plan or {
            "name": "temps",
            "summary": "temperature converter",
            "files": [CONVERT, REPORT],
            "conventions": ["Round to one decimal place in text."],
            "guesses": [{"text": "Which interface?", "choice": "functions only", "why": "smallest", "options": ["CLI"]}],
            "complete": True,
        }
        self.verdict = verdict
        self.tries = {}

    def __call__(self, role, model, system, user, schema):
        if role == "planner":
            return self.plan
        if role == "tester":
            return {"content": self.tests[written(user)], "decisions": []}
        if role == "builder":
            path = written(user)
            n = self.tries.get(path, 0)
            self.tries[path] = n + 1
            versions = self.builds[path]
            return {
                "content": "```python\n" + versions[min(n, len(versions) - 1)] + "```",
                "decisions": [{"text": f"How {path} rounds", "choice": "no rounding", "why": "simple"}],
                "contract_requests": [],
                "lesson": "Convert with (f - 32) * 5 / 9." if path == "app/convert.py" else "",
            }
        if role == "reviewer":
            return {"blame": self.verdict, "reason": "it subtracts but never scales"}
        if role == "reporter":
            return {"notes": "Built the converter."}
        raise AssertionError(f"unexpected role {role}")


class HubCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "proj"
        self.hub = LocalHub(self.project)
        self.hub.init("A temperature converter", {"sandbox": False, "fix_tries": 2})
        subprocess.run(["git", "-C", str(self.project), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.project), "config", "user.name", "test"], check=True)

    def tearDown(self):
        self._tmp.cleanup()

    def run_round(self, script):
        return asyncio.run(Round(self.hub, FakeLLM(script)).run())

    def statuses(self):
        return {t["id"]: t["status"] for t in self.hub.tasks()}


class ProtocolTest(unittest.TestCase):
    def test_every_role_has_a_reply_schema(self):
        self.assertEqual(set(protocol.REPLIES), {"planner", "tester", "builder", "reviewer", "reporter"})

    def test_validate_reports_the_missing_field(self):
        with self.assertRaisesRegex(protocol.ProtocolError, "lesson"):
            protocol.validate({"content": "", "decisions": [], "contract_requests": []}, protocol.FILE)

    def test_validate_checks_enums_and_nested_items(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate({"blame": "nobody", "reason": "x"}, protocol.REVIEW)
        with self.assertRaisesRegex(protocol.ProtocolError, r"files\[0\]"):
            protocol.validate({**Script({}).plan, "files": [{"path": "a.py"}]}, protocol.PLAN)

    def test_unknown_or_incomplete_events_are_refused(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.check_event({"type": "gossip"})
        with self.assertRaises(protocol.ProtocolError):
            protocol.check_event({"type": "task_end", "task": "x"})


class ContractsTest(HubCase):
    def test_paths_map_to_ids_and_tests(self):
        self.assertEqual(file_id("app/geo_tools.py"), "app-geo-tools-py")
        self.assertEqual(test_path("app/geo/core.py"), "tests/test_app_geo_core.py")

    def test_bad_paths_are_refused(self):
        for path in ("../x.py", "/abs.py", "app/Geo.py", "tests/test_x.py", "app/geo.js"):
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

    def test_syntax_errors_and_missing_exports_are_caught(self):
        self.assertIn("SyntaxError", check_code("def f(:\n", "a.py"))
        self.assertIn("to_f", check_code("def g(): pass\n", "a.py", [export("to_f", "def to_f()")]))
        self.assertEqual(check_code("X: int = 1\nclass Y: pass\n", "a.py", [export("X", "X"), export("Y", "Y")]), "")


class RoundTest(HubCase):
    def test_a_round_builds_every_file_and_passes(self):
        script = Script({"app/convert.py": [CONVERT_GOOD], "app/report.py": [REPORT_GOOD]})
        summary = self.run_round(script)
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
        script = Script({"app/convert.py": [CONVERT_GOOD], "app/report.py": [REPORT_GOOD]})
        asyncio.run(Round(self.hub, FakeLLM(script), echo=events.append).run())
        order = [e["task"] for e in events if e["type"] == "task_end"]
        self.assertLess(order.index("app-convert-py-tests"), order.index("app-convert-py"))
        self.assertLess(order.index("app-convert-py"), order.index("app-report-py"))

    def test_a_failing_try_is_fixed_and_leaves_a_lesson(self):
        script = Script({"app/convert.py": [CONVERT_BAD, CONVERT_GOOD], "app/report.py": [REPORT_GOOD]})
        self.run_round(script)
        self.assertEqual(script.tries["app/convert.py"], 2)
        self.assertEqual(self.hub.task("app-convert-py")["summary"], "app/convert.py passes its tests after 2 tries")
        self.assertIn("(f - 32) * 5 / 9", self.hub.memory.lessons())

    def test_failing_code_is_retried_then_asked_about(self):
        script = Script({"app/convert.py": [CONVERT_BAD], "app/report.py": [REPORT_GOOD]})
        summary = self.run_round(script)
        task = self.hub.task("app-convert-py")
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["attempts"], 3)
        self.assertEqual(self.statuses()["app-report-py"], "todo")  # waits on convert
        self.assertEqual(len(summary["open_questions"]), 1)

    def test_blaming_the_test_rewrites_it(self):
        script = Script({"app/convert.py": [CONVERT_BAD], "app/report.py": [REPORT_GOOD]}, verdict="test")
        self.run_round(script)
        self.assertGreaterEqual(self.hub.task("app-convert-py-tests")["attempts"], 2)
        self.assertTrue(any("tests redone" in n for n in self.hub.task("app-convert-py")["notes"]))

    def test_answering_rebuilds_on_the_next_round(self):
        script = Script({"app/convert.py": [CONVERT_BAD], "app/report.py": [REPORT_GOOD]})
        self.run_round(script)
        question = self.hub.status()["open_questions"][0]
        self.hub.answer(question, text="Use (f - 32) * 5 / 9")
        script.builds["app/convert.py"] = [CONVERT_GOOD]
        script.plan = {**script.plan, "files": [], "guesses": []}
        summary = self.run_round(script)
        self.assertTrue(summary["tests_passed"])
        self.assertEqual(set(self.statuses().values()), {"done"})

    def test_contract_requests_extend_the_other_file(self):
        script = Script({"app/convert.py": [CONVERT_GOOD], "app/report.py": [REPORT_GOOD]})
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
                await asyncio.gather(*(sched.ask("reporter", "Reporter: x", "y", f"c{i}") for i in range(4)))

            asyncio.run(go())
            ends = [e for e in events.read() if e["type"] == "llm_end"]
        self.assertEqual(peak[0], 1)
        self.assertEqual(len(ends), 4)


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        (self.project / "tests").mkdir()
        (self.project / "tests/__init__.py").write_text("")

    def tearDown(self):
        self._tmp.cleanup()

    def test_failures_are_traced_to_their_test_files(self):
        (self.project / "tests/test_app_one.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 2)\n"
        )
        (self.project / "tests/test_app_two.py").write_text("from app.missing import x\n")
        passed, output = asyncio.run(Runner(self.project, sandbox=False).run())
        self.assertFalse(passed)
        self.assertEqual(Runner.failing_test_files(output), ["tests/test_app_one.py", "tests/test_app_two.py"])

    def test_endless_tests_are_stopped(self):
        (self.project / "tests/test_app_loop.py").write_text("while True:\n    pass\n")
        passed, output = asyncio.run(Runner(self.project, timeout=1, sandbox=False).run("tests/test_app_loop.py"))
        self.assertFalse(passed)
        self.assertIn("stopped", output)

    @unittest.skipUnless(sys.platform == "darwin", "sandbox-exec is macOS only")
    def test_the_sandbox_blocks_network_and_outside_writes(self):
        outside = Path.home() / ".roughcut-local-sandbox-probe"
        (self.project / "tests/test_app_escape.py").write_text(
            "import socket, unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_network(self):\n"
            "        with self.assertRaises(OSError):\n"
            "            socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
            "    def test_write(self):\n"
            "        with self.assertRaises(OSError):\n"
            f"            open({str(outside)!r}, 'w')\n"
        )
        passed, output = asyncio.run(Runner(self.project, sandbox=True).run("tests/test_app_escape.py"))
        self.assertTrue(passed, output)
        self.assertFalse(outside.exists())


class StatusTest(HubCase):
    def test_snapshot_after_a_round(self):
        self.run_round(Script({"app/convert.py": [CONVERT_GOOD], "app/report.py": [REPORT_GOOD]}))
        state = snapshot(self.hub, FakeLLM(None))
        self.assertEqual(state["phase"], "finished")
        self.assertFalse(state["running"])
        self.assertEqual(len(state["tasks"]), 4)
        self.assertEqual(state["active"], [])
        self.assertIn("load", state["resources"])
        json.dumps(state)  # the page fetches it as JSON


if __name__ == "__main__":
    unittest.main()
