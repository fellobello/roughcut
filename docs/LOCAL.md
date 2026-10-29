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
./roughcut-local new "a pace calculator for runners"
```

That talks the idea through with you, builds it in up to 4 rounds, then opens the app in
your browser.

Or do all of it from one page in your browser:

```bash
./roughcut-local studio
```

The studio (http://127.0.0.1:8770) has your projects on the left (open one, start a new
one, or bring in existing code from a folder or GitHub repo), the project's files below that, and five tabs:

- **Talk**: chat with the brainstormer. Its questions come up one at a time in their own box,
  each answer 2 or 3 words, with what it means when you hover over it; ★ marks its pick.
  Type your own answer instead, or press Keep talking to take its picks. Build starts the rounds.
  Once it has built, the guesses it made and the files it left for you come up in the chat
  too, one at a time: the swarm asks on the left, and you answer in the box on the right.
  Answered one wrong? **Change** under your answer brings its options back; changing an
  answer rebuilds whatever was built on it. The message box stays on the bottom edge of
  the window, and **Stop** replaces Build while something runs.
- **Editor**: open any file from **Files** (the tree under the folder picker; files left for
  you have an orange dot). It shows the language, line numbers and whether it's saved; ⌘S
  saves. Beside the code: what the file must do (its contract and examples), and **Run its
  tests**, which runs them in the box and shows pass or fail. Only files inside the project
  can be opened or saved, and `.hub/` and `.git/` are read only.
- **Progress**: the round as it runs. **Working now** shows what each model is writing as it
  writes it (and its thinking, for a model with thinking on), and **Just finished** keeps the
  last few replies to open. Then tasks, speed and what happened. **Stop now** cuts off the
  call that's running and puts the files the round was on back to todo; **Stop after this
  round** lets it finish first.
- **App**: the app running inside the page, and every screenshot from the rounds and test users.
- **Models**: whether Ollama answers, what's installed, and a model per role (changes are
  used from the next call). Performance settings will go here later.

The projects column (left) and the cycle column (right) open and close with the icons at
the top corners. The cycle lists the stages of a round (talk, plan, build files, design the
page, check everything, test users, report) and lights up the one running.

**Your answers are rules.** Every answer you've given (in the brainstorm or to a guess) goes
to the planner, tester, builder and reviewer in every round, not just the round after you
gave it, so a rule like "shares are whole dollars" can't get lost. Tests check values and
behaviour, never the exact wording of a message or label the contract didn't spell out. A
contract's examples are checked one by one: any that aren't real values are left out, and a
plan whose examples are mostly junk is asked for again. When a contract can't work (it
contradicts you, or a function is missing an input), the next plan revises it, and the file
gets a fresh set of tries.

**Blame goes by the facts first.** When a file keeps failing, who is wrong is settled
without a model where the facts decide it: tests that import a name the contract doesn't
have, or expect something else than the contract's own example for the same call, are
wrong; code that fails the contract's examples is wrong; code that meets every example
while the tester's extra tests fail means the tests asked for more than the contract
promises; a contract that gives two results for one call is wrong. Only what's left goes to
the reviewer, which may blame the contract only by quoting the words that can't work.

**You check the examples.** Small models get arithmetic wrong, and one wrong example fails a
file that's right. So every new example a plan writes comes up in Talk as "Is this right?
splitBill(99, 2) returns [49, 50]", taken as right until you answer. **Wrong** drops it from
the contract and the tests, or type what it should return. Tests lean on what must hold
(shares add up to the total) more than on hand-worked numbers, and the reviewer is shown
the actual and expected values, not node's stack frames.

**Finished files stay finished.** A plan can't change the contract of a file that's done
unless something you've said since is about it (or you asked for changes in a talk). A file
sent back by a change elsewhere is tested first, and kept if it still passes. For a file left
for you, **It works** counts it as done as it is: the tests it fails don't hold anything back.

**Nothing stops to ask you.** Like the Claude version, a run guesses and keeps going.

**A file the builder keeps getting wrong is split.** After two failed attempts where the
reviewer blames the code (or the same tests failing the same way twice), the planner breaks
the file into 2 to 4 smaller files in a folder next to it (`app/board_parts/neighbors.js`),
each with its own contract, examples, tests and builder. The file keeps its contract and its
tests and is rebuilt as a thin file that imports the pieces, so nothing that uses it changes.
A file is split only once, and a piece is never split again.

When the models still can't get a file to pass, it's **left for you**: the file keeps its best attempt,
with a note at the top saying what it must do (its contract and examples), how it's tested
and why they got stuck, and everything that doesn't need it is built anyway. Finish it in
any editor; once its tests pass, the next round marks it done. Or answer its guess with
"Try again" or "Simplify it", and the next round acts on that. You get one notification when
the run ends, saying what was built, what's left for you and how many guesses to look at.
While rounds run, the Mac is kept awake (`caffeinate`); keep it plugged in, since closing
the lid still puts it to sleep.

The page talks only to the desk (`rclocal/desk.py`), which talks to the hub. The desk
knows what the hub doesn't: which project is open, where the chat is, and what's running.
It runs one job at a time in the background (a brainstorm turn, rounds, reading a project
brought in), so two rounds never touch the same hub. The server listens on 127.0.0.1 only,
and every change needs a token that's only in the page it serves, so other websites can't
drive it. The project goes in `~/roughcut-projects/<name>`. Come back to it later, by
folder, by name, or bring in a GitHub repo:

```bash
./roughcut-local projects
```

```bash
./roughcut-local open pace-calculator
```

```bash
./roughcut-local open https://github.com/you/some-small-app
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
`ROLE_JOBS` in [`rclocal/protocol.py`](../rclocal/protocol.py)):

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
       reviewer ─► still failing? blame the code (retry), the test (rewrite it) or the contract (replan),
                   from the facts where they decide it, else the reviewer model
     │
   designer (web) ──► first, as soon as there's a plan: index.html + styles.css from kit.css,
                      on placeholder files that return their contracts' examples, so the
                      page loads and can be tried while the logic is built. Checked in
                      headless Chrome (only that it loads, until every file is built; then
                      in full). A failed check rewrites only the part it flagged
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
[`rclocal/protocol.py`](../rclocal/protocol.py).

## Commands

| Command | What it does |
| --- | --- |
| `new "idea"` | Make a project, talk it through, build up to 4 rounds, open the app (`--no-talk`, `--rounds`, `--ui`, `--no-open`) |
| `open <folder, name or GitHub URL>` | Show the notes, talk about changes, build up to 2 rounds |
| `projects` | List every project |
| `trial --role <role>` | Try models on a dummy task for one role (below) |
| `talk` | Talk about changes to this project before the next round |
| `round --rounds N --ui` | Run rounds; stops early when everything passes, or when the next round would have nothing to do (what's left waits on a file left for you) |
| `questions --ask` | Go through open questions and guesses one at a time |
| `answer <id> --text ...` / `--keep` | Answer one |
| `redo <task> --reason ...` | Send a task back |
| `app` | Serve the app and open it in your browser |
| `ui` | The status page on its own: tasks, running calls, speed per model, memory, CPU, loaded models |
| `check` | Ollama, models, sandbox, Node and Chrome |

Project commands run in the current folder, or take `--project <folder>` first.

## Trying models

```bash
./roughcut-local trial --role designer --models qwen2.5-coder:7b,qwen2.5-coder:14b --ui
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
python3 -m unittest discover -s tests
```

They use a fake model, so they don't need Ollama. A few drive a real headless Chrome and
are skipped without it.
