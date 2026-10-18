# Questions

_Round 0. Generated from `.hub/questions/` by `hub.py render`, so don't edit it by hand. Answer in Claude Code with `/idea-builder:answer`._

## Big guesses to confirm (7)

Choices that shape everything else, made so work didn't have to wait. Confirm or change each one; changing one rebuilds the parts that depend on it.

### `hub.1` What runs a round?

- **Picked:** A Claude Code dynamic workflow (workflows/round.js), one run per round
- **Why:** It runs agents in parallel, survives pauses and usage limits, and keeps the orchestration in code. It can't take input mid-run, which fits rounds.
- **Other options:** The build skill drives subagents turn by turn · Agent teams

### `hub.2` What language is the hub in?

- **Picked:** Python 3, standard library only
- **Why:** Nothing to install, easy to test, and already on most developers' machines.
- **Other options:** Node.js · No script: agents edit the JSON files directly

### `hub.3` Where does a project's state live?

- **Picked:** JSON files in <project>/.hub/, committed with the code
- **Why:** Agents trade files instead of waiting on each other, git history shows every decision, and the standalone version can reuse it as is.
- **Other options:** SQLite · Outside the repo

### `hub.4` Who creates the worktrees?

- **Picked:** hub.py, branching from the project's current HEAD
- **Why:** Claude Code's own worktree option branches from the remote default branch, so once a project has a remote, wave 2 wouldn't see wave 1's merged code.
- **Other options:** Claude Code's isolation: 'worktree' plus the worktree.baseRef = head setting

### `hub.5` Where do new projects go?

- **Picked:** A new folder under the current directory (for you, ~/Desktop/CODE)
- **Why:** No setup, and you choose the place by where you start Claude.
- **Other options:** One fixed folder, set in the plugin's settings

### `hub.6` Should round reports show how much usage the round took?

- **Picked:** Not yet
- **Why:** Claude Code gives the 5-hour and weekly usage percentages only to the status line, and a plugin can't set your status line. It would take a line added to your status line settings that saves the numbers to a file.
- **Other options:** Yes: add the status line piece (changes your Claude Code settings)

### `hub.7` What's it called?

- **Picked:** idea-builder (a placeholder)
- **Why:** Plain and searchable until you pick a real name.

## Needs your answer (2)

### `hub.8` Which license should the code have?

- **Why:** Other developers can't legally reuse it without one.
- **Other options:** MIT · Apache-2.0 · None for now (all rights reserved)

### `hub.9` Is this the same project as your 2026-10-05 note on a design-first AI dev tool (CODE/AI-system-design-tool-idea.txt)?

- **Why:** That note wants a structured spec, decisions with reasons, and links from code back to requirements. This tool already has the first two. The big difference: it builds before asking.
- **Other options:** Same project: fold that note's ideas in · Separate projects

## Smaller decisions this round (6)

Already built this way. Change any you disagree with.

### `hub.10` How do a wave's tasks get merged?

- **Picked:** Every task in the wave finishes, then they merge together
- **Why:** Simpler merges. Costs some idle time when one task is slow.
- **Other options:** Merge each task as soon as it finishes

### `hub.11` How many tries does a failing task get?

- **Picked:** 3 (the first try plus 2 retries), then it becomes a question for you
- **Why:** Enough to fix a real mistake without looping on a bad spec.

### `hub.12` When a task is rebuilt, do the tasks that depend on it rebuild too?

- **Picked:** No; the verify step catches whatever breaks
- **Why:** Rebuilding everything downstream would cost a lot for changes that usually don't matter.

### `hub.13` Where are questions answered?

- **Picked:** In chat, through /idea-builder:answer. QUESTIONS.md is generated, not edited.
- **Why:** One place to answer, and hub.py stays the only writer of the state.

### `hub.14` How do tests avoid just confirming the code they test?

- **Picked:** Each module gets a separate test task, written from the spec and interfaces only
- **Why:** An agent testing its own code tends to test what it built rather than what was asked for.

### `hub.15` What's the default branch called?

- **Picked:** main
- **Why:** The current convention.
