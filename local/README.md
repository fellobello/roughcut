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
| How agents reply | Tool calls in Claude Code | JSON held to a schema by Ollama's `format` |
| Who writes files | Each agent | Only the round controller |
| Language of the sample | Any | Python, standard library only (for now) |

Small models do well with one file and badly with a whole project, so every task is one
file, and every agent sees only that file's contract plus the contracts of what it imports.

## How a round works

```
 idea or answers
       │
   planner ──► contracts: one per file (purpose, exports, imports), + guesses, conventions
       │
   for every file, as soon as a slot is free and its imports are built:
       tester  ──► tests/test_<file>.py, from the contract alone
       builder ──► the file; run its tests; on failure, try again with the error (fix_tries)
       reviewer ─► still failing? blame the code (retry), the test (rewrite it) or the contract (ask you)
       │
   full test run ──► failing files go back for one fix pass
       │
   reporter ──► round report; QUESTIONS.md and CONTRACTS.md rendered
```

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
local/roughcut-local --project ~/projects/converter init --idea "A command-line unit converter"
```

```bash
local/roughcut-local --project ~/projects/converter check
```

```bash
local/roughcut-local --project ~/projects/converter round --ui
```

Open http://127.0.0.1:8765 to watch tasks, running model calls, speed per model, memory,
CPU and loaded models. Or run the page on its own with `ui` in another terminal.

Answer questions between rounds, then run the next one:

```bash
local/roughcut-local --project ~/projects/converter questions
```

```bash
local/roughcut-local --project ~/projects/converter answer hub.1 --keep
```

Overnight, run several rounds in a row. Guesses stand unless you change them; it stops
early when a question blocks work, when everything passes and the planner says the idea is
covered, or when a round makes no progress.

```bash
local/roughcut-local --project ~/projects/converter round --rounds 6 --ui
```

## Models and limits

Defaults in `.hub/local.json`, picked for a 16 GB Apple M5:

- `qwen3:8b` plans, reviews and reports (thinking turned off, for speed).
- `qwen2.5-coder:7b` writes tests and code. It fails native tool calling, but schema-held
  JSON works fine.
- Two models fit in memory together. A third makes Ollama swap them in and out.
- `max_parallel_calls` (2), `model_slots` (1 per model) and `max_parallel_tasks` (3) keep
  the machine from being asked for more than it can do at once.

Change a role's model at init with `--model builder=llama3.1:8b`, or edit `local.json`.

## Safety

Models write code, and the tests run it. Tests run with a time limit, and on macOS inside
`sandbox-exec` with no network and no writes outside the project and temp folders. On other
systems they run unsandboxed: use a VM or container there.

## Tests

```bash
python3 -m unittest discover -s local/tests
```

They use a fake model, so they don't need Ollama.
