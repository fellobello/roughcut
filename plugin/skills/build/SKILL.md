---
name: build
description: Start building a software idea with roughcut. Creates a project folder, then runs round 0, where agents plan the idea and build as much as they can without asking anything, and hand back a short list of questions. Use when the user runs /roughcut:build or asks roughcut to build an idea.
argument-hint: "<the idea, or a path to a file holding it>"
---

# Build an idea

The idea: $ARGUMENTS

1. Check that you have the Workflow tool. If it isn't listed, search for it with ToolSearch (`select:Workflow`). If there's no Workflow tool, stop before creating anything and tell the user: roughcut needs dynamic workflows, which some plans have off by default, and adding `"enableWorkflows": true` to `~/.claude/settings.json` (or turning on Dynamic workflows in `/config`) turns them on for new sessions.
   If the idea is empty, ask for it in one line and stop. If it's a path to a file, read the file and use its text.
2. Name the project: a short kebab-case folder name from the idea, two to four words. It goes in a new folder under the current working directory, unless the user named another place. If that folder already exists and has a `.hub/` but no `.hub/rounds/0.md`, round 0 never finished there: reuse the folder, skip step 3, and run round 0 again (the planner picks up where it stopped). Otherwise, if the folder isn't empty, pick another name. Tell the user the full path in one line.
3. Create the project's hub, passing the idea exactly as given:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hub.py" --project "<folder>" init --idea-file - <<'IDEA'
   <the idea>
   IDEA
   ```

4. Note the usage at the start: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hub.py" --project "<folder>" usage --mark start`. If it says there are no readings, carry on without them.
   Then run round 0 by calling the Workflow tool with the name `roughcut:round` and these args, as a JSON object (not a string):
   `{"project": "<absolute folder path>", "pluginRoot": "${CLAUDE_PLUGIN_ROOT}", "round": 0}`
   The round runs in the background. Wait for it to finish.
   If the result has `stopped`, the round ended early, most often at a usage limit. Tell the user what stopped and that finished work is kept, then stop here: in this same session the run can be resumed with the Workflow tool's `resumeFromRunId`, and from any session `/roughcut:answer` runs the round again.
5. When it finishes, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hub.py" --project "<folder>" usage --mark end`, which adds the round's usage to its report when readings exist. Then tell the user, briefly:
   - what got built, and how much usage it took if the report has a Usage section;
   - the big guesses to confirm and any questions that block work, from `QUESTIONS.md` in the project;
   - that `/roughcut:answer` goes through them and starts the next round.

   `QUESTIONS.md` and `.hub/rounds/0.md` hold the detail, so link them rather than repeating them.
