"""Chat: one temporary conversation with a local model that can see the open project.

Nothing is kept: the page holds the conversation and sends it with every message, and
switching tabs starts a new one. Each reply gets the project as it is now, read fresh
from the hub: the brief, every file's contract, how each task stands and why, the
questions, the last round's report and the latest notes. So "why is split.js stuck?"
gets an answer from what roughcut actually recorded, not a guess. The chat can't change
anything; it only reads.

The model's context is 8k tokens, so each section of the project details has its own cap
(the newest notes are the ones kept), and the conversation is capped at HISTORY_CHARS,
oldest turns dropped first.
"""

from __future__ import annotations

import json

from . import project

CONTEXT_CHARS = 13000  # about 3,300 tokens; each section has its own share below
HISTORY_CHARS = 10000
REPLY_TOKENS = 1200
NOTES = 25  # latest event notes shown
SYSTEM = (
    "You are the chat in Roughcut Local, a studio where small local models plan, test and build an app in rounds. "
    "Answer plainly and briefly. When the question is about the project, answer from the project details below and "
    "name the file, task or question you mean; if the details don't say, say so rather than guess. You don't see the "
    "code or the page itself, only what's below (the test users' report says what they saw on the page). Never say "
    "the page or a file does something unless a detail below says so: \"works in the browser\" only means the page "
    "loads and reacts, and a passing file only meets its own tests. You can't change "
    "files, answer questions or start a build from here: say how the owner can do it in the studio instead (Talk to "
    "change the plan or answer questions, Editor to change a file, Build to run a round)."
)


def cut(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 12].rstrip() + "\n… (cut)"


def project_context(hub):
    """What roughcut knows about the open project, newest state first, as text for the model."""
    if hub is None:
        return "No project is open."
    cfg = hub.config()
    parts = [f"Project: {hub.project.name} ({hub.local_config()['stack']} app), round {cfg['round']}."]
    parts.append("## What it's for\n" + cut(project.brief_text(hub), 2000))
    tasks = hub.tasks()
    if tasks:
        rows = [f"- {t['id']} ({t['kind']}): {t['status']}, {t['attempts']} attempts"
                + (f", blame {t['blame']}" if t.get("blame") else "") + (f": {cut(t['summary'], 240)}" if t.get("summary") else "")
                for t in sorted(tasks, key=lambda t: (t["status"] == "done", t["id"]))]
        parts.append("## Tasks (not done first)\n" + cut("\n".join(rows), 2500))
    contracts = hub.contracts.all()
    if contracts:
        lines = []
        for c in contracts:
            lines.append(f"- {c['path']}: {c['purpose']}" + (f" (imports {', '.join(c['imports'])})" if c["imports"] else ""))
            for e in c["exports"]:
                examples = "; ".join(f"{x['call']} -> {x['returns']}" for x in e.get("examples", [])[:2])
                lines.append(f"  - {e['signature']}" + (f" e.g. {examples}" if examples else ""))
        parts.append("## Files and their contracts\n" + cut("\n".join(lines), 2500))
    questions = [q for q in hub.questions() if q["status"] in ("open", "decided", "answered")]
    if questions:
        lines = [f"- {q['id']} [{q['status']}]: {cut(q['text'], 200)} -> {q.get('answer') or q.get('choice') or '?'}"
                 for q in questions[-15:]]
        parts.append("## Latest questions\n" + cut("\n".join(lines), 1200))
    reports = sorted((hub.root / "rounds").glob("[0-9]*.notes.md"), key=lambda p: int(p.name.split(".")[0]))
    if reports:
        parts.append(f"## Round {reports[-1].name.split('.')[0]} report\n" + cut(reports[-1].read_text(encoding="utf-8"), 800))
    users = sorted((hub.root / "rounds").glob("[0-9]*.users.md"), key=lambda p: int(p.name.split(".")[0]))
    if users:
        parts.append(f"## What the test users saw on the page (round {users[-1].name.split('.')[0]})\n"
                     + cut(users[-1].read_text(encoding="utf-8"), 1200))
    notes = latest_notes(hub)
    if notes:
        lines, size = [], 0
        for note in reversed(notes):  # the newest ones, as many as fit
            size += len(line := f"- {cut(note, 220)}") + 1
            if size > 1600:
                break
            lines.append(line)
        parts.append("## Latest notes (oldest first)\n" + "\n".join(lines[::-1]))
    return cut("\n\n".join(parts), CONTEXT_CHARS)


def latest_notes(hub):
    path = hub.root / "events.jsonl"
    if not path.exists():
        return []
    notes = []
    for line in path.read_text(encoding="utf-8").splitlines()[-600:]:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "note" and event.get("text"):
            notes.append(event["text"])
    return notes[-NOTES:]


def messages_for(history, context):
    """The model's conversation: the rules and project, then as many recent turns as fit."""
    kept, size = [], 0
    for m in reversed(history):
        if m.get("role") not in ("user", "assistant") or not str(m.get("content", "")).strip():
            continue
        size += len(m["content"])
        if kept and size > HISTORY_CHARS:
            break
        kept.append({"role": m["role"], "content": cut(m["content"], HISTORY_CHARS)})
    return [{"role": "system", "content": f"{SYSTEM}\n\n# The project now\n{context}"}] + kept[::-1]


def reply(llm, model, history, hub, live):
    """Stream one reply: `live(text_so_far)` as it comes in. Returns the whole reply."""
    def on_text(text, thinking):
        live(text)

    think = False if model.startswith("qwen3") else None  # thinking is slow here, and other models refuse the flag
    text, _ = llm.chat_text(model, messages_for(history, project_context(hub)), think=think,
                            max_tokens=REPLY_TOKENS, live=on_text)
    return text
