# Plan

## Goal

Submit a software idea and get back as much of a working sample as can be built without guessing at anything important. Then answer a few questions, and repeat until nothing is left to ask. Built first as a Claude Code plugin for its author and other Claude Code users. A standalone version, on the Claude Agent SDK, comes later, once the plugin works.

## The loop

1. **Submit.** `/roughcut:build <idea>` creates a project folder and its `.hub/`.
2. **Round 0.** Plans the idea and builds without asking anything. Big choices get a default and are logged as guesses.
3. **Questions.** `QUESTIONS.md` lists, in order: big guesses to confirm, questions that block work, and the round's smaller decisions. Answering them is the one approval step.
4. **Next round.** `/roughcut:answer` records the answers and runs another round, which builds on them.
5. **Repeat** until no questions are left and the sample is complete. The user can stop after any round, which is how they control time and usage.

## Pieces

| Piece | File | Job |
| --- | --- | --- |
| Orchestrator | `workflows/round.js` | Runs one round: plan → waves of parallel tasks → merge → verify → questions. Holds no state. |
| Hub | `scripts/hub.py` | The only writer of `.hub/`. Tasks, the dependency graph, worktrees, questions, decisions, reports. |
| Planner | `agents/planner.md` | Idea → spec, interface code, tasks. Later rounds: answers → updated spec and new tasks. |
| Builder | `agents/builder.md` | Builds one module in its own worktree against the interfaces. |
| Tester | `agents/tester.md` | Writes acceptance tests from the spec alone; runs the full suite after merges. |
| Synthesizer | `agents/synthesizer.md` | Merges a wave's branches, resolves conflicts, cleans up worktrees. |
| Questioner | `agents/questioner.md` | Dedupes questions, writes `QUESTIONS.md` and the round report. |

## Interfaces between the pieces

### The hub CLI

Every agent changes state only through `python3 scripts/hub.py --project <folder> <command>`. Every command prints JSON; errors go to stderr with exit status 1.

| Command | Used by | Effect |
| --- | --- | --- |
| `init --idea-file -` | build skill | Creates `.hub/`, and a git repo if the folder isn't its own repo |
| `add-task <id> --title --kind build\|test --depends a,b` | planner | Adds a task |
| `check` | planner, `start-wave` | Missing dependencies and cycles |
| `start-wave` | orchestrator | Starts every ready task on its own branch and worktree off the current `HEAD` |
| `finish <id> --status done\|blocked\|failed` | builder, tester, synthesizer | Ends a task's attempt |
| `ask --task <id> --text [--critical] [--choice] --why --option --affects` | everyone | Logs a decision (has `--choice`) or asks a blocking question (critical, no choice) |
| `answer <id> --text \| --keep` | answer skill | Settles a question; changing a decision sends the done tasks built on it back to `todo` |
| `reopen <id> --reason` | tester, synthesizer | Sends a done task back; after 3 attempts it becomes a question for the user |
| `cleanup <id>` | synthesizer | Removes the worktree, and the branch once merged |
| `dismiss <id> --reason` | questioner | Closes a duplicate question |
| `render`, `report --notes` | questioner | Writes `QUESTIONS.md` and `.hub/rounds/<n>.md` |
| `usage --mark start\|end` | build and answer skills | Saves the status line's latest usage reading; the report shows the difference |
| `status`, `tasks`, `task`, `questions`, `ready` | anyone | Read-only |
| `next-round` | answer skill | Moves to the next round |

### Task states

```
todo ──start-wave──► in_progress ──finish──► done ──reopen──► todo
  ▲                       │                    │
  │                       ├──► blocked ──answer──► todo
  │                       └──► failed
  └──── answer that changes a decision a done task was built on
```

A task is ready when it's `todo`, nothing blocks it, and every task it depends on is `done`.

### Questions and decisions

| Kind | How it's made | Blocks work? | Shown under |
| --- | --- | --- | --- |
| Big guess | `ask --critical --choice` | No | Big guesses to confirm |
| Blocking question | `ask --critical`, no choice | Yes: its task and `--affects` | Needs your answer |
| Smaller decision | `ask --choice` | No | Smaller decisions this round |

Question ids are `<asking task>.<n>`, or `hub.<n>` for the planner and questioner.

### What agents return to the orchestrator

Each agent returns structured output, checked against a JSON schema in `round.js`: the planner returns how many tasks are in `todo`; builders and test writers return their task, status and summary; the synthesizer returns what it merged; the verifier returns whether everything passed and what it reopened; the questioner returns the round's summary. The orchestrator uses these only to decide what runs next. The real state is always in `.hub/`.

## Next

1. **First real run** on a small idea (the Flock camera map), to find where the prompts and the loop break.
2. **Merge tasks as they finish** instead of a barrier per wave, if waves leave agents idle.
3. **Standalone version** on the Claude Agent SDK, reusing `hub.py` and the role prompts unchanged.
