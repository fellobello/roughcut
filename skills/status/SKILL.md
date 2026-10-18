---
name: status
description: Show where a roughcut project stands: the round, what's built, what's blocked, and what needs the user. Use when the user runs /roughcut:status or asks how a roughcut project is going.
argument-hint: "[project folder]"
---

# Project status

Project folder: $ARGUMENTS. If that's empty, use the current working directory. If the folder has no `.hub/`, ask which project.

Run:

    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hub.py" --project "<project folder>" status

Then tell the user in a few lines: the round, how many tasks are done, blocked or ready, how many questions need them, and the path to `QUESTIONS.md`. If questions are waiting, mention `/roughcut:answer`. Change nothing.
