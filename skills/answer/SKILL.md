---
name: answer
description: Go through a roughcut project's questions with the user, record the answers, and start the next round. Use when the user runs /roughcut:answer, answers questions from a roughcut QUESTIONS.md, or wants a roughcut project to keep going.
argument-hint: "[project folder]"
---

# Answer questions and run the next round

Project folder: $ARGUMENTS. If that's empty, use the current working directory. If the folder has no `.hub/`, ask which project.

The hub command, called HUB below, is:

    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hub.py" --project "<project folder>"

1. Run `HUB status` and `HUB questions`.
2. Go through them in this order:
   1. big guesses to confirm (critical, status `decided`),
   2. questions that block work (status `open`),
   3. then ask once whether the user wants to review this round's smaller decisions.

   Use AskUserQuestion when a question has options, up to four questions per call. For a guess, put the current pick first. If the user already answered something in their message, use that instead of asking again.
3. Record each answer:
   - Confirm a guess or decision as it is: `HUB answer <id> --keep`
   - Change a guess or decision: `HUB answer <id> --text "<new choice>"`. The tasks built on it get rebuilt next round.
   - Answer a question: `HUB answer <id> --text "<answer>"`
   - Skipped questions stay open for later.
4. Check that you have the Workflow tool, the same way `/roughcut:build` does, and stop with the same explanation if it's missing. Then run `HUB next-round`, `HUB status` and `HUB usage --mark start` (no readings is fine). Say in one line how many tasks are ready, then start the round by calling the Workflow tool with the name `roughcut:round` and these args, as a JSON object (not a string):
   `{"project": "<absolute folder path>", "pluginRoot": "${CLAUDE_PLUGIN_ROOT}", "round": <the new round number>}`
   The planner adds new work from the answers, so start the round even when no tasks are ready yet.
5. When it finishes, run `HUB usage --mark end`, then report the same way `/roughcut:build` does: what got built, what needs the user, and where the detail is.

If nothing is left to answer and the last round's report says the sample is complete, say so and don't start a round: the project is done for now.
