"""roughcut-local: build a software idea a round at a time with local Ollama models only.

    roughcut-local init --project ~/proj --idea "a CLI that converts units"
    roughcut-local round --project ~/proj --ui        # one round, with the status page
    roughcut-local round --project ~/proj --rounds 6  # overnight: up to 6 rounds in a row
    roughcut-local answer --project ~/proj hub.2 --text "use JSON files"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import threading

from .hub_local import HubError, LocalHub
from .llm import LLMError, OllamaClient
from .protocol import ROLES
from .rounds import Round


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
    elif t in ("note", "contract") or (t == "question" and event["blocking"]):
        line = "   " + (event.get("text") or f"contract {event['path']} v{event['version']}: {event['reason']}")
    else:
        return
    print(line, flush=True)


def cmd_init(hub, args):
    if args.idea_file == "-":
        idea = sys.stdin.read()
    elif args.idea_file:
        with open(args.idea_file, encoding="utf-8") as f:
            idea = f.read()
    else:
        idea = args.idea or ""
    models = {}
    for pair in args.model:
        role, _, model = pair.partition("=")
        if role not in ROLES or not model:
            raise HubError(f"--model takes role=model with a role from {', '.join(ROLES)}")
        models[role] = model
    return hub.init(idea, {"models": models} if models else None)


def cmd_check(hub, args):
    cfg = hub.local_config()
    client = client_for(hub)
    try:
        installed = client.installed()
    except LLMError as e:
        return {"ollama": str(e)}
    wanted = sorted(set(cfg["models"].values()))
    missing = [m for m in wanted if m not in installed and f"{m}:latest" not in installed]
    return {
        "ollama": "running",
        "models": wanted,
        "missing": missing,
        "pull": [f"ollama pull {m}" for m in missing],
        "sandbox": bool(cfg["sandbox"] and sys.platform == "darwin" and shutil.which("sandbox-exec")),
    }


def cmd_round(hub, args):
    client = client_for(hub)
    if args.ui:
        from .status import serve

        server = serve(hub, client, args.ui)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"Status page: http://127.0.0.1:{args.ui}", flush=True)
    summaries = []
    for _ in range(args.rounds):
        before = sum(t["status"] == "done" for t in hub.tasks())
        summary = asyncio.run(Round(hub, client, echo=None if args.quiet else echo).run())
        summaries.append(summary)
        print(json.dumps(summary, indent=2), flush=True)
        after = sum(t["status"] == "done" for t in hub.tasks())
        todo = any(t["status"] == "todo" for t in hub.tasks())
        if summary["open_questions"]:
            print("Stopping: some questions need your answer (see QUESTIONS.md).", flush=True)
            break
        if summary["complete"] and not todo and summary["tests_passed"]:
            print("Stopping: the planner says the idea is covered and every test passes.", flush=True)
            break
        if after == before and not todo:
            print("Stopping: the last round made no progress.", flush=True)
            break
    return {"rounds": len(summaries), "last": summaries[-1] if summaries else None}


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
    hub.render()
    questions = hub.questions()
    return {
        "needs_your_answer": [q for q in questions if q["status"] == "open"],
        "big_guesses": [{"id": q["id"], "text": q["text"], "choice": q["choice"]} for q in questions
                        if q["status"] == "decided" and q["critical"]],
    }


def build_parser():
    parser = argparse.ArgumentParser(prog="roughcut-local", description=__doc__.splitlines()[0])
    parser.add_argument("--project", default=".", help="project folder (default: the current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="start a project from an idea")
    p.add_argument("--idea")
    p.add_argument("--idea-file", help="a file holding the idea, or - for stdin")
    p.add_argument("--model", action="append", default=[], help="role=model, e.g. builder=qwen2.5-coder:7b")

    sub.add_parser("check", help="check that Ollama runs and has every model local.json names")

    p = sub.add_parser("round", help="run rounds: plan, build, verify, questions")
    p.add_argument("--rounds", type=int, default=1, help="run up to this many rounds in a row (default 1)")
    p.add_argument("--ui", type=int, nargs="?", const=8765, help="also serve the status page (default port 8765)")
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("answer", help="answer a question, or change or confirm a guess")
    p.add_argument("id")
    p.add_argument("--text")
    p.add_argument("--keep", action="store_true")

    sub.add_parser("questions", help="write QUESTIONS.md and list what needs you")
    sub.add_parser("status", help="summarize tasks and questions")
    p = sub.add_parser("ui", help="serve the status page")
    p.add_argument("--port", type=int, default=8765)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    hub = LocalHub(args.project)
    commands = {
        "init": cmd_init,
        "check": cmd_check,
        "round": cmd_round,
        "answer": lambda h, a: h.answer(a.id, a.text, a.keep),
        "questions": cmd_questions,
        "status": lambda h, a: h.status(),
        "ui": cmd_ui,
    }
    try:
        result = commands[args.command](hub, args)
    except (HubError, LLMError) as e:
        print(f"roughcut-local: error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0
