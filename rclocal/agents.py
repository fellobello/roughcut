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
RULES = "Rules from the owner (always follow these)"

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
  add .highlight to the most important one; .list for rows; .badge; .error.
Update results live as the user types or picks (input and click events), no submit button needed.
Results are always visible: never use the hidden attribute on them, never remove or replace them.
Before the inputs are valid, every .stat-value shows "—" (an em dash); never write example numbers into
the HTML. Put readable .error text in its own element, empty when the input is fine.
Give every input a data-sample attribute with a realistic example value (data-sample="120").
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


QUESTION_RULES = (
    "Ask at most 3 questions, only about choices that change what gets built (what it does, who it's for, "
    "what's left out, how it looks). Each question has 2 to 4 options: a label of 2 or 3 words, and a detail "
    "sentence on what picking it means. default is the label you would pick, word for word. Never ask what the "
    "user already told you, and never ask about code, files or technology."
)


async def brainstorm(sched, stack, idea, project_notes, talk, turns_left, settled=""):
    """One brainstorm turn. talk is the conversation so far as text. Returns BRAINSTORM data."""
    platform = "a web page that runs in the browser, with no server and no accounts" if stack.name == "web" else (
        "a standard-library Python program"
    )
    system = (
        "Brainstormer: You talk an app idea through with its owner before anything is built, like a good product "
        f"partner. The app will be {platform}, built by small local models, so smaller and clearer is better. "
        "Keep a brief of the app: a name, a one or two sentence pitch, must-haves (at most 5), nice-to-haves, "
        "what is out of scope, rules (every exact rule the owner gave: defaults, rounding, limits, which inputs "
        "to refuse, in the owner's words), and how it should look and feel. Anything the owner said to skip or "
        "leave out goes in out of scope, never in must-haves or nice-to-haves. Push back where it helps: say in one sentence "
        "each what you would cut, simplify or add, and why; agree when the idea is already good. Never repeat a "
        "point you made earlier in the talk; if you have nothing new to say, leave pushback empty. "
        f"{QUESTION_RULES} Set ready to true when the brief is clear enough to build the first version; "
        f"you have {turns_left} turn(s) left, so on the last one set ready to true and ask nothing. {JSON_ONLY}"
    )
    user = (_section("Idea", idea) + _section("What the project already is", project_notes)
            + _section("Already decided by the owner (never ask about these again)", settled) + _section("Talk so far", talk))
    return await sched.ask("brainstormer", _chat(system, user), "brainstorm", reply="brainstorm")


async def survey(sched, stack, path, code, other_paths):
    """Describe one existing file as a contract. Returns FILE_PLAN data."""
    system = (
        f"Surveyor: {TEAM}\n"
        "The project already exists. Read one of its files and write that file's contract: its purpose in one "
        f"sentence, every export other files use or could use (each signature one exact line: {stack.export_hint}), "
        f"1 or 2 examples per function (an exact call with literal arguments, like {stack.example_call}, and exactly "
        "what the code returns for it, as a literal), and the other project files it imports. Describe only what "
        "the code does now. "
        f"{JSON_ONLY}"
    )
    user = _section("Path", path) + _section("Other files in the project", "\n".join(other_paths)) + _section("Code", code[:8000])
    return await sched.ask("surveyor", _chat(system, user), f"survey:{path}", reply="survey")


async def pick_page_part(sched, html, problem):
    """Which one part of a working page to rewrite for a problem. Returns PAGE_PART data."""
    system = (
        "Designer: A page works but has a problem. Pick the one smallest part to rewrite: 'markup' (the HTML inside "
        "<main>), 'script' (the <script type=\"module\">), 'styles' (styles.css), or 'whole' only if the change needs "
        f"new inputs or results and new script together. Say why in one sentence. {JSON_ONLY}"
    )
    user = _section("Problem", problem) + _section("index.html", html)
    return await sched.ask("designer", _chat(system, user), "design:pick-part", reply="page_part")


PART_LANG = {"markup": "html", "script": "js", "styles": "css"}
PART_WHAT = {
    "markup": "the <main> element of index.html, from <main to </main>",
    "script": "the code inside <script type=\"module\"> (without the script tags)",
    "styles": "the whole of styles.css",
}


async def fix_page_part(sched, part, html, css, problem, contracts_md, notes):
    """Rewrite one part of the page and nothing else. Returns (code, cut_off)."""
    system = (
        f"Designer: {TEAM}\n"
        f"Fix a problem in the page by rewriting only {PART_WHAT[part]}. Everything else stays exactly as it is, "
        "so keep every id and class the rest of the page uses. "
        + ("The script imports only the exports listed in Contracts and its last line must be "
           "document.body.dataset.ready = \"1\";\n" if part == "script" else "\n")
        + f"{DESIGN_BRIEF}\n{SAFETY}\n{code_only(PART_LANG[part])}"
    )
    user = (
        _section("Problem to fix", problem)
        + _section("index.html now", html)
        + (_section("styles.css now", css) if part == "styles" else "")
        + _section("Contracts", contracts_md)
        + _section("Notes", "\n".join(notes))
    )
    return await sched.ask("designer", _chat(system, user), f"design:fix-{part}")


def _persona(persona):
    return f"User: You are {persona['name']}, {persona['who']} You are trying a new app for the first time."


async def user_plan(sched, persona, brief, controls):
    """A test user's steps on the page's controls. Returns USER_PLAN data."""
    system = (
        f"{_persona(persona)} {persona['how']} You can only use the controls listed, by their id or label; if there is no "
        "button, the results update by themselves as you type. Plan 3 to 8 steps: fill (type a value, or pick an option by its text) or click (a button). After each step, say in "
        "expect the one number you'd look for on the page if the app works (like 34.50), worked out by hand, or a "
        "word of the error message you'd expect; leave expect empty if you wouldn't check anything yet. "
        f"{JSON_ONLY}"
    )
    user = _section("What the app is for", brief) + _section("What's on the screen", controls)
    return await sched.ask("user", _chat(system, user), f"user:{persona['key']}:plan", reply="user_plan")


async def user_verdict(sched, persona, brief, transcript, facts):
    """What a test user thought of the app after trying it. Returns USER_VERDICT data."""
    system = (
        f"{_persona(persona)} Below is what happened when you tried it. Did it do what you wanted? List the problems "
        "a real user would notice: a wrong result, NaN or blank results, nothing happening, a missing or confusing "
        "error message, a confusing label. Write each problem as a sentence that says what went wrong (like: Each "
        "person's share showed $138.00 for 4 people, but should be $34.50). Before you call a number wrong, work it "
        "out again yourself; if your own "
        "expectation was the mistake, it isn't a problem. where is 'logic' when a calculated result is wrong and "
        "'page' for everything else. severity is 'high' only when the app fails at its main job. In wish, one thing "
        f"you'd like it to do, or an empty string. {JSON_ONLY}"
    )
    user = _section("What the app is for", brief) + _section("What happened", transcript) + _section("Checked automatically", facts)
    return await sched.ask("user", _chat(system, user), f"user:{persona['key']}:verdict", reply="user_verdict")


async def plan(sched, stack, idea, conventions, contracts_md, answers, first, rules=""):
    how = (
        f"For each file give its path (like {stack.example}), its purpose, its exports and the other project "
        f"files it imports. Each export's signature is one exact line: {stack.export_hint}. Prefer small pure "
        "functions over data tables. Give every function 1 or 2 examples that pin down the part that could be read "
        f"two ways (an edge, invalid input, rounding, an order): call is one exact call with literal arguments, like "
        f"{stack.example_call}, and returns is exactly what it returns, as a literal in the same language, like "
        f"{stack.example_returns}, or [58, 57]: a value, never a sentence. The tests check these examples exactly, "
        "so work them out carefully, and make them follow the owner's rules. Write signatures, descriptions and "
        f"examples in {stack.lang_name}, with its own library names. Every input a function needs must be one of "
        "its arguments: for each feature, first think what the user types or picks for it (a split by percentage "
        "needs each person's percentage), and make those arguments. Two functions never share the same examples. "
        "No parameter has the same name as a function the file imports."
        + (" Logic files never touch the page (no document, window, elements or localStorage): a theme file "
           "returns the next theme's name, and the page's script applies it." if stack.name == "web" else "")
    )
    if first:
        job = (
            f"Plan the smallest working sample of the idea as 2 to 5 logic files in app/. {how} Keep imports "
            "one-way, never in a circle. Do not plan tests or UI files: each logic file gets tests automatically"
            + (", and a designer makes the page from your exports. " if stack.name == "web" else ". ")
            + "List at most 3 big choices a user would care about (what is left out, a rounding or default rule) "
            "as guesses. Each guess has 2 to 4 options, including the one you picked: a label of 2 or 3 words and "
            "a detail sentence on what it means; choice is the label you picked, word for word. Skip anything the "
            "brief or the answers already settle. "
            "Add conventions only if the project needs more than the ones given. Set complete "
            "to true if these files cover the whole idea."
        )
    else:
        job = (
            "The project has the files in Contracts. Using the answers below, return only files that are new or "
            f"whose contract must change, each in full. {how} Return an empty files list if nothing needs to "
            "change. Set complete to true when the files cover the whole idea and nothing more is worth adding. "
            "Record any new big choice as a guess."
        )
    system = f"Planner: {TEAM}\n{job}\n{SAFETY}\n{JSON_ONLY}"
    user = (
        _section("Idea", idea)
        + _section("Conventions", conventions)
        + _section("Contracts", contracts_md)
        + _section(RULES, rules)
        + _section("New answers from the owner", "\n".join(f"- {q['text']} -> {q['answer']}" for q in answers))
    )
    return await sched.ask("planner", _chat(system, user), "plan", reply="planner")


async def split(sched, stack, brief, folder, test_code, code, failure, rules=""):
    """Break a file the builder keeps getting wrong into smaller files. Returns SPLIT data."""
    system = (
        f"Planner: {TEAM}\n"
        "A builder failed twice to write the file below so that its tests pass. Split the work into 2 to 4 smaller "
        f"files in {folder}/, each with one clear job that is easy to get right on its own (like the math, the data, "
        "the steps). The original file will import the pieces and must keep its contract exactly, so together the "
        "pieces must cover everything it promises. Pieces may import each other, one way only, but never the original "
        f"file. For each piece give its path, purpose, exports and imports. Each export's signature is one exact line: "
        f"{stack.export_hint}. Give every function 1 or 2 examples: call is one exact call with literal arguments, like "
        f"{stack.example_call}, and returns is exactly what it returns, as a literal value, like {stack.example_returns}. "
        f"Follow the owner's rules. In why, one sentence on what made the file hard. {SAFETY}\n{JSON_ONLY}"
    )
    user = (
        _section(RULES, rules)
        + _section("The file's contract", brief)
        + _section("Its tests", test_code[:4000])
        + _section("The builder's best attempt", code[:4000])
        + _section("How it fails", failure[-1500:])
    )
    return await sched.ask("planner", _chat(system, user), "split", reply="split")


async def write_tests(sched, stack, brief, conventions, test_path, notes, rules=""):
    """Returns (code, cut_off)."""
    system = (
        f"Tester: {TEAM}\n"
        "Write the test file for one file, using only its contract: you never see its code. Test each export "
        "once or twice with typical inputs, comparing floats with a tolerance, and name each test after the "
        "promise it checks. Test only what the contract and the owner's rules promise: values and behaviour. Never "
        "test the exact wording of text, messages, labels or descriptions, or the names of keys, unless the contract "
        "spells them out word for word; check that they exist instead. Test only inputs the contract covers. Work out "
        "every exact value step by step from the contract before you write it; better still, check what must be true "
        "whatever the numbers (parts add up to the whole, one result per item, the order the contract gives) and use "
        "exact values only where the arithmetic is simple. The contract's examples are added to your file "
        f"automatically, so test other cases. Keep the file under 80 lines. Start the file "
        f"with:\n{stack.test_header}\n{SAFETY}\n"
        f"{code_only(stack.lang)}"
    )
    user = (
        _section("Contract", brief)
        + _section(RULES, rules)
        + _section("Conventions", conventions)
        + _section("Notes", "\n".join(notes))
        + _section("Write", test_path)
    )
    return await sched.ask("tester", _chat(system, user), f"tests:{test_path}")


def builder_conversation(stack, brief, path, test_path, conventions, lessons, notes, readable, max_steps, rules=""):
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
        + _section(RULES, rules)
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
        "You just wrote the file below. List at most two choices a user of the app would notice and might "
        "want changed (a default, a limit, a rounding rule), as decisions. Leave out anything that only restates "
        "the contract, and leave the list empty if there is nothing like that. If it needs something another file doesn't export, "
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


async def review(sched, brief, test_code, code, failure, rules=""):
    system = (
        f"Reviewer: {TEAM}\n"
        "A file keeps failing its tests. Decide who is wrong. blame is 'code' if the file breaks its contract or "
        "the owner's rules. blame is 'test' if a test expects something the contract doesn't promise, like the exact "
        "wording of a message or description, or a rule the owner didn't give. blame is 'contract' if the contract "
        "can't work as written: it contradicts the owner's rules, its examples are wrong, or a function needs an input "
        "it isn't given. Don't blame the code for doing what the owner asked. Examples and tests are written by small "
        "models and are often wrong, and the description and the owner's rules come first. So: work out from the "
        "description and the rules what the right value is, then compare it with the failure's actual and expected "
        "values. If the actual value is the right one, the code is not to blame: blame 'contract' when a wrong value "
        "is one of the contract's examples, 'test' when it's only in the test file. Give the reason in one or two "
        "sentences. To blame the contract, put in quote the exact words of the contract that can't work, copied "
        "from it; without a quote the blame goes to the code. For 'code' or 'test', quote is an empty string. "
        f"{JSON_ONLY}"
    )
    user = (
        _section(RULES, rules)
        + _section("Contract", brief)
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
