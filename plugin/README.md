# Roughcut for Claude Code

The Claude Code plugin version of Roughcut: Claude agents plan an idea, build its modules in
parallel in git worktrees, merge and test them, and hand you a short list of questions after
each round. Install and use it as described in the [main README](../README.md#install-the-plugin).

| Path | What it is |
| --- | --- |
| `.claude-plugin/plugin.json` | The plugin's manifest |
| `workflows/round.js` | The orchestrator: one round, as a Claude Code workflow |
| `scripts/hub.py` | The state store every agent goes through (standard library only); Roughcut Local uses it too |
| `scripts/usage_statusline.py` | Optional status line that saves usage numbers for the reports |
| `agents/` | The five roles: planner, builder, tester, synthesizer, questioner |
| `skills/` | The three commands: build, answer, status |
| `tests/` | `python3 -m unittest discover -s plugin/tests` and `node --test plugin/tests/round.test.mjs`, from the repo's top |
| `PLAN.md` | The design, and what's planned next |
