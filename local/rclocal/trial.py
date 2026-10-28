"""Trying models on one role's job, with dummy tasks whose right answers are known.

    roughcut-local trial --role designer --models qwen2.5-coder:7b,qwen2.5-coder:14b --ui

Each model gets the same small task for the role, in a throwaway project under
~/roughcut-projects/.trials/<time>/, run by the same code a real round uses (so the
same box, schemas and limits). Then it's scored against what's known to be right:

- brainstormer: a vague idea. At most 3 questions, each with options and a pick, and a brief with must-haves.
- planner: a tip calculator. 1 to 5 files, valid contracts, at most 3 guesses.
- tester: a contract for a tip module. Its tests must pass on correct code and fail on code with a planted bug.
- builder: the same contract with known-good tests. Its file must pass them.
- designer: the contract and working code. Its page must pass the page check.
- user: a page with a planted bug (each share is the whole total). Some test user must notice.

You watch it in the terminal, or on a status page with --ui. Results, screenshots and
each trial's project are kept in the trial folder, with results.md comparing the models
on score, time and speed.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import threading
import time
from pathlib import Path

from . import project
from .brainstorm import Brainstorm
from .hub_local import LocalHub
from .llm import LLMError, OllamaClient
from .rounds import DESIGN_TASK, Round
from .users import TestUsers

IDEA = "A tip calculator: enter the bill, pick a tip percent and the number of people; see the tip, the total and each person's share."
VAGUE = "something for my book club"
TIP = {
    "path": "app/tip.js",
    "purpose": "Work out the tip, the total and each person's share of a bill.",
    "exports": [
        {"name": "tipAmount", "signature": "function tipAmount(bill, percent) -> number",
         "description": "the tip in dollars: tipAmount(120, 15) is 18"},
        {"name": "totalWithTip", "signature": "function totalWithTip(bill, percent) -> number",
         "description": "bill plus tip: totalWithTip(120, 15) is 138"},
        {"name": "perPerson", "signature": "function perPerson(bill, percent, people) -> number",
         "description": "each person's share of the total, rounded to cents: perPerson(120, 15, 4) is 34.5"},
    ],
    "imports": [],
}
GOOD = """export function tipAmount(bill, percent) {
  return Math.round(bill * percent) / 100;
}

export function totalWithTip(bill, percent) {
  return Math.round((bill + tipAmount(bill, percent)) * 100) / 100;
}

export function perPerson(bill, percent, people) {
  return Math.round((totalWithTip(bill, percent) / people) * 100) / 100;
}
"""
BUGGY = GOOD.replace("totalWithTip(bill, percent) / people", "totalWithTip(bill, percent) / 1")  # planted bug
TESTS = """import { test } from 'node:test';
import assert from 'node:assert/strict';
import { tipAmount, totalWithTip, perPerson } from '../app/tip.js';

test('tipAmount is the percent of the bill', () => assert.equal(tipAmount(120, 15), 18));
test('totalWithTip adds the tip', () => assert.equal(totalWithTip(120, 15), 138));
test('perPerson splits the total', () => assert.equal(perPerson(120, 15, 4), 34.5));
test('perPerson rounds to cents', () => assert.equal(perPerson(100, 10, 3), 36.67));
"""
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tip</title><link rel="stylesheet" href="kit.css"><link rel="stylesheet" href="styles.css"></head>
<body><main class="app">
<header class="app-header"><p class="eyebrow">Tool</p><h1>Tip calculator</h1><p class="lede">Split a bill.</p></header>
<section class="card stack"><div class="row">
<div class="field"><label for="bill">Bill</label><input id="bill" type="number" data-sample="120"></div>
<div class="field"><label for="percent">Tip %</label><input id="percent" type="number" data-sample="15"></div>
<div class="field"><label for="people">People</label><input id="people" type="number" data-sample="4"></div>
</div><p class="error" id="error"></p></section>
<section class="card"><h2 class="card-title">Result</h2><div class="stats">
<div class="stat"><div class="stat-label">Tip</div><div class="stat-value" id="tip">—</div></div>
<div class="stat"><div class="stat-label">Total</div><div class="stat-value" id="total">—</div></div>
<div class="stat highlight"><div class="stat-label">Each person</div><div class="stat-value" id="each">—</div></div>
</div></section></main>
<script type="module">
import { tipAmount, totalWithTip, perPerson } from './app/tip.js';
const $ = id => document.getElementById(id);
function update() {
  const bill = parseFloat($('bill').value), percent = parseFloat($('percent').value), people = parseInt($('people').value);
  const ok = bill > 0 && percent >= 0 && people > 0;
  $('error').textContent = ok || !$('bill').value ? '' : 'Enter a bill above 0 and at least 1 person.';
  $('tip').textContent = ok ? '$' + tipAmount(bill, percent).toFixed(2) : '—';
  $('total').textContent = ok ? '$' + totalWithTip(bill, percent).toFixed(2) : '—';
  $('each').textContent = ok ? '$' + perPerson(bill, percent, people).toFixed(2) : '—';
}
document.querySelectorAll('input').forEach(i => i.addEventListener('input', update));
document.body.dataset.ready = "1";
</script></body></html>
"""
ROLES = ("brainstormer", "planner", "tester", "builder", "designer", "user")


class Trial:
    def __init__(self, folder, role, model, echo=None):
        self.folder = Path(folder)
        self.role, self.model = role, model
        self.hub = LocalHub(self.folder)
        models = {role: model}
        if role == "user":
            models["designer"] = model  # not used: the page is given
        self.hub.init(VAGUE if role == "brainstormer" else IDEA, {"stack": "web", "models": models, "test_users": 3})
        cfg = self.hub.local_config()
        self.client = OllamaClient(cfg["host"], cfg["num_ctx"], cfg["temperature"], cfg["keep_alive"], cfg["call_timeout"])
        self.echo = echo

    def round(self):
        rnd = Round(self.hub, self.client, echo=self.echo)
        rnd.number = 0
        rnd.events.round = 0
        return rnd

    def write(self, path, text):
        self.hub._write_text(self.folder / path, text)

    def add_tip(self, tests=True):
        rnd = self.round()
        contract, _ = self.hub.contracts.upsert(TIP, 0, "trial")
        rnd.ensure_tasks(contract, "new")
        if tests:
            self.write(contract["test_path"], TESTS)
            self.hub.mark_done(contract["id"] + "-tests", "given")
        return rnd, contract

    async def run(self):
        return await getattr(self, f"try_{self.role}")()

    async def try_brainstormer(self):
        session = Brainstorm(self.hub)
        session.start(VAGUE)
        reply = await session.turn(self.round().sched, VAGUE, 3)
        qs = reply["questions"]
        checks = {
            "at most 3 questions": len(qs) <= 3,
            "asks something": bool(qs),
            "every question has 2+ options": all(len(q["options"]) >= 2 for q in qs),
            "every pick is one of its options": all(q["default"] in q["options"] for q in qs),
            "brief has must-haves": bool(reply["brief"]["must_have"]),
        }
        return checks, f"{reply['brief']['name']}: " + " | ".join(q["text"] for q in qs)

    async def try_planner(self):
        rnd = self.round()
        await rnd.plan(IDEA)
        contracts = self.hub.contracts.all()
        guesses = [q for q in self.hub.questions() if q["critical"]]
        checks = {
            "plans 1 to 5 files": 1 <= len(contracts) <= 5,
            "every file exports something": all(c["exports"] for c in contracts) and bool(contracts),
            "signatures look like JavaScript": all(
                e["signature"].split()[0] in ("function", "const", "class", "async") for c in contracts for e in c["exports"]
            ),
            "imports all exist": not self.hub.contracts.check(),
            "at most 3 guesses": len(guesses) <= 3,
        }
        return checks, "; ".join(f"{c['path']}: {', '.join(e['name'] for e in c['exports'])}" for c in contracts)

    async def try_tester(self):
        rnd, contract = self.add_tip(tests=False)
        task = self.hub.start_tasks()[0]
        status, summary = await rnd.write_tests(task, contract)
        checks = {"writes a usable test file": status == "done"}
        if status == "done":
            self.write(contract["path"], GOOD)
            checks["passes on correct code"] = (await rnd.runner.run(contract["test_path"]))[0]
            self.write(contract["path"], BUGGY)
            checks["catches the planted bug"] = not (await rnd.runner.run(contract["test_path"]))[0]
        return checks, summary

    async def try_builder(self):
        rnd, contract = self.add_tip()
        task = self.hub.start_tasks()[0]
        status, summary, _ = await rnd.build(task, contract)
        steps = sum(1 for e in self.hub.events.read() if e["type"] == "agent_step")
        return {"its file passes the tests": status == "done", "uses at most 4 steps": steps <= 4}, summary

    async def try_designer(self):
        rnd, contract = self.add_tip()
        self.write(contract["path"], GOOD)
        self.hub.mark_done(contract["id"], "given")
        rnd.ensure_design_task(False)
        task = self.hub.start_tasks()[0]
        status, summary = await rnd.design(task, IDEA)
        fixes = sum(1 for e in self.hub.events.read() if e["type"] == "page_fix")
        return {"the page passes its check": status == "done", "needs at most 1 fix": fixes <= 1}, summary

    async def try_user(self):
        rnd, contract = self.add_tip()
        self.write(contract["path"], BUGGY)
        self.write("index.html", PAGE)
        self.write("styles.css", "")
        ok, problem = await rnd.runner.check_page()
        if not ok:
            return {"the trial page loads": False}, problem
        rnd.page_checked = True
        users = TestUsers(rnd)
        reports = []
        original = rnd.runner.try_as_user

        async def keep(steps, **kw):
            reports.append(await original(steps, **kw))
            return reports[-1]

        rnd.runner.try_as_user = keep
        results = await users.run(IDEA, rnd.runner.last_check["inventory"])
        caught = any(share_caught(r) for r in reports if r)
        about_share = re.compile(r"share|each|per.?person|split|divid", re.I)
        says_wrong = re.compile(r"wrong|incorrect|should|expected|instead|but ", re.I)
        found = [f"{r['persona']}: {p['what']}" for r in results for p in r["problems"]
                 if not p.get("seen") and about_share.search(p["what"]) and says_wrong.search(p["what"])]
        noise = [p["what"] for r in results for p in r["problems"] if len(p["what"].split()) < 3]
        false_alarm = any(r["works"] for r in results if r["key"] == "riley")
        if caught:
            found.insert(0, "a test user expected the right share and it wasn't on the page")
        return {
            "every test user finished": len(results) == 3,
            "someone caught the wrong share": bool(found),
            "the careful one didn't call it fine": not false_alarm,
            "problems are sentences, not keywords": len(noise) <= 1,
        }, "; ".join(found[:3]) or "nobody named it: " + "; ".join(p["what"] for r in results for p in r["problems"])[:300]


def share_caught(report):
    """Did a user expect the right share (worked out from what they typed) and find it missing?"""
    values = {"bill": None, "percent": None, "people": None}
    for e in report.get("log", []):
        target = e["target"].lower().lstrip("#")
        key = "bill" if "bill" in target else "percent" if ("tip" in target or "percent" in target) else (
            "people" if "people" in target else None)
        if key and e.get("value"):
            try:
                values[key] = float(e["value"])
            except ValueError:
                values[key] = None
        if None in values.values() or not values["people"] or values["people"] <= 0 or not e.get("missing"):
            continue
        right = round(values["bill"] * (1 + values["percent"] / 100) / values["people"], 2)
        if any(abs(float(n) - right) < 0.011 for n in e["missing"]):
            return True
    return False


def installed_models(client):
    names = client.installed()
    return [n for n in names if "embed" not in n]


class CurrentHub:
    """What the status page reads: whichever trial is running now."""

    def __init__(self):
        self.hub = None

    def __getattr__(self, name):
        return getattr(self.hub, name)


def main(args):
    from .cli import echo

    client = OllamaClient()
    try:
        models = [m.strip() for m in args.models.split(",")] if args.models else installed_models(client)
    except LLMError as e:
        return {"error": str(e)}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = project.projects_dir() / ".trials" / f"{stamp}-{args.role}"
    current = CurrentHub()
    if args.ui:
        from .status import serve

        server = serve(current, client, args.ui)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"Status page: http://127.0.0.1:{args.ui}", flush=True)
    rows = []
    for model in models:
        for n in range(1, args.repeat + 1):
            name = f"{model.replace(':', '-').replace('/', '-')}-{n}"
            print(f"\n=== {args.role} on {model} (run {n})", flush=True)
            trial = Trial(folder / name, args.role, model, echo=echo)
            current.hub = trial.hub
            trial.hub.events.round = 0
            trial.hub.events.emit("round_start", idea=f"trial: {args.role} on {model}")
            start = time.monotonic()
            try:
                checks, detail = asyncio.run(trial.run())
            except LLMError as e:
                checks, detail = {"no model error": False}, str(e)
            seconds = time.monotonic() - start
            trial.hub.events.emit("round_end", summary={"trial": args.role, "model": model})
            calls = [e for e in trial.hub.events.read() if e["type"] == "llm_end"]
            tokens = sum(e["output_tokens"] for e in calls)
            model_seconds = sum(e["seconds"] for e in calls)
            for shot in (trial.hub.root / "screens").glob("*.png") if (trial.hub.root / "screens").exists() else []:
                shutil.copy(shot, folder / f"{name}-{shot.name}")
            row = {
                "model": model, "run": n, "score": sum(checks.values()), "out_of": len(checks), "checks": checks,
                "seconds": round(seconds, 1), "calls": len(calls), "output_tokens": tokens,
                "tokens_per_second": round(tokens / model_seconds, 1) if model_seconds else 0, "detail": detail[:500],
            }
            rows.append(row)
            print(f"--> {row['score']}/{row['out_of']} in {row['seconds']}s, {row['calls']} calls, "
                  f"{row['tokens_per_second']} tok/s: " + ", ".join(f"{'✓' if v else '✗'} {k}" for k, v in checks.items()),
                  flush=True)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "results.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    (folder / "results.md").write_text(render(args.role, rows), encoding="utf-8")
    return {"folder": str(folder), "results": [{k: r[k] for k in ("model", "run", "score", "out_of", "seconds")} for r in rows]}


def render(role, rows):
    out = [f"# Trial: {role}", "", "| Model | Run | Score | Time | Calls | tok/s | Notes |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        missed = ", ".join(k for k, v in r["checks"].items() if not v) or "all checks pass"
        out.append(f"| {r['model']} | {r['run']} | {r['score']}/{r['out_of']} | {r['seconds']}s | {r['calls']} | "
                   f"{r['tokens_per_second']} | {missed} |")
    out += ["", "## What each run produced", ""]
    out += [f"- **{r['model']}** run {r['run']}: {r['detail']}" for r in rows]
    return "\n".join(out) + "\n"
