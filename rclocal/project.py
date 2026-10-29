"""What a project is, kept where you and every agent can find it between sessions.

- The brief (.hub/brief.json): what the app is, agreed in the brainstorm.
- PROJECT.md, at the project's top: the brief, how the app works (every file's job and
  exports, from the contracts), what's been decided, the conventions, the round history
  and how to run it. It is rewritten after every brainstorm and round, so coming back to
  a project months later means reading one file, and so does every agent: the
  brainstormer reads it before talking about changes.
- The project list (~/.roughcut-local/projects.json): every project you've made or
  opened, so `roughcut-local projects` and `open <name>` find them again.

Bringing in a project that wasn't made here (a folder or a GitHub repo) works on a copy
in the projects folder. Its logic files that fit the stack's layout (app/*.js, app/*.py)
are read by the surveyor, which writes a contract for each, and are kept as built; their
tests are written from those contracts in the next round. Other files are listed in
PROJECT.md as reference, and nothing in the copy runs outside the box.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import agents
from .contracts import ContractError, file_id
from .llm import LLMError
from .protocol import BRIEF, validate

SKIP_DIRS = {".git", ".hub", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".worktrees", "tests"}
MAX_SURVEY = 12  # files the surveyor reads when a project is brought in
MAX_FILE = 30_000  # bytes; bigger files are listed but not read
AGREED = "\n\n## Agreed in the brainstorm"
GITHUB = re.compile(r"^(https://github\.com/|git@github\.com:)[\w.-]+/[\w.-]+?(\.git)?/?$")


def home():
    return Path(os.environ.get("ROUGHCUT_LOCAL_HOME", Path.home() / ".roughcut-local"))


def projects_dir():
    return Path(os.environ.get("ROUGHCUT_PROJECTS", Path.home() / "roughcut-projects"))


FILLER = {"a", "an", "the", "for", "of", "to", "and", "with", "my", "that", "i", "want", "app", "simple", "small", "some",
          "where", "which", "who", "when", "can", "lets", "let", "me", "you", "your", "it", "in", "on", "see", "so"}


def slug(text, fallback="app"):
    """A folder name from an idea: its first few words that carry meaning."""
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in FILLER][:3]
    return "-".join(words) or fallback


def free_folder(name):
    base = projects_dir() / name
    path, n = base, 2
    while path.exists():
        path, n = base.with_name(f"{base.name}-{n}"), n + 1
    return path


# The brief


def brief_path(hub):
    return hub.root / "brief.json"


def load_brief(hub):
    path = brief_path(hub)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save_brief(hub, brief):
    validate(brief, BRIEF, "brief")
    hub._write(brief_path(hub), brief)


def brief_markdown(brief):
    def items(title, values):
        values = [v for v in values if v.strip()]
        return [f"**{title}:**", ""] + [f"- {v}" for v in values] + [""] if values else []

    out = [brief["pitch"].strip(), ""]
    out += items("Must have", brief["must_have"])
    out += items("Nice to have", brief["nice_to_have"])
    out += items("Left out", brief["out_of_scope"])
    out += items("Rules", brief.get("rules", []))
    if brief["look"].strip():
        out += [f"**Look and feel:** {brief['look'].strip()}", ""]
    return "\n".join(out).strip()


def brief_text(hub):
    """What the app is for, as agents get it: the brief if there is one, else the idea."""
    brief = load_brief(hub)
    if brief:
        return f"{brief['name']}\n{brief_markdown(brief)}"
    idea = hub.root / "idea.md"
    return idea.read_text(encoding="utf-8").strip() if idea.exists() else ""


def set_idea_from_brief(hub, original):
    """The planner plans from idea.md, so after a brainstorm it holds the original words and the brief."""
    brief = load_brief(hub)
    original = original.split(AGREED)[0]
    text = original.strip()
    if brief:
        text = f"{original.strip()}{AGREED}\n\n# {brief['name']}\n\n{brief_markdown(brief)}"
    hub._write_text(hub.root / "idea.md", text + "\n")


# PROJECT.md


def render(hub, latest_notes=""):
    brief = load_brief(hub)
    name = brief["name"] if brief else hub.project.name
    stack = hub.stack
    out = [
        f"# {name}",
        "",
        "_Kept up to date by roughcut-local after every brainstorm and round. Read this first when you come back; "
        "every agent does._",
        "",
        "## What it is",
        "",
        brief_markdown(brief) if brief else (hub.root / "idea.md").read_text(encoding="utf-8").strip(),
        "",
        "## How it works",
        "",
    ]
    contracts = hub.contracts.all()
    for c in contracts:
        exports = ", ".join(f"`{e['name']}`" for e in c["exports"]) or "nothing"
        uses = f" Uses {', '.join(f'`{i}`' for i in c['imports'])}." if c["imports"] else ""
        out.append(f"- `{c['path']}`: {c['purpose']} Exports {exports}.{uses}")
    if stack.name == "web" and (hub.project / "index.html").exists():
        out.append("- `index.html` and `styles.css`: the page, built on `kit.css`; its script imports from `app/`.")
    if not contracts:
        out.append("Nothing built yet.")
    out += ["", "Each file's full contract (signatures, what it promises) is in `CONTRACTS.md`.", ""]
    reference = (hub.root / "reference.md")
    if reference.exists():
        out += ["## Files brought in as reference", "", reference.read_text(encoding="utf-8").strip(), ""]
    questions = hub.questions()
    settled = [q for q in questions if q["status"] == "answered"]
    guesses = [q for q in questions if q["status"] == "decided" and q["critical"]]
    out += ["## Decided", ""]
    out += [f"- {q['text']} → **{q['answer']}**" for q in settled] or ["Nothing yet."]
    if guesses:
        out += ["", "Guessed, not confirmed yet (see `QUESTIONS.md`):", ""]
        out += [f"- `{q['id']}` {q['text']} → {q['choice']}" for q in guesses]
    out += ["", "## Conventions", "", hub.memory.conventions().replace("# Conventions", "").strip(), ""]
    lessons = hub.memory.lessons()
    if lessons:
        out += ["## Lessons", "", lessons, ""]
    out += ["## Rounds", ""]
    for notes in sorted(hub.rounds_dir.glob("*.notes.md"), key=lambda p: int(p.stem.split(".")[0])):
        first = notes.read_text(encoding="utf-8").strip().split(". ")[0].strip()
        users = hub.rounds_dir / f"{notes.stem.split('.')[0]}.users.md"
        out.append(f"- Round {notes.stem.split('.')[0]}: {first.rstrip('.')}." + (" Test users: see "
                   f"`.hub/rounds/{users.name}`." if users.exists() else ""))
    if not list(hub.rounds_dir.glob("*.notes.md")):
        out.append("No rounds yet.")
    test_cmd = "node --test tests/*.test.mjs" if stack.name == "web" else "python3 -m unittest discover -s tests"
    out += [
        "",
        "## How to run it",
        "",
        "```bash",
        f"roughcut-local --project {hub.project} app      # open the app in your browser" if stack.name == "web" else
        f"cd {hub.project} && python3 -m app",
        f"cd {hub.project} && {test_cmd}",
        f"roughcut-local open {hub.project}               # talk about changes, then build another round",
        "```",
        "",
    ]
    hub._write_text(hub.project / "PROJECT.md", "\n".join(out))
    remember(hub)
    return hub.project / "PROJECT.md"


# The project list


def _registry_path():
    return home() / "projects.json"


def registry():
    path = _registry_path()
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        return {}


def remember(hub):
    brief = load_brief(hub)
    entries = registry()
    entries[str(hub.project)] = {
        "name": brief["name"] if brief else hub.project.name,
        "stack": hub.stack.name,
        "round": hub.config()["round"],
        "updated": time.strftime("%Y-%m-%d %H:%M"),
    }
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def find(name_or_path):
    """A project folder from a path, or from a name or folder name in the project list."""
    path = Path(name_or_path).expanduser()
    if path.exists():
        return path.resolve()
    wanted = name_or_path.lower()
    for folder, entry in registry().items():
        if wanted in (entry["name"].lower(), Path(folder).name.lower()) and Path(folder).exists():
            return Path(folder)
    return None


# Bringing a project in


def is_github(text):
    return bool(GITHUB.match(text.strip()))


def clone(url):
    """Clone a GitHub repo into the projects folder (no hooks, no submodules). Returns its folder."""
    name = slug(url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1], "repo")
    target = free_folder(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "clone", "--depth", "1", "--no-recurse-submodules", url, str(target)],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr.strip()[-400:]}")
    return target


def locate(target, in_place=False):
    """The folder to work in for a path, a project name or a GitHub URL. Returns (folder, needs bringing in).

    A GitHub repo is cloned into the projects folder; any other folder that isn't a
    roughcut-local project yet is copied there, unless in_place.
    """
    if is_github(target):
        folder = clone(target)
    else:
        folder = find(target)
        if folder is None:
            raise RuntimeError(f"no project or folder called {target!r}")
    if (folder / ".hub" / "hub.json").exists():
        return folder, False
    if not is_github(target) and not in_place:
        folder = copy_folder(folder)
    return folder, True


def bring_in(folder, llm, stack=None, echo=None):
    """Make a folder a roughcut-local project and survey its code. Returns (hub, what was found)."""
    import asyncio

    from .hub_local import LocalHub
    from .rounds import commit
    from .scheduler import Scheduler

    hub = LocalHub(folder)
    hub.init(first_words(folder), {"stack": stack or guess_stack(folder)})
    hub.events.echo = echo
    found = asyncio.run(survey(hub, Scheduler(llm, hub.local_config(), hub.events), hub.events))
    commit(hub.project, "Brought in")
    render(hub)
    return hub, found


def copy_folder(source):
    target = free_folder(slug(source.name, "project"))
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("node_modules", ".venv", "venv", "__pycache__", ".worktrees"))
    return target


def guess_stack(folder):
    py = js = 0
    for path in source_files(folder):
        py += path.suffix == ".py"
        js += path.suffix in (".js", ".mjs", ".html")
    return "python" if py > js else "web"


def source_files(folder):
    found = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if Path(name).suffix in (".py", ".js", ".mjs", ".html", ".css") and name != "kit.css":
                found.append(Path(root) / name)
    return found


def first_words(folder):
    """A starting idea for a project brought in: the start of its README, or its name."""
    for name in ("README.md", "readme.md", "README"):
        path = folder / name
        if path.exists():
            text = " ".join(path.read_text(encoding="utf-8", errors="replace").split())
            return text[:600]
    return f"The existing project {folder.name}."


async def survey(hub, sched, events):
    """Write contracts for the files that fit the stack's layout, and list the rest as reference."""
    stack, project = hub.stack, hub.project
    files = [p.relative_to(project).as_posix() for p in source_files(project)]
    own = [f for f in files if stack.check_path(f)][:MAX_SURVEY]
    others = [f for f in files if f not in own and f not in ("index.html", "styles.css")]
    known = set(own)
    for path in own:
        code = (project / path).read_text(encoding="utf-8", errors="replace")
        if len(code) > MAX_FILE:
            others.append(path)
            continue
        try:
            plan = await agents.survey(sched, stack, path, code, [f for f in own if f != path])
        except LLMError as e:
            events.emit("note", text=f"couldn't survey {path}: {e}")
            continue
        plan = {**plan, "path": path, "imports": [i for i in plan["imports"] if i in known and i != path]}
        try:
            contract, _ = hub.contracts.upsert(plan, hub.config()["round"], "surveyed from the existing code")
        except (ContractError, ValueError) as e:
            events.emit("note", text=f"skipped {path}: {e}")
            continue
        events.emit("contract", path=path, version=contract["version"], reason="surveyed from the existing code")
    for contract in hub.contracts.all():
        test_id, build_id = contract["id"] + "-tests", contract["id"]
        hub.add_task(test_id, f"Tests for {contract['path']}", "test", contract["path"], (), contract["purpose"])
        deps = [test_id] + [file_id(p) for p in contract["imports"]]
        hub.add_task(build_id, f"Build {contract['path']}", "build", contract["path"], deps, contract["purpose"])
        hub.mark_done(build_id, "brought in from the existing project")
    if others:
        lines = []
        for path in others[:40]:
            size = (project / path).stat().st_size
            lines.append(f"- `{path}` ({size:,} bytes)")
        hub._write_text(hub.root / "reference.md", "\n".join(lines) + "\n")
    hub.contracts.render()
    return {"surveyed": [c["path"] for c in hub.contracts.all()], "reference": others}
