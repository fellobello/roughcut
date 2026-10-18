# Questions

_Round 1. Generated from `.hub/questions/` by `hub.py render`, so don't edit it by hand. Answer in Claude Code with `/roughcut:answer`._

## Big guesses to confirm (0)

Choices that shape everything else, made so work didn't have to wait. Confirm or change each one; changing one rebuilds the parts that depend on it.

None.

## Needs your answer (0)

None.

## Smaller decisions this round (2)

Already built this way. Change any you disagree with.

### `hub.16` Where are usage readings saved?

- **Picked:** ~/.claude/roughcut/usage.json (ROUGHCUT_USAGE_FILE overrides it)
- **Why:** Outside any project, so every project's rounds read the same file.

### `hub.17` When are usage readings taken?

- **Picked:** By the build and answer commands, right before and right after each round
- **Why:** The status line only updates when your main session gets a reply, so readings taken inside a round would be stale.

## Settled (15)

- `hub.1` What runs a round? → A Claude Code dynamic workflow (workflows/round.js), one run per round
- `hub.2` What language is the hub in? → Python 3, standard library only
- `hub.3` Where does a project's state live? → JSON files in <project>/.hub/, committed with the code
- `hub.4` Who creates the worktrees? → hub.py, branching from the project's current HEAD
- `hub.5` Where do new projects go? → A new folder under the current directory (for you, ~/Desktop/CODE)
- `hub.6` Should round reports show how much usage the round took? → Yes, via the status line
- `hub.7` What's it called? → roughcut
- `hub.8` Which license should the code have? → None for now (all rights reserved)
- `hub.9` Is this the same project as your 2026-10-05 note on a design-first AI dev tool (CODE/AI-system-design-tool-idea.txt)? → They started from similar ideas and grew to overlap. Take notes and ideas from the Oct 5 note, but don't be bound by it.
- `hub.10` How do a wave's tasks get merged? → Every task in the wave finishes, then they merge together
- `hub.11` How many tries does a failing task get? → 3 (the first try plus 2 retries), then it becomes a question for you
- `hub.12` When a task is rebuilt, do the tasks that depend on it rebuild too? → No; the verify step catches whatever breaks
- `hub.13` Where are questions answered? → In chat, through /roughcut:answer. QUESTIONS.md is generated, not edited.
- `hub.14` How do tests avoid just confirming the code they test? → Each module gets a separate test task, written from the spec and interfaces only
- `hub.15` What's the default branch called? → main
