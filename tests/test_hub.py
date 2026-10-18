import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from hub import HUB_OWNER, Hub, HubError  # noqa: E402


def git(project, *args):
    return subprocess.run(
        ["git", "-C", str(project), "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        check=True, capture_output=True, text=True,
    )


class HubTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "proj"
        self.hub = Hub(self.project)
        self.hub.init("An app that does x, y and z")

    def tearDown(self):
        self._tmp.cleanup()

    def commit_all(self, message="plan"):
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", message)

    def build(self, task_id):
        """Run one task through a wave and finish it as done."""
        started = [t["id"] for t in self.hub.start_wave()["tasks"]]
        self.assertIn(task_id, started)
        return self.hub.finish(task_id, "done", "built it")


class InitTest(HubTest):
    def test_creates_state_and_its_own_repo(self):
        self.assertEqual(self.hub.config()["round"], 0)
        self.assertEqual((self.project / ".hub" / "idea.md").read_text(), "An app that does x, y and z\n")
        self.assertTrue((self.project / ".git").exists())
        self.assertIn(".worktrees/", (self.project / ".gitignore").read_text().splitlines())

    def test_refuses_a_second_init(self):
        with self.assertRaises(HubError):
            self.hub.init("again")

    def test_nested_project_gets_its_own_repo(self):
        inner = self.project / "inner"
        Hub(inner).init("nested idea")
        top = git(inner, "rev-parse", "--show-toplevel").stdout.strip()
        self.assertEqual(Path(top).resolve(), inner.resolve())

    def test_commands_need_a_hub(self):
        with self.assertRaises(HubError):
            Hub(Path(self._tmp.name) / "nothing").status()


class TaskGraphTest(HubTest):
    def test_rejects_bad_ids_and_duplicates(self):
        with self.assertRaises(HubError):
            self.hub.add_task("Bad Id", "title")
        self.hub.add_task("api", "Build the API")
        with self.assertRaises(HubError):
            self.hub.add_task("api", "Again")

    def test_check_finds_missing_dependencies_and_cycles(self):
        self.hub.add_task("a", "A", depends_on=["b"])
        self.hub.add_task("b", "B", depends_on=["a"])
        self.hub.add_task("c", "C", depends_on=["ghost"])
        errors = self.hub.check()
        self.assertIn("c depends on missing task ghost", errors)
        self.assertTrue(any(e.startswith("dependency cycle: a -> b -> a") for e in errors), errors)

    def test_ready_waits_for_dependencies(self):
        self.hub.add_task("data", "Data layer")
        self.hub.add_task("ui", "UI", depends_on=["data"])
        self.hub.add_task("ui-tests", "UI tests", kind="test", module="ui")
        self.assertEqual([t["id"] for t in self.hub.ready()], ["data", "ui-tests"])
        self.commit_all()
        self.build("data")
        self.assertEqual([t["id"] for t in self.hub.ready()], ["ui"])

    def test_start_wave_refuses_an_invalid_graph(self):
        self.hub.add_task("a", "A", depends_on=["ghost"])
        self.commit_all()
        with self.assertRaises(HubError):
            self.hub.start_wave()


class WaveTest(HubTest):
    def test_needs_a_commit(self):
        self.hub.add_task("a", "A")
        with self.assertRaises(HubError):
            self.hub.start_wave()

    def test_gives_each_task_a_branch_and_worktree(self):
        self.hub.add_task("a", "A")
        self.hub.add_task("b", "B")
        self.commit_all()
        started = self.hub.start_wave()["tasks"]
        self.assertEqual([t["id"] for t in started], ["a", "b"])
        for task in started:
            self.assertEqual(task["status"], "in_progress")
            self.assertEqual(task["attempts"], 1)
            self.assertTrue(Path(task["worktree"]).is_dir())
            self.assertEqual(git(task["worktree"], "branch", "--show-current").stdout.strip(), task["branch"])
        self.assertEqual(self.hub.start_wave()["tasks"], [])

    def test_limit(self):
        for task_id in ("a", "b", "c"):
            self.hub.add_task(task_id, task_id.upper())
        self.commit_all()
        self.assertEqual(len(self.hub.start_wave(limit=2)["tasks"]), 2)

    def test_finish_rules(self):
        self.hub.add_task("a", "A")
        with self.assertRaises(HubError):
            self.hub.finish("a", "done")  # not started
        self.commit_all()
        self.hub.start_wave()
        with self.assertRaises(HubError):
            self.hub.finish("a", "blocked")  # blocked needs a question
        self.hub.ask("a", "Which map provider?", critical=True)
        with self.assertRaises(HubError):
            self.hub.finish("a", "done")  # can't be done with an open question
        self.assertEqual(self.hub.finish("a", "blocked", "waiting")["status"], "blocked")

    def test_cleanup_deletes_merged_branches_only(self):
        self.hub.add_task("a", "A")
        self.hub.add_task("b", "B")
        self.commit_all()
        started = {t["id"]: t for t in self.hub.start_wave()["tasks"]}
        for task_id, task in started.items():
            (Path(task["worktree"]) / f"{task_id}.txt").write_text(task_id)
            git(task["worktree"], "add", "-A")
            git(task["worktree"], "commit", "-q", "-m", task_id)
            self.hub.finish(task_id, "done")
        git(self.project, "merge", "-q", "--no-ff", started["a"]["branch"], "-m", "merge a")
        merged = self.hub.cleanup("a")
        unmerged = self.hub.cleanup("b")
        self.assertTrue(merged["worktree_removed"] and merged["branch_deleted"])
        self.assertTrue(unmerged["worktree_removed"])
        self.assertFalse(unmerged["branch_deleted"])
        self.assertTrue((self.project / "a.txt").exists())

    def test_retry_keeps_the_previous_branch(self):
        self.hub.add_task("a", "A")
        self.commit_all()
        first = self.hub.start_wave()["tasks"][0]
        (Path(first["worktree"]) / "a.txt").write_text("first try")
        git(first["worktree"], "add", "-A")
        git(first["worktree"], "commit", "-q", "-m", "first try")
        self.hub.finish("a", "done")
        self.hub.cleanup("a")  # unmerged, so the branch stays
        self.hub.reopen("a", "tests fail")
        second = self.hub.start_wave()["tasks"][0]
        self.assertEqual(second["attempts"], 2)
        self.assertEqual(second["previous_branches"], [first["branch"]])
        self.assertNotEqual(second["branch"], first["branch"])


class QuestionTest(HubTest):
    def test_blocking_question_holds_tasks_until_answered(self):
        self.hub.add_task("map", "Map")
        self.hub.add_task("routes", "Routes")
        question = self.hub.ask(HUB_OWNER, "Avoid cameras completely or pass as few as possible?",
                                critical=True, affects=["map", "routes"])
        self.assertEqual(question["id"], "hub.1")
        self.assertEqual(question["status"], "open")
        self.assertEqual(self.hub.ready(), [])
        result = self.hub.answer("hub.1", "As few as possible")
        self.assertEqual(question["task"], HUB_OWNER)
        self.assertEqual([t["id"] for t in self.hub.ready()], ["map", "routes"])
        self.assertIn("hub.1 answered: As few as possible", self.hub.task("map")["notes"])
        self.assertEqual(result["reworked"], [])

    def test_blocked_task_returns_to_todo_when_answered(self):
        self.hub.add_task("a", "A")
        self.commit_all()
        self.hub.start_wave()
        self.hub.ask("a", "Which API key source?", critical=True)
        self.hub.finish("a", "blocked")
        self.assertEqual(self.hub.answer("a.1", "Environment variable")["unblocked"], ["a"])
        self.assertEqual(self.hub.task("a")["status"], "todo")

    def test_decisions_need_a_choice_and_never_block(self):
        self.hub.add_task("a", "A")
        with self.assertRaises(HubError):
            self.hub.ask("a", "Which test runner?")
        decision = self.hub.ask("a", "Which test runner?", choice="pytest", why="most common")
        guess = self.hub.ask("hub", "Which language?", critical=True, choice="Python")
        self.assertEqual((decision["status"], guess["status"]), ("decided", "decided"))
        self.assertEqual([t["id"] for t in self.hub.ready()], ["a"])

    def test_changing_a_decision_reworks_done_tasks(self):
        self.hub.add_task("a", "A")
        self.commit_all()
        self.build("a")
        self.hub.ask("hub", "Which language?", critical=True, choice="Python", affects=["a"])
        result = self.hub.answer("hub.1", "TypeScript")
        self.assertEqual(result["reworked"], ["a"])
        task = self.hub.task("a")
        self.assertEqual(task["status"], "todo")
        self.assertIn("hub.1 answered: TypeScript", task["notes"])

    def test_keeping_a_decision_changes_nothing(self):
        self.hub.add_task("a", "A")
        self.commit_all()
        self.build("a")
        self.hub.ask("hub", "Which language?", critical=True, choice="Python", affects=["a"])
        result = self.hub.answer("hub.1", keep=True)
        self.assertEqual(result["question"]["answer"], "Python")
        self.assertEqual(result["reworked"], [])
        self.assertEqual(self.hub.task("a")["status"], "done")
        self.hub.ask("hub", "Open question", critical=True)
        with self.assertRaises(HubError):
            self.hub.answer("hub.2", keep=True)  # nothing to keep: it's a question

    def test_ids_count_per_owner(self):
        self.hub.add_task("a", "A")
        ids = [
            self.hub.ask("a", "one", choice="x")["id"],
            self.hub.ask("hub", "two", choice="y")["id"],
            self.hub.ask("a", "three", choice="z")["id"],
        ]
        self.assertEqual(ids, ["a.1", "hub.1", "a.2"])

    def test_dismiss_unblocks(self):
        self.hub.add_task("a", "A")
        self.hub.ask("hub", "Duplicate question", critical=True, affects=["a"])
        self.hub.dismiss("hub.1", "same as hub.2")
        self.assertEqual([t["id"] for t in self.hub.ready()], ["a"])

    def test_reopen_retries_then_asks_the_user(self):
        self.hub.add_task("a", "A")
        self.commit_all()
        for attempt in (1, 2):
            self.build("a")
            self.assertEqual(self.hub.reopen("a", "test_x fails")["task"]["status"], "todo")
        self.build("a")
        result = self.hub.reopen("a", "test_x fails")
        self.assertEqual(result["task"]["status"], "blocked")
        self.assertEqual(result["question"]["status"], "open")
        self.assertEqual(result["task"]["blocked_by"], [result["question"]["id"]])
        self.assertEqual(self.hub.task("a")["notes"].count("reopened: test_x fails"), 3)


class OutputTest(HubTest):
    def test_render_puts_big_guesses_first(self):
        self.hub.add_task("a", "A")
        self.hub.ask("hub", "Which language?", critical=True, choice="Python", why="no install step")
        self.hub.ask("hub", "Which map tiles?", critical=True, options=["OpenStreetMap", "Mapbox"], affects=["a"])
        self.hub.ask("a", "Which test runner?", choice="pytest")
        self.hub.render()
        text = (self.project / "QUESTIONS.md").read_text()
        order = [text.index(s) for s in ("Big guesses", "Which language?", "Needs your answer",
                                         "Which map tiles?", "Smaller decisions", "Which test runner?")]
        self.assertEqual(order, sorted(order))
        self.assertIn("- **Blocks:** a", text)

    def test_report_lists_tasks(self):
        self.hub.add_task("a", "A")
        path = Path(self.hub.report("Built the skeleton.")["path"])
        self.assertEqual(path.name, "0.md")
        self.assertIn("| a | build | todo | 0 |", path.read_text())

    def test_next_round(self):
        self.assertEqual(self.hub.next_round(), {"round": 1})
        self.assertEqual(self.hub.ask("hub", "q", choice="c")["round"], 1)


class CliTest(HubTest):
    def cli(self, *args, stdin=None):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "hub.py"), "--project", str(self.project), *args],
            capture_output=True, text=True, input=stdin,
        )

    def test_prints_json_and_errors(self):
        added = self.cli("add-task", "api", "--title", "Build the API", "--depends", "")
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(json.loads(added.stdout)["id"], "api")
        missing = self.cli("task", "ghost")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no task ghost", missing.stderr)

    def test_init_reads_the_idea_from_stdin(self):
        other = Path(self._tmp.name) / "other"
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "hub.py"), "--project", str(other), "init", "--idea-file", "-"],
            capture_output=True, text=True, input="google maps but with flock cameras\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((other / ".hub" / "idea.md").read_text(), "google maps but with flock cameras\n")

    def test_parallel_asks_get_unique_ids(self):
        self.hub.add_task("a", "A")
        procs = [
            subprocess.Popen(
                [sys.executable, str(SCRIPTS / "hub.py"), "--project", str(self.project),
                 "ask", "--task", "a", "--text", f"decision {i}", "--choice", "x"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for i in range(8)
        ]
        ids = []
        for proc in procs:
            out, err = proc.communicate()
            self.assertEqual(proc.returncode, 0, err)
            ids.append(json.loads(out)["id"])
        self.assertEqual(sorted(ids, key=lambda i: int(i.split(".")[1])), [f"a.{n}" for n in range(1, 9)])


if __name__ == "__main__":
    unittest.main()
