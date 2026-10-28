"""Test users: three people who try the finished page, knowing nothing about how it was built.

The tests check each file against its contract, and the page check checks that the page
reacts at all. Neither knows whether the app does what the idea asked for, because the
planner wrote the contracts both are checked against. The test users are independent of
all that: each sees only the brief and the controls on screen, plans what they'd do
(with the results they'd expect, worked out by hand), and the steps are then carried
out for real in headless Chrome (runner.try_as_user). Afterwards each says what went
wrong. Facts the browser can see for itself (a thrown error, NaN on screen, a control
that isn't there) are added as problems without asking a model.

What they find goes into .hub/rounds/<n>.users.md. Problems with the page send the page
back to the designer (at most `user_fixes` times per project); a result a user thinks is
wrong becomes a question for you, since a test user can be wrong too.
"""

from __future__ import annotations

import asyncio
import json
import re

from . import agents
from .hub_local import HUB_OWNER
from .llm import LLMError

PERSONAS = [
    {
        "key": "sam", "name": "Sam", "who": "a busy person on their phone who wants the answer fast.",
        "how": "Do the main thing once with ordinary values and read the result.", "viewport": (390, 844),
    },
    {
        "key": "riley", "name": "Riley", "who": "careful and good with numbers.",
        "how": "Do the main task with values you can check by hand, then change one value and check the result follows.",
        "viewport": (1280, 900),
    },
    {
        "key": "alex", "name": "Alex", "who": "curious and a little careless.",
        "how": "Try odd input: leave a field empty, type 0, a negative number, a huge number or letters, and see "
               "whether the app copes with a clear message instead of NaN, blanks or nothing at all.",
        "viewport": (1280, 900),
    },
]
BROKEN = re.compile(r"\bNaN\b|undefined|Infinity|\[object")
MAX_STEPS = 8
MISSING = "there is no control called"


def describe_controls(inventory):
    lines = []
    for c in inventory.get("controls", []):
        name = f"#{c['id']}" if c.get("id") else f'"{c["label"]}"'
        extra = f", options: {', '.join(c['options'])}" if c.get("options") else ""
        extra += f", now: {c['value']}" if c.get("value") and c["kind"] not in ("button",) else ""
        lines.append(f"- {c['kind']} {name} labelled \"{c['label']}\"{extra}")
    shows = inventory.get("results") or []
    return "\n".join(lines) + ("\nThe results area shows: " + " | ".join(shows) if shows else "")


def describe_run(report):
    lines = []
    for i, e in enumerate(report.get("log", []), 1):
        what = f"{e['do']} {e['target']}" + (f" with {e['value']!r}" if e.get("value") else "")
        line = f"{i}. {what}: {e.get('result', '')}."
        if e.get("shows"):
            line += f" The page then showed: {' | '.join(e['shows'])}."
        if e.get("expect"):
            seen = "it was there" if e.get("saw_expected") else (
                f"{', '.join(e['missing'])} was NOT on the page" if e.get("missing") else "it was NOT there")
            line += f" You expected to see {e['expect']!r}: {seen}."
        lines.append(line)
    return "\n".join(lines) or "Nothing happened: there were no steps to carry out."


def browser_facts(report):
    """Problems the browser saw for itself. These don't depend on any model's judgement."""
    if report is None:
        return []
    facts = []
    if not report.get("ready"):
        facts.append("the page never finished loading")
    for error in (report.get("errors") or [])[:2]:
        facts.append(f"the page threw an error: {error}")
    for e in report.get("log", []):
        if e.get("result", "").startswith("there is no such control"):
            facts.append(f"{MISSING} {e['target']!r}")
        broken = [s for s in e.get("shows", []) if BROKEN.search(s)]
        if broken:
            facts.append(f"after {e['do']} {e['target']}, the page showed {broken[0]!r}")
    return list(dict.fromkeys(facts))


def missed_expectations(report):
    """Steps where a number the user worked out by hand wasn't on the page. Their math can be wrong too."""
    misses = []
    for e in (report or {}).get("log", []):
        if e.get("missing"):
            did = f"{e['do']} {e['target']}" + (f" with {e['value']!r}" if e.get("value") else "")
            misses.append(f"after {did}, expected {', '.join(e['missing'])}, but the page showed: {' | '.join(e.get('shows', []))}")
    return misses


class TestUsers:
    def __init__(self, round_):
        self.round = round_
        self.hub = round_.hub
        self.events = round_.events

    @property
    def history_path(self):
        return self.hub.root / "users.json"

    def history(self):
        return json.loads(self.history_path.read_text(encoding="utf-8")) if self.history_path.exists() else []

    async def run(self, brief, inventory):
        count = self.round.cfg["test_users"]
        if not count or not inventory or not inventory.get("controls"):
            return []
        controls = describe_controls(inventory)
        results = await asyncio.gather(*(self.one(p, brief, controls) for p in PERSONAS[:count]))
        results = [r for r in results if r]
        self.record(results)
        return results

    async def one(self, persona, brief, controls):
        sched, runner, n = self.round.sched, self.round.runner, self.round.number
        try:
            plan = await agents.user_plan(sched, persona, brief, controls)
        except LLMError as e:
            self.events.emit("note", text=f"test user {persona['name']} couldn't plan: {e}")
            return None
        steps = [s for s in plan["steps"] if s["target"].strip()][:MAX_STEPS]
        shot = self.hub.root / "screens" / f"round-{n}-{persona['key']}.png"
        report = await runner.try_as_user(steps, viewport=persona["viewport"], screenshot=shot)
        if report is None:
            return None  # no Chrome: nothing was tried, so nothing to judge
        facts = browser_facts(report)
        try:
            verdict = await agents.user_verdict(sched, persona, brief, describe_run(report), "\n".join(facts) or "nothing")
        except LLMError as e:
            self.events.emit("note", text=f"test user {persona['name']} gave no verdict: {e}")
            verdict = {"works": not facts, "problems": [], "wish": ""}
        # A control the user made up isn't a broken page; everything else the browser saw is.
        problems = [{"what": f, "severity": "low" if f.startswith(MISSING) else "high", "where": "page", "seen": True}
                    for f in facts]
        problems += [{"what": m, "severity": "low", "where": "logic", "seen": True} for m in missed_expectations(report)[:3]]
        problems += [p for p in verdict["problems"] if len(p["what"].split()) >= 4][:4]  # not bare labels
        works = verdict["works"] and not facts
        self.events.emit("user_test", persona=persona["name"], works=works, problems=[p["what"] for p in problems])
        return {
            "persona": persona["name"], "key": persona["key"], "goal": plan["goal"], "works": works,
            "problems": problems, "wish": verdict["wish"].strip(), "run": describe_run(report),
            "screenshot": str(shot) if shot.exists() else None,
        }

    def record(self, results):
        """Write the round's report, send page problems to the designer and ask you about the rest."""
        hub, n = self.hub, self.round.number
        history = self.history()
        history.append({"round": n, "results": [{k: r[k] for k in ("persona", "works", "problems", "wish")} for r in results]})
        hub._write_text(self.history_path, json.dumps(history, indent=2) + "\n")
        hub._write_text(hub.root / "rounds" / f"{n}.users.md", self.render(results))
        page = [f"{r['persona']}: {p['what']}" for r in results for p in r["problems"]
                if p["where"] == "page" and p["severity"] == "high"]
        fixes_used = sum(1 for h in history[:-1] if h.get("sent_back"))
        from .rounds import DESIGN_TASK

        has_page_task = any(t["id"] == DESIGN_TASK for t in hub.tasks())
        if page and has_page_task and fixes_used < self.round.cfg["user_fixes"]:
            history[-1]["sent_back"] = True
            hub._write_text(self.history_path, json.dumps(history, indent=2) + "\n")
            self.round.redo(DESIGN_TASK, "test users found: " + "; ".join(page[:4]))
            self.events.emit("note", text=f"test users sent the page back: {'; '.join(page[:4])}")
        asked = {q["text"] for q in hub.questions()}
        for r in results:
            for p in r["problems"]:
                text = f"Test user {r['persona']} says a result is wrong: {p['what']}"
                if p["where"] == "logic" and text not in asked:
                    q = hub.ask(
                        HUB_OWNER, text, critical=True,
                        choice="Leave it for now", why="a test user can be wrong too, so you decide",
                        options=[
                            {"label": "Fix it", "detail": "The planner looks at this result again next round and changes what it promises."},
                            {"label": "It's fine", "detail": "The test user was wrong; nothing changes."},
                            {"label": "Leave it for now", "detail": "Decide later; nothing changes until you do."},
                        ],
                    )
                    self.events.emit("question", id=q["id"], text=q["text"], blocking=False)

    def render(self, results):
        out = [f"# Test users, round {self.round.number}", ""]
        for r in results:
            out += [f"## {r['persona']}: {'it works' if r['works'] else 'problems'}", "", f"Wanted to: {r['goal']}", ""]
            out += ["What they did:", "", r["run"], ""]
            if r["problems"]:
                out += ["Problems:", ""] + [
                    f"- **{p['severity']}** ({p['where']}{', seen by the browser' if p.get('seen') else ''}): {p['what']}"
                    for p in r["problems"]
                ] + [""]
            if r["wish"]:
                out += [f"Wish: {r['wish']}", ""]
            if r["screenshot"]:
                out += [f"Screenshot: `{r['screenshot']}`", ""]
        return "\n".join(out)
