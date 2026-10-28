"""Every message format in roughcut-local, in one place.

Parts of the system never pass loose text to each other. There are two kinds of reply:

- Data replies (plans, notes, reviews, reports) are held to one of the schemas in
  REPLIES. Ollama's `format` field enforces the shape while the model writes, and
  `validate` checks it again on arrival.
- Code replies (CODE_ROLES) are one complete file in a single ```python block, and
  generation stops at the closing fence. Code is never put inside JSON: small models
  writing a file into a JSON string often never close the string and run on until
  they hit the token limit.

Every line in `.hub/events.jsonl` is one of the EVENTS. To change how two parts talk,
change it here: the agents, the round controller, the status page and the tests all
read these definitions.

The file-level contracts (what each file in the project being built exports and
imports) are data, not code, and live in the project's `.hub/contracts/`; see
contracts.py.
"""

from __future__ import annotations


def _obj(required, **properties):
    return {"type": "object", "required": list(required), "properties": properties}


def _list(item):
    return {"type": "array", "items": item}


STR = {"type": "string"}
BOOL = {"type": "boolean"}

# An exact case an export must meet: a call with literal arguments, and the literal it returns.
# These are the shared truth between tester and builder: rounds.write_tests adds them to the
# tests as they are, so neither side has to guess what a vague description means.
EXAMPLE = _obj(["call", "returns"], call=STR, returns=STR)  # e.g. "tipFor(100, 15)" -> "15"

# One function, class or constant a file promises to other files.
EXPORT = _obj(
    ["name", "signature", "description", "examples"],
    name=STR,
    signature=STR,  # e.g. "def to_celsius(f: float) -> float" or "class Route"
    description=STR,
    examples=_list(EXAMPLE),  # 1 or 2 for a function; none for a class or constant
)

# What the planner proposes for one file. contracts.py adds id, version, test_path and history.
FILE_PLAN = _obj(
    ["path", "purpose", "exports", "imports"],
    path=STR,  # relative, e.g. "app/geo.py"
    purpose=STR,
    exports=_list(EXPORT),
    imports=_list(STR),  # paths of other project files this one uses
)

# One answer you can pick: a label of 2 or 3 words, and a sentence on what it means (shown on hover).
OPTION = _obj(["label", "detail"], label=STR, detail=STR)
GUESS = _obj(["text", "choice", "why", "options"], text=STR, choice=STR, why=STR, options=_list(OPTION))
DECISION = _obj(["text", "choice", "why"], text=STR, choice=STR, why=STR)

# A builder asking for something another file doesn't export yet. Additive only.
CONTRACT_REQUEST = _obj(["path", "add_exports", "why"], path=STR, add_exports=_list(EXPORT), why=STR)

# Reply schemas, one per agent role.
PLAN = _obj(
    ["name", "summary", "files", "conventions", "guesses", "complete"],
    name=STR,
    summary=STR,
    files=_list(FILE_PLAN),
    conventions=_list(STR),
    guesses=_list(GUESS),
    complete=BOOL,  # true when these files cover the whole idea and nothing more is worth adding
)
# What a builder reports about the file it just wrote, in a second, short call.
NOTES = _obj(
    ["decisions", "contract_requests", "lesson"],
    decisions=_list(DECISION),
    contract_requests=_list(CONTRACT_REQUEST),
    lesson=STR,  # one line worth remembering for later files, or ""
)
BLAMES = ("code", "test", "contract")
REVIEW = _obj(["blame", "reason"], blame={"type": "string", "enum": list(BLAMES)}, reason=STR)
REPORT = _obj(["notes"], notes=STR)


def _enum(*values):
    return {"type": "string", "enum": list(values)}


# A question for the user: always with options and the one the asker would pick, so
# pressing Enter is a real answer.
QUESTION = _obj(["text", "options", "default", "why"], text=STR, options=_list(OPTION), default=STR, why=STR)
# What the app is, agreed with the user before building. Kept in .hub/brief.json and PROJECT.md.
BRIEF = _obj(
    ["name", "pitch", "must_have", "nice_to_have", "out_of_scope", "look"],
    name=STR,
    pitch=STR,  # one or two sentences
    must_have=_list(STR),
    nice_to_have=_list(STR),
    out_of_scope=_list(STR),
    look=STR,  # how it should look and feel
)
# One brainstorm turn: the brief so far, where the brainstormer pushes back, and up to 3 questions.
BRAINSTORM = _obj(
    ["brief", "pushback", "questions", "ready"],
    brief=BRIEF,
    pushback=_list(STR),  # "I'd leave out X: ..." or "Y is harder than it sounds: ..."
    questions=_list(QUESTION),
    ready=BOOL,  # true when the brief is clear enough to start building
)
# Which part of a working page a fix should rewrite; everything else is kept as it is.
PAGE_PARTS = ("markup", "script", "styles", "whole")
PAGE_PART = _obj(["part", "why"], part=_enum(*PAGE_PARTS), why=STR)
# A test user's plan: steps on the page's real controls, each with what they expect to see after.
USER_STEPS = ("fill", "click")
USER_PLAN = _obj(
    ["goal", "steps"],
    goal=STR,
    steps=_list(_obj(["do", "target", "value", "expect"], do=_enum(*USER_STEPS), target=STR, value=STR, expect=STR)),
)
# What a test user thought after trying the app. where: "page" (the screen) or "logic" (a wrong result).
USER_PROBLEM = _obj(["what", "severity", "where"], what=STR, severity=_enum("high", "low"), where=_enum("page", "logic"))
USER_VERDICT = _obj(["works", "problems", "wish"], works=BOOL, problems=_list(USER_PROBLEM), wish=STR)

# One step of a builder agent: which action to take next. Code never goes in a step:
# "write" makes the round ask for the file in a separate code reply.
ACTIONS = {
    "read": "read a project file you may see (path)",
    "write": "write your file; the next message asks you for its full text (path = your file)",
    "test": "run your file's tests in the sandbox",
    "finish": "stop because your tests pass (note = one-sentence summary)",
    "ask": "stop and ask the user, only when the contract can't work (note = the question)",
}
STEP = _obj(
    ["action", "path", "note"],
    action={"type": "string", "enum": list(ACTIONS)},
    path=STR,
    note=STR,
)

# Data replies by call kind. "notes" runs on the builder's model, "page_part" on the
# designer's, "user_plan" and "user_verdict" on the test users', "survey" on the surveyor's.
REPLIES = {
    "brainstorm": BRAINSTORM,
    "planner": PLAN,
    "survey": FILE_PLAN,
    "step": STEP,
    "notes": NOTES,
    "page_part": PAGE_PART,
    "user_plan": USER_PLAN,
    "user_verdict": USER_VERDICT,
    "reviewer": REVIEW,
    "reporter": REPORT,
}
CODE_ROLES = ("tester", "builder", "designer")
CODE_STOP = "\n```"  # the closing fence of a code reply
# Every role, with its one job. Each picks a model in local.json.
ROLE_JOBS = {
    "brainstormer": "talks the idea through with you before anything is built: pushes back, asks up to 3 questions a turn",
    "planner": "turns the brief into files and their contracts",
    "surveyor": "reads an existing project's files and writes a contract for each",
    "tester": "writes one file's tests from its contract alone, never seeing the code",
    "builder": "owns one file and works in steps until its tests pass",
    "designer": "makes the page from the kit, and later fixes only the part of it that's wrong",
    "user": "a test user who tries the finished page and says what's wrong, knowing nothing of the code",
    "reviewer": "decides whether the code, the test or the contract is wrong when a file keeps failing",
    "reporter": "writes the round summary",
}
ROLES = tuple(ROLE_JOBS)

# Events: type -> fields every event of that type carries. Every event also has "ts" and "round".
EVENTS = {
    "round_start": ("idea",),
    "round_end": ("summary",),
    "phase": ("name",),
    "task_start": ("task", "kind", "path", "attempt"),
    "task_end": ("task", "status", "summary"),
    "llm_start": ("call", "role", "model", "label"),
    "llm_end": (
        "call", "role", "model", "label", "seconds", "prompt_tokens", "output_tokens", "tokens_per_second", "cut_off",
    ),
    "llm_error": ("call", "role", "model", "label", "error"),
    "agent_step": ("task", "step", "action", "path", "note"),
    "tests": ("target", "passed", "seconds"),
    "blocked_code": ("task", "path", "problems"),
    "contract": ("path", "version", "reason"),
    "question": ("id", "text", "blocking"),
    "note": ("text",),
    "page_fix": ("part", "reason"),
    "user_test": ("persona", "works", "problems"),
}


class ProtocolError(ValueError):
    """A message that doesn't match its schema."""


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int}


def validate(data, schema, where="reply"):
    """Check data against the small JSON-schema subset used above. Raises ProtocolError."""
    kind = schema.get("type")
    if kind and (not isinstance(data, _TYPES[kind]) or (kind == "integer" and isinstance(data, bool))):
        raise ProtocolError(f"{where} should be {kind}, got {type(data).__name__}")
    if "enum" in schema and data not in schema["enum"]:
        raise ProtocolError(f"{where} should be one of {schema['enum']}, got {data!r}")
    if kind == "object":
        for key in schema.get("required", ()):
            if key not in data:
                raise ProtocolError(f"{where} is missing {key!r}")
        for key, sub in schema.get("properties", {}).items():
            if key in data:
                validate(data[key], sub, f"{where}.{key}")
    if kind == "array":
        for i, item in enumerate(data):
            validate(item, schema["items"], f"{where}[{i}]")
    return data


def check_event(event):
    fields = EVENTS.get(event.get("type"))
    if fields is None:
        raise ProtocolError(f"unknown event type {event.get('type')!r}")
    missing = [f for f in fields if f not in event]
    if missing:
        raise ProtocolError(f"{event['type']} event is missing {', '.join(missing)}")
    return event
