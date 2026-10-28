"""roughcut-local: build a software idea a round at a time with local Ollama models only.

    roughcut-local new "a bill splitter for group dinners"   # talk it through, build, open the app
    roughcut-local open ~/roughcut-projects/bill-splitter    # or a project name, or a GitHub URL
    roughcut-local projects                                  # every project made or opened here
    roughcut-local trial --role designer                     # try models on one role's dummy task

  One project at a time (run inside it, or pass --project):
    roughcut-local talk                 # brainstorm changes before the next round
    roughcut-local round --rounds 3 --ui
    roughcut-local questions --ask      # go through what the models guessed, Enter to keep
    roughcut-local app                  # open the app in your browser
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import project
from .brainstorm import LETTERS, Brainstorm
from .hub_local import HUB_OWNER, HubError, LocalHub
from .llm import LLMError, OllamaClient
from .protocol import ROLES
from .rounds import Round, commit
from .runner import Runner, free_port
from .scheduler import Scheduler
from .stacks import STACKS


def client_for(hub):
    cfg = hub.local_config()
    return OllamaClient(cfg["host"], cfg["num_ctx"], cfg["temperature"], cfg["keep_alive"], cfg["call_timeout"])


def echo(event):
    """One terminal line per event worth seeing."""
    t = event["type"]
    if t == "phase":
        line = f"== {event['name']}"
    elif t == "task_start":
        line = f"   start  {event['task']} (try {event['attempt']})"
    elif t == "task_end":
        line = f"   {event['status']:<6} {event['task']}: {event['summary']}"
    elif t == "llm_end":
        line = f"          {event['role']} on {event['model']}: {event['seconds']}s, {event['tokens_per_second']} tok/s"
    elif t == "llm_error":
        line = f"   ERROR  {event['label']}: {event['error']}"
    elif t == "tests" and event["target"] == "all":
        line = f"   tests  full run {'passed' if event['passed'] else 'failed'}"
    elif t == "agent_step":
        line = f"          {event['task']} step {event['step']}: {event['action']} {event['path']}".rstrip()
    elif t == "blocked_code":
        line = f"   GUARD  {event['path']}: {event['problems']}"
    elif t == "page_fix":
        line = f"   page   fixing only the {event['part']}: {event['reason'][:160]}"
    elif t == "user_test":
        line = f"   user   {event['persona']}: {'works' if event['works'] else '; '.join(event['problems'])[:200]}"
    elif t in ("note", "contract") or (t == "question" and event["blocking"]):
        line = "   " + (event.get("text") or f"contract {event['path']} v{event['version']}: {event['reason']}")
    else:
        return
    print(line, flush=True)


def ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        return ""


def models_from(pairs):
    models = {}
    for pair in pairs:
        role, _, model = pair.partition("=")
        if role not in ROLES or not model:
            raise HubError(f"--model takes role=model with a role from {', '.join(ROLES)}")
        models[role] = model
    return models


def read_idea(args):
    if getattr(args, "idea_file", None) == "-":
        return sys.stdin.read()
    if getattr(args, "idea_file", None):
        with open(args.idea_file, encoding="utf-8") as f:
            return f.read()
    return args.idea or ""


# Talking it through


def talk(hub, client, thoughts):
    """The brainstorm, in the terminal. Ends with the brief saved and idea.md updated from it."""
    cfg = hub.local_config()
    idea = (hub.root / "idea.md").read_text(encoding="utf-8")
    session = Brainstorm(hub)
    if hub.contracts.all():  # changes to a project that exists: the planner should see them as asked
        q = hub.ask(HUB_OWNER, "What should change next?", critical=True, choice="(in your words)")
        hub.answer(q["id"], thoughts)
    session.start(thoughts)
    turns = cfg["brainstorm_turns"]
    print("\nLet's talk it through first. Enter takes my pick, a letter picks an option, or type your own answer.")
    for turn in range(turns):
        print("\n… thinking", flush=True)
        reply = asyncio.run(session.turn(Scheduler(client, cfg, hub.events), idea, turns - turn))
        brief = reply["brief"]
        print(f"\n{brief['name']}: {brief['pitch']}")
        if brief["must_have"]:
            print("  Must have: " + "; ".join(brief["must_have"]))
        if brief["out_of_scope"]:
            print("  Leaving out: " + "; ".join(brief["out_of_scope"]))
        for point in reply["pushback"]:
            print(f"  • {point}")
        for i, q in enumerate(reply["questions"], 1):
            print(f"\n{i}. {q['text']}" + (f"  ({q['why']})" if q["why"].strip() else ""))
            for letter, option in zip(LETTERS, q["options"]):
                print(f"   {letter}) {option}{'   ← my pick' if option == q['default'] else ''}")
            print(f"   → {session.answer(q, ask('   > '))}")
        settled = reply["ready"] or not reply["questions"]  # nothing left to ask is as good as ready
        hint = "Enter starts building" if settled else "Enter keeps talking"
        extra = ask(f"\nAnything to add? ({hint}; 'build' starts now) > ").strip()
        if extra.lower() in ("build", "go", "done", "start"):
            break
        if extra:
            session.add(extra)
        elif settled:
            break
    project.set_idea_from_brief(hub, idea)
    project.render(hub)
    print(f"\nSaved the brief to {hub.project / 'PROJECT.md'}\n", flush=True)


def walk_questions(hub):
    """Go through open questions and unconfirmed guesses one at a time."""
    todo = [q for q in hub.questions() if q["status"] == "open" or (q["status"] == "decided" and q["critical"])]
    if not todo:
        print("Nothing needs you right now.")
        return {"answered": 0}
    print(f"{len(todo)} to go. Enter keeps the pick, a letter picks an option, your own words change it, s skips.\n")
    answered = 0
    for q in todo:
        print(f"{q['id']}: {q['text']}" + (f"  ({q['why']})" if q["why"] else ""))
        options = list(dict.fromkeys(([q["choice"]] if q["choice"] else []) + q["options"]))[:4]
        for letter, option in zip(LETTERS, options):
            print(f"   {letter}) {option}{'   ← picked' if option == q['choice'] else ''}")
        reply = ask("   > ").strip()
        if reply.lower() == "s":
            continue
        if not reply and q["choice"]:
            hub.answer(q["id"], keep=True)
        else:
            if len(reply) == 1 and reply.lower() in LETTERS[: len(options)]:
                reply = options[LETTERS.index(reply.lower())]
            if not reply:
                continue
            result = hub.answer(q["id"], reply)
            if result["reworked"]:
                print(f"   will redo: {', '.join(result['reworked'])}")
        answered += 1
        print()
    hub.render()
    project.render(hub)
    return {"answered": answered}


# Rounds and the app


def run_rounds(hub, client, rounds, ui=None, quiet=False):
    if ui:
        from .status import serve

        server = serve(hub, client, ui)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"Status page: http://127.0.0.1:{ui}", flush=True)
    summaries = []
    for _ in range(rounds):
        before = sum(t["status"] == "done" for t in hub.tasks())
        summary = asyncio.run(Round(hub, client, echo=None if quiet else echo).run())
        summaries.append(summary)
        print(json.dumps(summary, indent=2), flush=True)
        after = sum(t["status"] == "done" for t in hub.tasks())
        todo = any(t["status"] == "todo" for t in hub.tasks())
        if summary["open_questions"]:
            print("Stopping: some questions need your answer (roughcut-local questions --ask).", flush=True)
            break
        if summary["complete"] and not todo and summary["tests_passed"]:
            print("Stopping: the planner says the idea is covered and every test passes.", flush=True)
            break
        if after == before and not todo:
            print("Stopping: the last round made no progress.", flush=True)
            break
    return {"rounds": len(summaries), "last": summaries[-1] if summaries else None}


def serve_app(hub, port=None, open_browser=True):
    if hub.stack.name != "web" or not (hub.project / "index.html").exists():
        return {"app": None, "why": "no page yet" if hub.stack.name == "web" else "the python stack has no page"}
    runner = Runner(hub.project, hub.stack, sandbox=hub.local_config()["sandbox"])
    port = port or free_port()
    server = runner.serve(port)
    url = f"http://127.0.0.1:{port}/"
    time.sleep(0.5)
    print(f"\nThe app is at {url}  (Ctrl-C to stop it)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop(server)
    return {"app": url, "stopped": True}


def finish(hub, client, args):
    """After talking: build, then show the app."""
    result = run_rounds(hub, client, args.rounds, args.ui, args.quiet)
    project.render(hub)
    if not args.no_open:
        serve_app(hub)
    return {**result, "project": str(hub.project), "notes": str(hub.project / "PROJECT.md")}


# Commands that start from no project


def cmd_new(args):
    idea = read_idea(args) or ask("What do you want to build? ")
    if not idea.strip():
        raise HubError("the idea is empty")
    folder = Path(args.dir).expanduser() if args.dir else project.free_folder(project.slug(idea))
    hub = LocalHub(folder)
    config = {"stack": args.stack}
    if args.model:
        config["models"] = models_from(args.model)
    hub.init(idea, config)
    print(f"New project in {hub.project}", flush=True)
    client = client_for(hub)
    if not args.no_talk:
        talk(hub, client, idea)
    project.render(hub)
    return finish(hub, client, args)


def cmd_open(args):
    target = args.target
    if project.is_github(target):
        print(f"Cloning {target} …", flush=True)
        folder = project.clone(target)
    else:
        folder = project.find(target)
        if folder is None:
            raise HubError(f"no project or folder called {target!r}; see roughcut-local projects")
    if not (folder / ".hub" / "hub.json").exists():
        if not project.is_github(target) and not args.in_place:
            folder = project.copy_folder(folder)
            print(f"Working on a copy in {folder}, so your folder stays as it is", flush=True)
        hub = LocalHub(folder)
        hub.init(project.first_words(folder), {"stack": args.stack or project.guess_stack(folder)})
        client = client_for(hub)
        print("Reading the existing code …", flush=True)
        hub.events.echo = echo
        sched = Scheduler(client, hub.local_config(), hub.events)
        found = asyncio.run(project.survey(hub, sched, hub.events))
        print(f"Contracts for: {', '.join(found['surveyed']) or 'nothing (no files in app/)'}", flush=True)
        commit(hub.project, "Brought in")
    else:
        hub = LocalHub(folder)
        client = client_for(hub)
    project.render(hub)
    print(f"\n{(hub.project / 'PROJECT.md').read_text(encoding='utf-8')[:1500]}", flush=True)
    thoughts = args.thoughts or ask("\nWhat would you like to change or add? > ")
    if thoughts.strip():
        talk(hub, client, thoughts)
        return finish(hub, client, args)
    return {"project": str(hub.project), "rounds": 0}


def cmd_projects(args):
    entries = project.registry()
    rows = sorted(entries.items(), key=lambda kv: kv[1]["updated"], reverse=True)
    for folder, e in rows:
        mark = "" if Path(folder).exists() else "  (folder gone)"
        print(f"{e['name']:<28} round {e['round']:<3} {e['stack']:<6} {e['updated']}  {folder}{mark}")
    if not rows:
        print("No projects yet: roughcut-local new \"your idea\"")
    return {"projects": len(rows)}


def cmd_trial(args):
    from . import trial

    return trial.main(args)


# Commands on one project


def cmd_init(hub, args):
    config = {"stack": args.stack}
    if args.model:
        config["models"] = models_from(args.model)
    result = hub.init(read_idea(args), config)
    project.render(hub)
    return result


def cmd_check(hub, args):
    cfg = hub.local_config()
    client = client_for(hub)
    try:
        installed = client.installed()
    except LLMError as e:
        return {"ollama": str(e)}
    wanted = sorted(set(cfg["models"].values()))
    missing = [m for m in wanted if m not in installed and f"{m}:latest" not in installed]
    runner = Runner(hub.project, hub.stack, sandbox=cfg["sandbox"])
    return {
        "ollama": "running",
        "stack": hub.stack.name,
        "models": wanted,
        "missing": missing,
        "pull": [f"ollama pull {m}" for m in missing],
        "sandbox": runner.sandbox,
        "node": bool(shutil.which("node")) if hub.stack.name == "web" else None,
        "chrome_for_page_check": bool(runner.chrome()) if hub.stack.name == "web" else None,
    }


def cmd_talk(hub, args):
    thoughts = args.thoughts or ask("What would you like to change or add? > ")
    if not thoughts.strip():
        return {"talked": False}
    talk(hub, client_for(hub), thoughts)
    return {"talked": True, "next": "roughcut-local round"}


def cmd_ui(hub, args):
    from .status import serve

    server = serve(hub, client_for(hub), args.port)
    print(f"Status page: http://127.0.0.1:{args.port}  (Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return {"stopped": True}


def cmd_questions(hub, args):
    if args.ask:
        return walk_questions(hub)
    hub.render()
    questions = hub.questions()
    return {
        "needs_your_answer": [q for q in questions if q["status"] == "open"],
        "big_guesses": [{"id": q["id"], "text": q["text"], "choice": q["choice"]} for q in questions
                        if q["status"] == "decided" and q["critical"]],
    }


def build_parser():
    parser = argparse.ArgumentParser(
        prog="roughcut-local", description=__doc__.split("\n\n")[1], formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--project", default=".", help="project folder (default: the current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    def building(p, rounds):
        p.add_argument("--rounds", type=int, default=rounds, help=f"build up to this many rounds (default {rounds})")
        p.add_argument("--ui", type=int, nargs="?", const=8765, help="also serve the status page (default port 8765)")
        p.add_argument("--no-open", action="store_true", help="don't open the app when it's built")
        p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("new", help="start from an idea: talk it through, build it, open it")
    p.add_argument("idea", nargs="?")
    p.add_argument("--idea-file", help="a file holding the idea, or - for stdin")
    p.add_argument("--dir", help="where to make it (default: ~/roughcut-projects/<name>)")
    p.add_argument("--stack", choices=sorted(STACKS), default="web")
    p.add_argument("--model", action="append", default=[], help="role=model, e.g. designer=qwen2.5-coder:14b")
    p.add_argument("--no-talk", action="store_true", help="skip the brainstorm and build from the idea as written")
    building(p, 4)

    p = sub.add_parser("open", help="open a project (folder, name or GitHub URL), talk about changes, build")
    p.add_argument("target")
    p.add_argument("--thoughts", help="what you want changed, instead of being asked")
    p.add_argument("--stack", choices=sorted(STACKS), help="for a project brought in (default: guessed)")
    p.add_argument("--in-place", action="store_true", help="work in a folder brought in, instead of on a copy")
    building(p, 2)

    sub.add_parser("projects", help="list every project made or opened here")

    p = sub.add_parser("trial", help="try models on dummy tasks for one role, and compare them")
    p.add_argument("--role", choices=["brainstormer", "planner", "tester", "builder", "designer", "user"], required=True)
    p.add_argument("--models", help="comma-separated (default: every installed model)")
    p.add_argument("--repeat", type=int, default=1, help="runs per model (default 1)")
    p.add_argument("--ui", type=int, nargs="?", const=8766, help="watch on a status page (default port 8766)")

    p = sub.add_parser("init", help="start a project from an idea, without talking or building")
    p.add_argument("--idea")
    p.add_argument("--idea-file", help="a file holding the idea, or - for stdin")
    p.add_argument("--stack", choices=sorted(STACKS), default="web", help="web (default): a designed browser app; python")
    p.add_argument("--model", action="append", default=[], help="role=model, e.g. builder=qwen2.5-coder:7b")

    sub.add_parser("check", help="check that Ollama runs and has every model local.json names")

    p = sub.add_parser("talk", help="brainstorm changes with the brainstormer before the next round")
    p.add_argument("--thoughts")

    p = sub.add_parser("round", help="run rounds: plan, build, verify, try, questions")
    p.add_argument("--rounds", type=int, default=1, help="run up to this many rounds in a row (default 1)")
    p.add_argument("--ui", type=int, nargs="?", const=8765, help="also serve the status page (default port 8765)")
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("answer", help="answer a question, or change or confirm a guess")
    p.add_argument("id")
    p.add_argument("--text")
    p.add_argument("--keep", action="store_true")

    p = sub.add_parser("redo", help="send a task back for the next round, e.g. redo page-design --reason 'more room'")
    p.add_argument("id")
    p.add_argument("--reason", default="asked for by you")

    p = sub.add_parser("questions", help="write QUESTIONS.md and list what needs you")
    p.add_argument("--ask", action="store_true", help="go through them one at a time")
    sub.add_parser("status", help="summarize tasks and questions")
    p = sub.add_parser("app", help="open the app in your browser")
    p.add_argument("--port", type=int)
    p = sub.add_parser("ui", help="serve the status page")
    p.add_argument("--port", type=int, default=8765)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    free = {"new": cmd_new, "open": cmd_open, "projects": cmd_projects, "trial": cmd_trial}
    on_project = {
        "init": cmd_init,
        "check": cmd_check,
        "talk": cmd_talk,
        "round": lambda h, a: run_rounds(h, client_for(h), a.rounds, a.ui, a.quiet),
        "answer": lambda h, a: h.answer(a.id, a.text, a.keep),
        "redo": lambda h, a: h.retry(a.id, a.reason),
        "questions": cmd_questions,
        "status": lambda h, a: h.status(),
        "app": lambda h, a: serve_app(h, a.port),
        "ui": cmd_ui,
    }
    try:
        if args.command in free:
            result = free[args.command](args)
        else:
            result = on_project[args.command](LocalHub(args.project), args)
    except (HubError, LLMError, RuntimeError) as e:
        print(f"roughcut-local: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Everything so far is saved; run the same command again to go on.", file=sys.stderr)
        return 130
    print(json.dumps(result, indent=2))
    return 0
