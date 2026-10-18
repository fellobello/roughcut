# Roughcut

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
claude --plugin-dir /path/to/roughcut
```

Or install it from GitHub, once it's published:

```bash
claude plugin marketplace add fellobello/roughcut
```

```bash
claude plugin install roughcut@roughcut
```

## Use

| Command | What it does |
| --- | --- |
| `/roughcut:build <idea>` | Makes a project folder in the current directory and runs round 0 |
| `/roughcut:answer [folder]` | Goes through the questions with you, then runs the next round |
| `/roughcut:status [folder]` | Shows the round, what's built, and what needs you |

Each round counts toward your Claude usage, and a round can use a lot: the first real one, a 12-task map app, took about 3 million tokens. The workflow asks before it starts, you can stop it from `/workflows`, and it runs at most 4 tasks at a time. If a round stops early, for example at a usage limit, finished work is kept and the next `/roughcut:answer` picks up where it stopped.

### Usage numbers in reports (optional)

Claude Code shows your 5-hour and weekly usage only to the status line, so roughcut reads them from there. To have each round's report say how much usage the round took, point your status line at the script in this repo, in `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /path/to/roughcut/scripts/usage_statusline.py"
  }
}
```

It shows the model and both percentages, and saves them to `~/.claude/roughcut/usage.json`. The numbers only appear on Pro and Max plans.

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
| `scripts/usage_statusline.py` | Optional status line that saves usage numbers for the reports |
| `agents/` | The five roles: planner, builder, tester, synthesizer, questioner |
| `skills/` | The three commands |
| `tests/` | `python3 -m unittest discover -s tests` for the scripts, `node --test tests/round.test.mjs` for the workflow |
| `docs/PLAN.md` | The design, and what's planned next |

Built with Claude Code.
