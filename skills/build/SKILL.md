---
name: build
description: Start building a software idea with idea-builder. Creates a project folder, then runs round 0, where agents plan the idea and build as much as they can without asking anything, and hand back a short list of questions. Use when the user runs /idea-builder:build or asks idea-builder to build an idea.
argument-hint: "<the idea, or a path to a file holding it>"
---

# Build an idea

The idea: $ARGUMENTS

1. If the idea is empty, ask for it in one line and stop. If it's a path to a file, read the file and use its text.
2. Name the project: a short kebab-case folder name from the idea, two to four words. It goes in a new folder under the current working directory, unless the user named another place. If that folder already exists and isn't empty, pick another name. Tell the user the full path in one line.
3. Create the project's hub, passing the idea exactly as given:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hub.py" --project "<folder>" init --idea-file - <<'IDEA'
   <the idea>
   IDEA
   ```

4. Run round 0 by calling the Workflow tool with the name `idea-builder:round` and these args, as a JSON object (not a string):
   `{"project": "<absolute folder path>", "pluginRoot": "${CLAUDE_PLUGIN_ROOT}", "round": 0}`
   The round runs in the background. Wait for it to finish.
5. When it finishes, tell the user, briefly:
   - what got built, from the round report;
   - the big guesses to confirm and any questions that block work, from `QUESTIONS.md` in the project;
   - that `/idea-builder:answer` goes through them and starts the next round.

   `QUESTIONS.md` and `.hub/rounds/0.md` hold the detail, so link them rather than repeating them.
