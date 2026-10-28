#!/usr/bin/env python3
"""A Claude Code status line that also saves your usage limits for roughcut's round reports.

Claude Code passes the 5-hour and weekly usage percentages only to the status line
command, so this script is how roughcut sees them. Point your statusLine setting at it:

    {"statusLine": {"type": "command", "command": "python3 /path/to/roughcut/scripts/usage_statusline.py"}}

It saves the latest numbers to ~/.claude/roughcut/usage.json (or $ROUGHCUT_USAGE_FILE)
and prints a short status line: the model and both percentages.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

WINDOWS = ("five_hour", "seven_day")


def usage_file():
    return Path(os.environ.get("ROUGHCUT_USAGE_FILE") or Path.home() / ".claude" / "roughcut" / "usage.json")


def save(limits, session_id):
    path = usage_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"rate_limits": limits, "session_id": session_id, "seen_at": int(time.time())}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def line(session):
    parts = []
    model = (session.get("model") or {}).get("display_name")
    if model:
        parts.append(f"[{model}]")
    limits = session.get("rate_limits") or {}
    labels = {"five_hour": "5h", "seven_day": "week"}
    shown = [
        f"{labels[w]} {limits[w]['used_percentage']:.0f}%"
        for w in WINDOWS
        if isinstance(limits.get(w), dict) and limits[w].get("used_percentage") is not None
    ]
    if shown:
        parts.append(" · ".join(shown))
    return " ".join(parts)


def main():
    try:
        session = json.load(sys.stdin)
    except ValueError:
        return 0  # never break the status line over bad input
    limits = {w: session["rate_limits"][w] for w in WINDOWS if w in (session.get("rate_limits") or {})}
    if limits:
        try:
            save(limits, session.get("session_id"))
        except OSError:
            pass
    print(line(session))
    return 0


if __name__ == "__main__":
    sys.exit(main())
