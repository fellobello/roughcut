# Roughcut Local

Roughcut's rounds, run entirely on local [Ollama](https://ollama.com) models. No Claude,
no API keys, no plan usage. It's slower and less capable than the Claude version, but it
can run all night on a laptop.

> Status: experimental, on the `ollama-local` branch.

## Quick start

Needs Python 3.9+, git, Node (for the web stack), Chrome (for the page check and test
users) and Ollama running (`ollama serve`).

```bash
ollama pull qwen3:8b && ollama pull qwen2.5-coder:7b
```

```bash
local/roughcut-local new "a pace calculator for runners"
```

That talks the idea through with you, builds it in up to 4 rounds, then opens the app in
your browser. The project goes in `~/roughcut-projects/<name>`. Come back to it later, by
folder, by name, or bring in a GitHub repo:

```bash
local/roughcut-local projects
```

```bash
local/roughcut-local open pace-calculator
```

```bash
local/roughcut-local open https://github.com/you/some-small-app
```

`open` shows the project's notes, asks what you'd like to change, talks it through, and
builds another round or two.

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

## The roles

Every role has one job and its own model in `.hub/local.json` (the list is
`ROLE_JOBS` in [`rclocal/protocol.py`](rclocal/protocol.py)):

| Role | Job |
| --- | --- |
| brainstormer | Talks the idea through with you before anything is built: pushes back, asks up to 3 questions a turn |
| planner | Turns the brief into files and their contracts |
| surveyor | Reads a project brought in and writes a contract for each file |
| tester | Writes one file's tests from its contract alone, never seeing the code |
| builder | Owns one file and works in steps until its tests pass |
| designer | Makes the page from the kit, and later fixes only the part of it that's wrong |
| user | A test user who tries the finished page, knowing nothing of the code |
| reviewer | Decides whether the code, the test or the contract is wrong when a file keeps failing |
| reporter | Writes the round summary |

## How it works

```
 your idea
     │
 brainstormer ⇄ you ──► the brief: pitch, must-haves, what's left out, look; your answers kept
     │
 every round:
   planner ──► contracts: one per file (purpose, exports, imports), + at most 3 guesses
     │
   for every file, as soon as a slot is free and its imports are built:
       tester  ──► the file's tests, from the contract alone
       builder ──► an agent: picks read / write / test / finish / ask, step by step
       reviewer ─► still failing? blame the code (retry), the test (rewrite it) or the contract (ask you)
     │
   designer (web) ──► index.html + styles.css from kit.css, checked in headless Chrome;
                      a failed check rewrites only the part it flagged
     │
   every file's tests ──► failing files go back for one fix pass
     │
   test users (web) ──► Sam, Riley and Alex try the page for real and report
     │
   reporter ──► round report; QUESTIONS.md, CONTRACTS.md and PROJECT.md rendered
```

**The brainstorm.** Before building, the brainstormer keeps a brief of the app and, each
turn, says what it would cut, simplify or add, then asks at most 3 questions. Every
question has options and the one it would pick: press Enter to take it, a letter for
another option, or type your own answer. Type `build` at "Anything to add?" to start.
`talk` does the same for changes to a project that exists. Every answer is kept as a
settled question, so the planner sees it, and the next talk starts from it.

**Questions after a round** are capped and real: at most 3 guesses per plan and 2 smaller
decisions per file, each with options. `questions --ask` goes through them one at a time:
Enter keeps the pick, a letter or your own words change it, and changing one rebuilds what
was built on it.

**Fixing the page, not redoing it.** The page is three parts: the markup (`<main>`), the
script (`<script type="module">`) and `styles.css`. When the page check fails, it says which
part is at fault (a thrown error is the script, made-up numbers in the results are the
markup) and the designer rewrites only that part; the others are kept exactly. Hidden
results and example numbers are repaired without a model at all. Sending a working page
back (`redo page-design --reason "bigger results"`, or the test users) first asks the
designer which one part the change needs, and if the change never passes the check, the
working page is put back.

**Test users.** The file tests and the page check are both checked against the planner's
contracts, so neither knows whether the app does what you asked for. The test users do:
each sees only the brief and the controls on screen, plans what they'd do with the results
they'd expect, and the steps happen for real in headless Chrome. Sam is in a hurry on a
phone-sized screen, Riley checks the numbers by hand, Alex tries empty, zero, negative and
silly input. What the browser sees for itself (a thrown error, `NaN` on screen, a control
that isn't there) counts without asking a model. Page problems send the page back (at most
`user_fixes` times per project); a result a user thinks is wrong becomes a question for you,
since a test user can be wrong too. Each round's reports and screenshots are in
`.hub/rounds/<n>.users.md` and `.hub/screens/`.

The builder becomes an agent because the round gives it five actions and a loop, and
carries out each action only if that file may do it: read its own file, its tests and the
files it imports; write only its own file. Builders can also ask another file to export
something it doesn't yet; additions are safe, so they're applied right away.

## Where things live

| Path | What |
| --- | --- |
| `PROJECT.md` | The project in one page: the brief, every file's job, what's decided, conventions, round history, how to run it. Rewritten after every talk and round. Read it first when you come back. |
| `CONTRACTS.md` | Every file's contract in full: the communication spec between files. |
| `QUESTIONS.md` | What needs you. |
| `.hub/brief.json`, `.hub/brainstorm.json` | The brief, and everything said while talking it through. |
| `.hub/contracts/*.json` | Every file's contract, with version and history. |
| `.hub/tasks/*.json`, `.hub/questions/*.json` | Tasks, decisions and questions with your answers. |
| `.hub/memory/conventions.md`, `lessons.md` | House style every agent follows, and lessons from files that took more than one try. |
| `.hub/rounds/` | Each round's report, notes and test-user report. |
| `.hub/screens/` | The page after each round, and what each test user saw. |
| `.hub/events.jsonl` | Everything that happened, one JSON line each. The status page reads it. |
| `.hub/local.json` | Models per role, parallel limits, timeouts. |
| `~/.roughcut-local/projects.json` | Every project made or opened, for `projects` and `open <name>`. |

All message formats (each role's reply schema and every event type) are in one file,
[`rclocal/protocol.py`](rclocal/protocol.py).

## Commands

| Command | What it does |
| --- | --- |
| `new "idea"` | Make a project, talk it through, build up to 4 rounds, open the app (`--no-talk`, `--rounds`, `--ui`, `--no-open`) |
| `open <folder, name or GitHub URL>` | Show the notes, talk about changes, build up to 2 rounds |
| `projects` | List every project |
| `trial --role <role>` | Try models on a dummy task for one role (below) |
| `talk` | Talk about changes to this project before the next round |
| `round --rounds N --ui` | Run rounds; stops early when a question blocks, when everything passes, or when a round makes no progress |
| `questions --ask` | Go through open questions and guesses one at a time |
| `answer <id> --text ...` / `--keep` | Answer one |
| `redo <task> --reason ...` | Send a task back |
| `app` | Serve the app and open it in your browser |
| `ui` | The status page on its own: tasks, running calls, speed per model, memory, CPU, loaded models |
| `check` | Ollama, models, sandbox, Node and Chrome |

Project commands run in the current folder, or take `--project <folder>` first.

## Trying models

```bash
local/roughcut-local trial --role designer --models qwen2.5-coder:7b,qwen2.5-coder:14b --ui
```

Each model gets the same small task for the role, run by the same code a real round uses,
and is scored against answers known to be right: the tester's tests must pass on correct
code and fail on code with a planted bug; the builder's file must pass known-good tests;
the designer's page must pass the page check; the test users must notice a planted bug
(each share is the whole total); the planner and brainstormer are checked for shape and
limits. Watch it in the terminal or on the status page (`--ui`, port 8766). Results go in
`~/roughcut-projects/.trials/<time>-<role>/results.md`, with each model's score, time,
calls and speed, plus every trial's project and screenshots. `--repeat 3` runs each
model three times, since small models vary.

## Models and limits

Defaults in `.hub/local.json`, picked for a 16 GB Apple M5:

- `qwen3:8b` brainstorms, plans, plays the test users, reviews and reports (thinking off).
- `qwen2.5-coder:7b` surveys, writes tests, code and the page. It fails native tool
  calling, but schema-held JSON steps work fine.
- Two models fit in memory together. A third makes Ollama swap them in and out.
- `max_parallel_calls` (2), `model_slots` (1 per model) and `max_parallel_tasks` (3) keep
  the machine from being asked for more than it can do at once.
- `max_tokens` caps every reply, and `call_timeout` (900 s) every call.
- `test_users` (3), `user_fixes` (2), `design_tries` (3) and `brainstorm_turns` (4) set how
  much each stage may spend.

Change a role's model with `new ... --model designer=qwen2.5-coder:14b`, or edit `local.json`.

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

Projects brought in are worked on as a copy (unless `--in-place`), cloned without
submodules, and nothing in them is installed or run except their tests, inside the box.
The page check's and test users' Chrome isn't inside `sandbox-exec`; it can only reach
127.0.0.1. Without `sandbox-exec` (not macOS), only 1, 2, 4 and 5 apply: use a VM or
container there.

## Tests

```bash
python3 -m unittest discover -s local/tests
```

They use a fake model, so they don't need Ollama. A few drive a real headless Chrome and
are skipped without it.
