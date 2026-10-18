# Idea Builder

A Claude Code plugin that turns a software idea into a working sample, a round at a time.

You give it an idea in a sentence or two, the way you'd write it in a notebook. Agents plan it, build the modules in parallel, merge and test them, and then stop with a short list of questions. You answer, and the next round builds on your answers. You decide when it's done.

> Status: round 0. The pieces are built and unit-tested, but the full loop hasn't run on a real idea yet.

## How a round works

```
 idea or answers
       │
   ┌───▼────┐   spec, interface code, task graph
   │ planner│──────────────────────────────┐
   └───┬────┘                              │
       │                              .hub/ (state)
   ┌───▼────────────────────────┐          │
   │ wave: every unblocked task │◄─────────┤  hub.py is the only writer
   │ builder  builder  tester   │  in its own git worktree each
   └───┬────────────────────────┘          │
   ┌───▼────────┐                          │
   │ synthesizer│ merges the wave ─────────┤
   └───┬────────┘  (repeat until nothing is ready)
   ┌───▼────┐                              │
   │ tester │ runs every test, sends failures back
   └───┬────┘                              │
   ┌───▼──────┐                            │
   │questioner│ writes QUESTIONS.md ───────┘
   └──────────┘
```

- **Round 0 never asks you anything.** Big choices (language, storage, outside services) get a sensible default and go at the top of `QUESTIONS.md` as guesses to confirm.
- **Interfaces come first.** The planner writes them as real code before any builder starts, so modules can be built at the same time without guessing about each other.
- **Tests come from the spec, not the code.** Each module gets a separate test task, written by an agent that never reads that module's implementation.
- **Agents don't wait on each other.** They talk through files in `.hub/`, written only by `scripts/hub.py`, which locks every change.
- **Changing an answer rebuilds only what depends on it.**
- **Code only.** Images, icons and other assets are placeholders.

## Install

Needs Claude Code with [dynamic workflows](https://code.claude.com/docs/en/workflows) (on a Pro plan, turn them on in `/config`), `git`, and Python 3.9 or later.

Try it from a local copy:

```bash
claude --plugin-dir /path/to/idea-builder
```

Or install it from GitHub, once it's published:

```bash
claude plugin marketplace add fellobello/idea-builder
```

```bash
claude plugin install idea-builder@idea-builder
```

## Use

| Command | What it does |
| --- | --- |
| `/idea-builder:build <idea>` | Makes a project folder in the current directory and runs round 0 |
| `/idea-builder:answer [folder]` | Goes through the questions with you, then runs the next round |
| `/idea-builder:status [folder]` | Shows the round, what's built, and what needs you |

Each round counts toward your Claude usage. The workflow asks before it starts, and you can stop it from `/workflows`.

## What a project looks like

```
my-idea/
├── QUESTIONS.md          what needs you, generated each round
├── .hub/
│   ├── idea.md           the idea as you wrote it
│   ├── spec.md           the plan: modules, interfaces, commands
│   ├── tasks/*.json      one file per task
│   ├── questions/*.json  one file per question or decision
│   └── rounds/<n>.md     what each round did
└── ...the code
```

`.hub/` is committed with the code, so git history shows every decision and when it changed.

## Layout of this repo

| Path | What it is |
| --- | --- |
| `workflows/round.js` | The orchestrator: one round, as a Claude Code workflow |
| `scripts/hub.py` | The state store every agent goes through (standard library only) |
| `agents/` | The five roles: planner, builder, tester, synthesizer, questioner |
| `skills/` | The three commands |
| `tests/` | Unit tests for `hub.py`: `python3 -m unittest discover -s tests` |
| `docs/PLAN.md` | The design, and what's planned next |

Built with Claude Code.
