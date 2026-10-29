# Roughcut

An LLM harness to transform ideas into testable products with Ollama or Claude using an async swarm of agents.

You give it an idea in a sentence or two, the way you'd write it in a notebook. Agents plan it, build the pieces in parallel, test them, and then stop with a short list of questions. You answer, and the next round builds on your answers. You decide when it's done.

There are two ways to run it:

| | [Roughcut Local](docs/LOCAL.md) (Ollama) | [Roughcut for Claude Code](plugin/README.md) (plugin) |
| --- | --- | --- |
| Models | Local [Ollama](https://ollama.com) models only: no API keys, no plan usage | Claude |
| Best for | Small browser apps and Python tools | Complex projects |
| How you use it | The studio, a page in your browser, or the `roughcut-local` command | `/roughcut:build`, `/roughcut:answer` in Claude Code |
| Speed | Slower (the demo bill splitter took about 25 minutes on a 16 GB Mac), free to run | Fast, but uses your Claude token allowance |

## Example

[alpr-route-planner](https://github.com/fellobello/alpr-route-planner) came from one line: "google maps but you can see flock cameras and plan routes around them." Roughcut's first round planned it as 6 modules, built them with acceptance tests in parallel, and merged them into a working web app. Its `.hub/` folder holds the whole plan, every task and every decision.

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

- **Interfaces come first.** The planner writes them as real code before any builder starts, so modules can be built at the same time without guessing about each other.
- **Tests come from the spec, not the code.** Each module gets a separate test task, written by an agent that never reads that module's implementation.
- **Agents don't wait on each other.** They talk through files in `.hub/`, written only by `plugin/scripts/hub.py`, which locks every change.
- **Code only.** Images, icons and other assets are built as placeholders.

## Roughcut Local in two minutes

Needs Python 3.9 or later, git, Node, Google Chrome, and [Ollama](https://ollama.com) running.

```bash
ollama pull qwen3:8b && ollama pull qwen2.5-coder:7b
```

```bash
./roughcut-local studio
```

That opens the studio at localhost:8770. Start a new project from an idea, talk it through with the brainstormer (it pushes back and asks a few questions, each with options), press Build, and watch the rounds. Answer what the models asked in the Questions tab, try the app in the App tab, and monitor or pick a model per role in the Models tab.

[docs/LOCAL.md](docs/LOCAL.md) has everything else: every command, the roles, where things live, and the safety box in detail.

## Install the plugin

Needs Claude Code with [dynamic workflows](https://code.claude.com/docs/en/workflows) (on a Pro plan, turn them on in `/config`), `git`, and Python 3.9 or later.

Try it from a local copy:

```bash
claude --plugin-dir /path/to/roughcut/plugin
```

Or install it from GitHub:

```bash
claude plugin marketplace add fellobello/roughcut
```

```bash
claude plugin install roughcut@roughcut
```

## Use the plugin

| Command | What it does |
| --- | --- |
| `/roughcut:build <idea>` | Makes a project folder in the current directory and runs round 0 |
| `/roughcut:answer [folder]` | Goes through the questions with you, then runs the next round |
| `/roughcut:status [folder]` | Shows the round, what's built, and what needs you |

Each round counts toward your Claude usage, and a round can use a lot: the first real one, a 12-task map app, took about 3 million tokens. There is room for lots of improvement here. The workflow asks before it starts, you can stop it from `/workflows`, and it runs at most 4 tasks at a time, to make management doable as code comes in. If a round stops early, for example at a usage limit, finished work is kept and the next `/roughcut:answer` picks up where it stopped.

### Usage numbers in reports (optional)

Claude Code shows your 5-hour and weekly usage only to the status line, so roughcut reads them from there. To have each round's report say how much usage the round took, point your status line at the script in this repo, in `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /path/to/roughcut/plugin/scripts/usage_statusline.py"
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
| `roughcut-local` | The command for Roughcut Local: `./roughcut-local studio`, `new`, `open`, `round`, `trial` |
| `rclocal/` | Roughcut Local itself: the agents, the rounds, the safety box, the studio |
| `tests/` | Roughcut Local's tests: `python3 -m unittest discover -s tests` (a fake model, so no Ollama needed) |
| `docs/LOCAL.md` | Everything about Roughcut Local: every command, the roles, where things live, the safety box |
| `plugin/` | The Claude Code plugin: agents, commands, the round workflow and `scripts/hub.py`, the state store both versions share ([README](plugin/README.md)) |
| `plugin/tests/` | The plugin's tests: `python3 -m unittest discover -s plugin/tests` and `node --test plugin/tests/round.test.mjs` |
| `.claude-plugin/marketplace.json` | Lets Claude Code install the plugin from this repo |
| `.hub/`, `QUESTIONS.md` | Roughcut's own rounds: the plugin was built with itself |

Built with Claude Code.
