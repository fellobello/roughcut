"""Running the project's tests, which models wrote, without trusting them.

Tests run with a time limit and, on macOS, inside sandbox-exec: no network, and no
writes outside the project folder and the system temp folders. Elsewhere, or with
"sandbox": false in local.json, they run unsandboxed.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROFILE = """(version 1)
(allow default)
(deny network*)
(deny file-write*)
(allow file-write* (subpath "{project}") (subpath "/private/tmp") (subpath "/private/var/folders") (literal "/dev/null"))
"""
FAILED_MODULE = re.compile(r"^(?:FAIL|ERROR): \S+ \((tests\.test_[a-z0-9_]+)\b", re.M)
FAILED_IMPORT = re.compile(r"^(?:FAIL|ERROR): (tests\.test_[a-z0-9_]+) \(unittest\.loader", re.M)
KEEP_OUTPUT = 4000  # characters of output kept for the model; the end is where the failures are


class Runner:
    def __init__(self, project, timeout=60, sandbox=True, events=None):
        self.project = Path(project).resolve()
        self.timeout = timeout
        self.events = events
        self.sandbox = bool(sandbox) and sys.platform == "darwin" and shutil.which("sandbox-exec")
        self._profile = None

    def _command(self, args):
        cmd = [sys.executable, "-m", "unittest", *args]
        if not self.sandbox:
            return cmd
        if self._profile is None:
            fd, self._profile = tempfile.mkstemp(prefix="roughcut-local-", suffix=".sb")
            with os.fdopen(fd, "w") as f:
                f.write(PROFILE.format(project=str(self.project).replace('"', '\\"')))
        return ["sandbox-exec", "-f", self._profile, *cmd]

    async def run(self, target=None):
        """Run one test file (a path like tests/test_app_geo.py), or every test. Returns (passed, output)."""
        if target:
            args = [target[:-3].replace("/", ".")]
        else:
            args = ["discover", "-s", "tests", "-t", "."]
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(self.project)}
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *self._command(args),
            cwd=self.project,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.timeout)
            output = out.decode(errors="replace")
            passed = proc.returncode == 0
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            output, passed = f"the tests ran longer than {self.timeout}s and were stopped (an endless loop?)", False
        if self.events:
            self.events.emit("tests", target=target or "all", passed=passed, seconds=round(time.monotonic() - start, 2))
        return passed, output[-KEEP_OUTPUT:]

    @staticmethod
    def failing_test_files(output):
        """Test files with a failure or error in a full run's output, as paths."""
        modules = set(FAILED_MODULE.findall(output)) | set(FAILED_IMPORT.findall(output))
        return sorted(m.replace(".", "/") + ".py" for m in modules)
