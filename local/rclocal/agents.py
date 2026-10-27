"""The agents: one small job each, one file at a time.

Each function builds a short prompt for one role and returns the model's reply: data
checked against protocol.REPLIES, or a code file (see protocol.CODE_ROLES). Agents only
produce replies. They never write files or touch the hub; the round controller does
that. Prompts stay small on purpose: an 8B model does well with one file's contract and
badly with a whole project.

Every system prompt starts with "<Role>:", which FakeLLM uses to tell calls apart.
"""

from __future__ import annotations

TEAM = (
    "You are one member of a team that builds a program one file at a time. Files talk to each "
    "other only through contracts: each file's purpose, exports and imports."
)
JSON_ONLY = "Reply with JSON only."
CODE_ONLY = "Reply with the complete file in one ```python block and nothing else."


def _section(title, body):
    body = (body or "").strip()
    return f"## {title}\n{body}\n" if body else ""


async def plan(sched, idea, conventions, contracts_md, answers, first):
    if first:
        job = (
            "Plan the smallest working sample of the idea as 2 to 6 Python files in the app/ package. "
            "For each file give its path (like app/geo.py), its purpose, its exports and the other project "
            "files it imports. Each export's signature is one exact Python line: 'def name(arg: type) -> type' "
            "for a function, 'class Name' for a class, or 'NAME: type' for a constant. Prefer functions over "
            "data tables, and describe in each export's description what it returns for a typical input. "
            "Keep imports one-way, never in a circle. Do not plan test files; each file gets tests "
            "automatically. List the big choices you made (storage, interface, data format, what is left "
            "out) as guesses with the options you passed over. Add conventions only if the project needs "
            "more than the ones given. Set complete to true if these files cover the whole idea."
        )
    else:
        job = (
            "The project has the files in Contracts. Using the answers below, return only files that are "
            "new or whose contract must change, each in full, with signatures written as exact Python lines. "
            "Return an empty files list if nothing needs to change. Set complete to true when the files cover "
            "the whole idea and nothing more is worth adding. Record any new big choice as a guess."
        )
    system = f"Planner: {TEAM}\n{job}\n{JSON_ONLY}"
    user = (
        _section("Idea", idea)
        + _section("Conventions", conventions)
        + _section("Contracts", contracts_md)
        + _section("Answers from the user", "\n".join(f"- {q['text']} -> {q['answer']}" for q in answers))
    )
    return await sched.ask("planner", system, user, "plan", reply="planner")


async def write_tests(sched, brief, conventions, test_path, notes):
    """Returns (code, cut_off)."""
    system = (
        f"Tester: {TEAM}\n"
        "Write the unittest file for one file, using only its contract: you never see its code. Test "
        "each export once or twice with typical inputs, using assertAlmostEqual for floats, with names "
        "that say which promise broke. Test only what the contract promises. Keep the file under 80 lines. "
        f"{CODE_ONLY}"
    )
    user = (
        _section("Contract", brief)
        + _section("Conventions", conventions)
        + _section("Notes", "\n".join(notes))
        + _section("Write", test_path)
    )
    return await sched.ask("tester", system, user, f"tests:{test_path}")


async def build_file(sched, brief, path, conventions, lessons, notes, previous, failure):
    """Returns (code, cut_off)."""
    system = (
        f"Builder: {TEAM}\n"
        "Write one complete Python file that keeps its contract exactly: every export with the exact "
        "signature. Use the files under 'It may use' only through their listed exports. "
        f"{CODE_ONLY}"
    )
    user = (
        _section("Contract", brief)
        + _section("Conventions", conventions)
        + _section("Lessons from earlier files", lessons)
        + _section("Notes", "\n".join(notes))
        + _section("Your previous version", previous)
        + _section("Its tests failed with", failure)
        + _section("Write", path)
    )
    return await sched.ask("builder", system, user, f"build:{path}")


async def file_notes(sched, brief, path, code, failure):
    system = (
        f"Notes: {TEAM}\n"
        "You just wrote the file below. List at most three choices a user of the program would notice "
        "(defaults, limits, formats) as decisions. If it needs something another file doesn't export, "
        "add a contract request naming that file's path and the exports to add; otherwise leave the list "
        "empty. In lesson, put one line worth remembering for later files, or an empty string. "
        f"{JSON_ONLY}"
    )
    user = _section("Contract", brief) + _section(path, code) + _section("Its tests still fail with", failure)
    return await sched.ask("builder", system, user, f"notes:{path}", reply="notes")


async def review(sched, brief, test_code, code, failure):
    system = (
        f"Reviewer: {TEAM}\n"
        "A file keeps failing its tests. Decide who is wrong. blame is 'code' if the file breaks its "
        "contract, 'test' if the test expects something the contract doesn't promise, or 'contract' "
        "if the contract itself can't work as written. Give the reason in one or two sentences. "
        f"{JSON_ONLY}"
    )
    user = (
        _section("Contract", brief)
        + _section("Test file", test_code)
        + _section("Code", code)
        + _section("Failure", failure)
    )
    return await sched.ask("reviewer", system, user, "review", reply="reviewer")


async def report(sched, facts):
    system = (
        f"Reporter: {TEAM}\n"
        "Write the round report's notes in plain words, three to six sentences: what was built, what "
        "still fails or is a stub, and what the next round would build. Use only the facts given. "
        f"{JSON_ONLY}"
    )
    return await sched.ask("reporter", system, facts, "report", reply="reporter")
