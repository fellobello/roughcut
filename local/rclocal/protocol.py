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

# One function, class or constant a file promises to other files.
EXPORT = _obj(
    ["name", "signature", "description"],
    name=STR,
    signature=STR,  # e.g. "def to_celsius(f: float) -> float" or "class Route"
    description=STR,
)

# What the planner proposes for one file. contracts.py adds id, version, test_path and history.
FILE_PLAN = _obj(
    ["path", "purpose", "exports", "imports"],
    path=STR,  # relative, e.g. "app/geo.py"
    purpose=STR,
    exports=_list(EXPORT),
    imports=_list(STR),  # paths of other project files this one uses
)

GUESS = _obj(["text", "choice", "why", "options"], text=STR, choice=STR, why=STR, options=_list(STR))
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

# Data replies by call kind. "notes" runs on the builder's model.
REPLIES = {"planner": PLAN, "notes": NOTES, "reviewer": REVIEW, "reporter": REPORT}
CODE_ROLES = ("tester", "builder")
CODE_STOP = "\n```"  # the closing fence of a code reply
ROLES = ("planner", "tester", "builder", "reviewer", "reporter")  # each picks a model in local.json

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
    "tests": ("target", "passed", "seconds"),
    "contract": ("path", "version", "reason"),
    "question": ("id", "text", "blocking"),
    "note": ("text",),
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
