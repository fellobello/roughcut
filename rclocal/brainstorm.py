"""The brainstorm: talking the idea through before anything is built.

Each turn, the brainstormer (agents.brainstorm) updates the brief, pushes back where it
would cut, simplify or add something, and asks at most 3 questions, each with options
and the one it would pick. You answer each with Enter (take the pick), a letter (an
option) or your own words, and can add anything else. It ends when the brainstormer says
the brief is clear and you agree, when you type "build", or after `brainstorm_turns`.

Everything is kept: the talk in .hub/brainstorm.json, the brief in .hub/brief.json and
PROJECT.md, and every question with your answer in .hub/questions/ like any other, so
the planner sees your answers and a later brainstorm about changes starts from them.
Turn() is separate from the terminal (cli.py asks the questions), so tests drive it directly.
"""

from __future__ import annotations

import json

from . import agents, project
from .hub_local import HUB_OWNER

LETTERS = "abcd"


class Brainstorm:
    def __init__(self, hub):
        self.hub = hub
        self.path = hub.root / "brainstorm.json"
        self.talk = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
        self.last_id = None  # the hub question the last answer went to

    def start(self, thoughts):
        """Begin a session with what you want: the idea, or the changes you have in mind."""
        if self.hub.contracts.all():  # changes to a project that exists: the planner should see them as asked
            q = self.hub.ask(HUB_OWNER, "What should change next?", critical=True, choice="(in your words)")
            self.hub.answer(q["id"], thoughts)
        self.talk.append({"who": "you", "text": thoughts.strip(), "start": True})
        self.save()

    def transcript(self):
        lines = []
        for t in self.talk[-30:]:
            who = "Owner" if t["who"] == "you" else "Brainstormer"
            lines.append(f"{who}: {t['text']}")
        return "\n".join(lines)

    async def turn(self, sched, idea, turns_left):
        notes = ""
        if (self.hub.project / "PROJECT.md").exists() and self.hub.contracts.all():
            notes = (self.hub.project / "PROJECT.md").read_text(encoding="utf-8")[:5000]
        reply = await agents.brainstorm(sched, self.hub.stack, idea, notes, self.transcript(), turns_left)
        questions = []
        for asked in [q for q in reply["questions"] if q["text"].strip()][:3]:
            # options as short labels, with what each means kept for hover
            details = {" ".join(o["label"].split()): o["detail"].strip() for o in asked["options"] if o["label"].strip()}
            options, default = list(details)[:4], " ".join(asked["default"].split())
            if default and default not in options:
                options = ([default] + options)[:4]
            questions.append({**asked, "options": options, "details": details, "default": default})
        reply = {**reply, "questions": questions}
        said_before = " ".join(t["text"] for t in self.talk if t["who"] == "brainstormer")
        reply["pushback"] = [p for p in reply["pushback"] if p.strip() and p.strip() not in said_before]
        project.save_brief(self.hub, reply["brief"])
        said = reply["pushback"] + [f"Q: {q['text']} ({' / '.join(q['options'])}; I'd pick {q['default']})"
                                    for q in reply["questions"]]
        self.talk.append({"who": "brainstormer", "text": " ".join(said) or "The brief looks clear to me."})
        self.save()
        return reply

    def answer(self, question, reply):
        """Record your answer to one question. reply: "" (the pick), a letter, or your own words."""
        text = reply.strip()
        if not text:
            text = question["default"] or (question["options"] or ["(no answer)"])[0]
        elif len(text) == 1 and text.lower() in LETTERS[: len(question["options"])]:
            text = question["options"][LETTERS.index(text.lower())]
        details = question.get("details", {})
        q = next((q for q in self.hub.questions() if q["task"] == HUB_OWNER and q["text"] == question["text"]), None)
        if q is None:  # asked again (an answer being changed) it's the same question, not a new one
            q = self.hub.ask(
                HUB_OWNER, question["text"], critical=True, choice=question["default"] or text,
                why=question["why"], options=[{"label": o, "detail": details.get(o, "")} for o in question["options"]],
            )
        self.hub.answer(q["id"], text)
        self.last_id = q["id"]
        self.talk.append({"who": "you", "text": f"{question['text']} -> {text}"})
        self.save()
        return text

    def add(self, text):
        if text.strip():
            self.talk.append({"who": "you", "text": text.strip()})
            self.save()

    def save(self):
        self.hub._write_text(self.path, json.dumps(self.talk, indent=2) + "\n")
