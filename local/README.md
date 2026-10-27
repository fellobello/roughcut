# Roughcut Local

Roughcut's rounds, run entirely on local [Ollama](https://ollama.com) models. No Claude,
no API keys, no plan usage. It's slower and less capable than the Claude version, but it
can run all night on a laptop.

> Status: experimental, on the `ollama-local` branch.

## How it differs from the Claude plugin

| | Roughcut (plugin) | Roughcut Local |
| --- | --- | --- |
| Models | Claude, through Claude Code | Any Ollama models, one per role |
| Unit of work | A module, in its own git worktree | One file, in the project folder |
| How agents reply | Tool calls in Claude Code | Data as JSON held to a schema by Ollama's `format`; code as one fenced file |
| Who writes files | Each agent | Only the round controller |
| What it makes | Anything | A designed browser app (`web`, default) or standard-library Python (`python`) |

Small models do well with one file and badly with a whole project, so every task is one
file, and every agent sees only that file's contract plus the contracts of what it imports.

## How a round works

```
 idea or answers
       │
   planner ──► contracts: one per file (purpose, exports, imports), + guesses, conventions
       │
   for every file, as soon as a slot is free and its imports are built:
       tester  ──► the file's tests, from the contract alone
       builder ──► an agent: picks read / write / test / finish / ask, step by step
       reviewer ─► still failing? blame the code (retry), the test (rewrite it) or the contract (ask you)
       │
   designer (web) ──► index.html + styles.css from kit.css, then used like a person in headless Chrome
       │
   every file's tests ──► failing files go back for one fix pass
       │
   reporter ──► round report; QUESTIONS.md and CONTRACTS.md rendered
```

The models aren't agents by themselves. The builder becomes one because the round gives it
five actions and a loop, and carries out each action only if that file may do it: read its
own file, its tests and the files it imports; write only its own file.

On the web stack, every project starts with `kit.css`, a finished design system. The
designer arranges its components; rules in `styles.css` that restyle the kit are dropped.
The page check fills in each field with sample values and fails the page on script errors,
results that never show a number, error messages for good input, or made-up numbers before
any input. Send any task back by hand with `redo <task> --reason "..."`.

Builders can ask another file to export something it doesn't yet (a contract request).
Additions are safe, so the round applies them right away, bumps that contract's version,
and rebuilds that file and its tests.

## Where things live

Everything shared is in the project's `.hub/`. Only the round controller writes it, under
the same lock Roughcut uses (`scripts/hub.py` is reused as is).

| Path | What |
| --- | --- |
| `.hub/contracts/*.json` | Every file's contract, with version and history. `CONTRACTS.md` is rendered from it. |
| `.hub/tasks/*.json`, `.hub/questions/*.json` | Tasks, decisions and questions, same as the plugin. |
| `.hub/memory/conventions.md` | House style every agent follows. Edit it between rounds. |
| `.hub/memory/lessons.md` | One-line lessons from files that took more than one try. |
| `.hub/events.jsonl` | Everything that happened, one JSON line each. The status page reads it. |
| `.hub/local.json` | Models per role, parallel limits, timeouts. |

All message formats (each agent's reply schema and every event type) are defined in one
file, [`rclocal/protocol.py`](rclocal/protocol.py).

## Run it

Needs Python 3.9+, git and Ollama running (`ollama serve`).

```bash
ollama pull qwen3:8b
ollama pull qwen2.5-coder:7b
```

```bash
local/roughcut-local --project ~/projects/tipper init --idea "A tip calculator that splits the bill"
```

```bash
local/roughcut-local --project ~/projects/tipper check
```

```bash
local/roughcut-local --project ~/projects/tipper round --ui
```

Open http://127.0.0.1:8765 to watch tasks, running model calls, speed per model, memory,
CPU and loaded models. Or run the page on its own with `ui` in another terminal.

Answer questions between rounds, then run the next one:

```bash
local/roughcut-local --project ~/projects/tipper questions
```

```bash
local/roughcut-local --project ~/projects/tipper answer hub.1 --keep
```

Overnight, run several rounds in a row. Guesses stand unless you change them; it stops
early when a question blocks work, when everything passes and the planner says the idea is
covered, or when a round makes no progress.

```bash
local/roughcut-local --project ~/projects/tipper round --rounds 6 --ui
```

## Models and limits

Defaults in `.hub/local.json`, picked for a 16 GB Apple M5:

- `qwen3:8b` plans, reviews and reports (thinking turned off, for speed).
- `qwen2.5-coder:7b` writes tests, code and the page. It fails native tool calling, but
  schema-held JSON steps work fine.
- Two models fit in memory together. A third makes Ollama swap them in and out.
- `max_parallel_calls` (2), `model_slots` (1 per model) and `max_parallel_tasks` (3) keep
  the machine from being asked for more than it can do at once.
- `max_tokens` caps every reply, and `call_timeout` (900 s) every call. Without the cap, a
  7B model writing code into a JSON string once ran for 30 minutes without closing the
  string. That's why code now comes back as a plain fenced file.

Change a role's model at init with `--model builder=llama3.1:8b`, or edit `local.json`.

## Safety: the box

Every agent gets the same safety rules in its prompt, but the rules only ask. These enforce:

1. **Guard.** Model-written code is scanned before it is written or run: no processes,
   network, deletes, environment variables, `eval` or outside packages (`stacks.py`).
2. **Allow-list.** Agents have no shell, only five actions, each checked per file.
3. **Sandbox.** Tests and the page server run under macOS `sandbox-exec`: localhost only,
   no writes outside the project and temp folders, no reads of your home folder except the
   project (`runner.py`).
4. **Limits.** CPU time, file size, open files, a timeout that kills the process group, and
   none of your environment variables.
5. **Undo.** Each project is a git repo with a commit after every phase.

The page check's Chrome isn't inside `sandbox-exec`; it can only reach 127.0.0.1. Without
`sandbox-exec` (not macOS), only 1, 2, 4 and 5 apply: use a VM or container there.

## Tests

```bash
python3 -m unittest discover -s local/tests
```

They use a fake model, so they don't need Ollama.
