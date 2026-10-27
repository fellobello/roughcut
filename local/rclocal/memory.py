"""Shared memory every agent reads: the project's conventions and lessons learned.

Both are plain Markdown in `.hub/memory/`, so you can read and edit them between rounds.
conventions.md is the house style every file follows. lessons.md collects one-line
lessons from tasks that needed more than one try, and the newest ones go into every
builder's prompt so later files avoid the same mistake.
"""

from __future__ import annotations

# The harness runs Python tests, so the sample is Python with the standard library only.
DEFAULT_CONVENTIONS = [
    "Python 3.9 or later, standard library only. No pip packages.",
    "Code lives in the app/ package. Import other project files as `from app.geo import distance`.",
    "Tests use unittest, live in tests/, and import the code the same way.",
    "No network access and no files outside the project folder, in code or tests.",
    "Type hints on every public function, and a one-line docstring.",
    "Keep each file under about 200 lines.",
]


class Memory:
    def __init__(self, hub):
        self.hub = hub
        self.dir = hub.root / "memory"
        self.conventions_path = self.dir / "conventions.md"
        self.lessons_path = self.dir / "lessons.md"

    def init(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        if not self.conventions_path.exists():
            self.set_conventions([])
        if not self.lessons_path.exists():
            self.hub._write_text(self.lessons_path, "# Lessons\n\n")

    def conventions(self):
        return self.conventions_path.read_text(encoding="utf-8") if self.conventions_path.exists() else ""

    def set_conventions(self, extra):
        """Write the defaults plus the planner's own conventions, keeping any already there."""
        have = [line[2:] for line in self.conventions().splitlines() if line.startswith("- ")]
        rules = []
        for rule in DEFAULT_CONVENTIONS + have + [e.strip() for e in extra]:
            if rule and rule not in rules:
                rules.append(rule)
        self.hub._write_text(self.conventions_path, "# Conventions\n\n" + "".join(f"- {r}\n" for r in rules))

    def add_lesson(self, text, source):
        text = " ".join(text.split())
        if not text:
            return
        with self.hub.locked():
            current = self.lessons_path.read_text(encoding="utf-8") if self.lessons_path.exists() else "# Lessons\n\n"
            if text in current:
                return
            self.hub._write_text(self.lessons_path, current + f"- {text} ({source})\n")

    def lessons(self, last=10):
        if not self.lessons_path.exists():
            return ""
        lines = [l for l in self.lessons_path.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]
        return "\n".join(lines[-last:])
