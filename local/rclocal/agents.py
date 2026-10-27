"""The agents: one small job each, one file at a time.

Each function builds a short conversation for one role and returns the model's reply:
data checked against protocol.REPLIES, or a code file (protocol.CODE_ROLES). Agents
never touch the disk or the hub themselves. Even the builder agent, which chooses its
own next step, only names an action; the round carries it out, inside the box, and
only if the action is allowed for that file (see rounds.BuilderAgent).

Every system prompt starts with "<Role>:", which FakeLLM uses to tell calls apart,
and carries the same SAFETY rules. The rules ask; the box (stacks guard + runner
sandbox) enforces, so a model that ignores them still can't do harm.
"""

from __future__ import annotations

from .protocol import ACTIONS

TEAM = (
    "You are one member of a team that builds a small app one file at a time. Files talk to each "
    "other only through contracts: each file's purpose, exports and imports."
)
SAFETY = (
    "Safety rules: you work inside a sandbox, and the app you build must be safe to run on someone's "
    "own computer. Never write code that starts programs, opens network connections, reads or changes "
    "files outside the project, deletes anything, or reads environment variables or secrets. Code that "
    "does is rejected before it runs. Keep data in memory unless the contract says otherwise."
)
JSON_ONLY = "Reply with JSON only."

DESIGN_BRIEF = """Build the page from kit.css, a finished design system (light and dark mode, spacing,
type, focus states are all done). Link it before styles.css. Use its classes:
- Layout: <main class="app">, <header class="app-header"> with <p class="eyebrow">, <h1>, <p class="lede">;
  .stack (vertical gaps), .row (side-by-side fields that wrap on phones).
- Surfaces: .card, .card-title, <hr class="divider">.
- Forms: <div class="field"><label for=..>..</label><input ..><span class="hint">..</span></div>;
  money inputs: <div class="input-prefix"><span>$</span><input ..></div>;
  choices: <div class="seg"> of <button type="button" aria-pressed="true|false">;
  toggles: <label class="switch"><input type="checkbox"> Text</label>;
  buttons: .btn, .btn-primary, .btn-block.
- Results: <div class="stats"> of <div class="stat"><div class="stat-label">..</div><div class="stat-value">..</div></div>,
  add .highlight to the most important one; .list for rows; .badge; .empty for the empty state; .error.
Update results live as the user types or picks (input and click events), no submit button needed.
Give every input a data-sample attribute with a realistic example value (data-sample="120").
Never replace or remove the result elements: show and hide them with the hidden attribute, and put
empty-state and error text in their own elements.
Show the .empty state until the inputs are valid, and readable .error text for bad input.
Example skeleton:
<main class="app"><header class="app-header"><p class="eyebrow">Tool</p><h1>Name</h1><p class="lede">One line.</p></header>
<section class="card stack"><div class="row"><div class="field">...</div></div></section>
<section class="card"><h2 class="card-title">Result</h2><div class="stats">...</div></section></main>"""


def code_only(lang):
    return f"Reply with the complete file in one ```{lang} block and nothing else."


def _section(title, body):
    body = (body or "").strip()
    return f"## {title}\n{body}\n" if body else ""


def _chat(system, user):
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def plan(sched, stack, idea, conventions, contracts_md, answers, first):
    rules = (
        f"For each file give its path (like {stack.example}), its purpose, its exports and the other project "
        f"files it imports. Each export's signature is one exact line: {stack.export_hint}. Prefer small pure "
        "functions over data tables, and describe in each export's description what it returns for a typical input."
    )
    if first:
        job = (
            f"Plan the smallest working sample of the idea as 2 to 5 logic files in app/. {rules} Keep imports "
            "one-way, never in a circle. Do not plan tests or UI files: each logic file gets tests automatically"
            + (", and a designer makes the page from your exports. " if stack.name == "web" else ". ")
            + "List the big choices you made (storage, data format, what is left out) as guesses with the options "
            "you passed over. Add conventions only if the project needs more than the ones given. Set complete "
            "to true if these files cover the whole idea."
        )
    else:
        job = (
            "The project has the files in Contracts. Using the answers below, return only files that are new or "
            f"whose contract must change, each in full. {rules} Return an empty files list if nothing needs to "
            "change. Set complete to true when the files cover the whole idea and nothing more is worth adding. "
            "Record any new big choice as a guess."
        )
    system = f"Planner: {TEAM}\n{job}\n{SAFETY}\n{JSON_ONLY}"
    user = (
        _section("Idea", idea)
        + _section("Conventions", conventions)
        + _section("Contracts", contracts_md)
        + _section("Answers from the user", "\n".join(f"- {q['text']} -> {q['answer']}" for q in answers))
    )
    return await sched.ask("planner", _chat(system, user), "plan", reply="planner")


async def write_tests(sched, stack, brief, conventions, test_path, notes):
    """Returns (code, cut_off)."""
    system = (
        f"Tester: {TEAM}\n"
        "Write the test file for one file, using only its contract: you never see its code. Test each export "
        "once or twice with typical inputs, comparing floats with a tolerance, and name each test after the "
        f"promise it checks. Test only what the contract promises. Keep the file under 80 lines. Start the file "
        f"with:\n{stack.test_header}\n{SAFETY}\n"
        f"{code_only(stack.lang)}"
    )
    user = (
        _section("Contract", brief)
        + _section("Conventions", conventions)
        + _section("Notes", "\n".join(notes))
        + _section("Write", test_path)
    )
    return await sched.ask("tester", _chat(system, user), f"tests:{test_path}")


def builder_conversation(stack, brief, path, test_path, conventions, lessons, notes, readable, max_steps):
    """The opening of a builder agent's conversation: its job, its actions and its limits."""
    actions = "\n".join(f"- {name}: {what}" for name, what in ACTIONS.items())
    system = (
        f"Builder: {TEAM}\n"
        f"You own exactly one file, {path}, and must make it keep its contract and pass {test_path}. "
        "Work in steps. Each step, reply with one action:\n"
        f"{actions}\n"
        f"You may read only: {', '.join(readable)}. You may write only {path}. You have at most {max_steps} steps. "
        "A good order: write, test, then read the failure, write again, test again, finish.\n"
        f"{SAFETY}\n{JSON_ONLY}"
    )
    user = (
        _section("Contract", brief)
        + _section("Conventions", conventions)
        + _section("Lessons from earlier files", lessons)
        + _section("Notes", "\n".join(notes))
        + "Choose your first action."
    )
    return _chat(system, user)


async def next_step(sched, messages, label):
    return await sched.ask("builder", messages, label, reply="step")


async def write_owned_file(sched, stack, messages, path, label):
    """Ask the builder for its file, in the middle of its conversation. Returns (code, cut_off)."""
    ask = messages + [{"role": "user", "content": f"Write {path} now. {code_only(stack.lang)}"}]
    return await sched.ask("builder", ask, label)


async def file_notes(sched, brief, path, code, failure):
    system = (
        f"Notes: {TEAM}\n"
        "You just wrote the file below. List at most three choices a user of the app would notice "
        "(defaults, limits, formats) as decisions. If it needs something another file doesn't export, "
        "add a contract request naming that file's path and the exports to add; otherwise leave the list "
        f"empty. In lesson, put one line worth remembering for later files, or an empty string. {JSON_ONLY}"
    )
    user = _section("Contract", brief) + _section(path, code) + _section("Its tests still fail with", failure)
    return await sched.ask("builder", _chat(system, user), f"notes:{path}", reply="notes")


async def design_page(sched, idea, contracts_md, conventions, notes):
    """Returns (html, cut_off)."""
    system = (
        f"Designer: {TEAM}\n"
        "Write index.html, the app's only page. Link kit.css, then styles.css. Put the app's script inline in one "
        "<script type=\"module\"> that imports only the exports listed in Contracts, from './app/<file>.js', "
        "and wires them to the page. Give elements meaningful class names for styles.css to use. The very last "
        "line of the script must be document.body.dataset.ready = \"1\";\n"
        f"{DESIGN_BRIEF}\n{SAFETY}\n{code_only('html')}"
    )
    user = (
        _section("Idea", idea)
        + _section("Contracts", contracts_md)
        + _section("Conventions", conventions)
        + _section("Notes", "\n".join(notes))
    )
    return await sched.ask("designer", _chat(system, user), "design:index.html")


async def design_styles(sched, html, notes):
    """Returns (css, cut_off)."""
    system = (
        f"Designer: {TEAM}\n"
        "Write styles.css for the page below. kit.css already styles every kit class, so add only what this "
        "app needs beyond it, using kit's custom properties (var(--accent), var(--space-4) and so on). Never "
        f"restyle body or the kit classes. Keep it under 60 lines.\n{SAFETY}\n{code_only('css')}"
    )
    user = _section("index.html", html) + _section("Notes", "\n".join(notes))
    return await sched.ask("designer", _chat(system, user), "design:styles.css")


async def review(sched, brief, test_code, code, failure):
    system = (
        f"Reviewer: {TEAM}\n"
        "A file keeps failing its tests. Decide who is wrong. blame is 'code' if the file breaks its "
        "contract, 'test' if the test expects something the contract doesn't promise, or 'contract' "
        f"if the contract itself can't work as written. Give the reason in one or two sentences. {JSON_ONLY}"
    )
    user = (
        _section("Contract", brief)
        + _section("Test file", test_code)
        + _section("Code", code)
        + _section("Failure", failure)
    )
    return await sched.ask("reviewer", _chat(system, user), "review", reply="reviewer")


async def report(sched, facts):
    system = (
        f"Reporter: {TEAM}\n"
        "Write the round report's notes in plain words, three to six sentences: what was built, what "
        f"still fails or is a stub, and what the next round would build. Use only the facts given. {JSON_ONLY}"
    )
    return await sched.ask("reporter", _chat(system, facts), "report", reply="reporter")
