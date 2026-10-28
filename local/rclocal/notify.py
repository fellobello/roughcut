"""Telling you something needs you, and keeping the Mac awake while it works.

Both use what macOS already has (osascript and caffeinate) and do nothing elsewhere.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys

# The text goes in as arguments, never into the script, so nothing in it can run.
NOTIFY = 'on run argv\ndisplay notification (item 1 of argv) with title (item 2 of argv) sound name "Glass"\nend run'


def notify(message, title="Roughcut Local"):
    """A macOS notification. Returns True if one was sent."""
    if sys.platform != "darwin" or not shutil.which("osascript") or os.environ.get("ROUGHCUT_QUIET"):
        return False
    try:
        subprocess.run(["osascript", "-e", NOTIFY, message[:220], title], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


@contextlib.contextmanager
def awake():
    """Keep the Mac from sleeping while the block runs (plugged in; closing the lid still sleeps it)."""
    proc = None
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        # -i: no idle sleep; -s: no system sleep on power; -w: give up if this process dies.
        proc = subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())])
    try:
        yield proc is not None
    finally:
        if proc:
            proc.terminate()
            proc.wait()
