"""The box: running model-written code without trusting it.

Nothing a model writes runs until the stack's guard has passed it (stacks.py). Then
every test run, and the static server for the page check, is boxed in four ways:

1. sandbox-exec (macOS): no internet (localhost only), no writes outside the project and
   the temp folders, and no reads anywhere in your home folder except the project.
2. Resource limits: CPU seconds, largest file it can write, open files.
3. A wall-clock timeout, after which the whole process group is killed.
4. A clean environment: none of your shell's variables (tokens, keys) are passed on.

Without sandbox-exec (not macOS, or "sandbox": false) only 2 to 4 apply, and the
round says so. The page check runs headless Chrome with every host but 127.0.0.1
unreachable.
"""

from __future__ import annotations

import asyncio
import os
import resource
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROFILE = """(version 1)
(allow default)
(deny network*)
(allow network-bind (local ip "localhost:*"))
(allow network-inbound (local ip "localhost:*"))
(allow network-outbound (remote ip "localhost:*"))
(deny file-write*)
(allow file-write* (subpath "{project}") (subpath "/private/tmp") (subpath "/private/var/folders") (literal "/dev/null"))
(deny file-read* (subpath "{home}"))
(allow file-read* (subpath "{project}") (literal "{home}"))
"""
KEEP_OUTPUT = 4000  # characters of output kept for the model; the end is where the failures are
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SAFE_ENV = ("PATH", "LANG", "LC_ALL", "TMPDIR")


def _limits(cpu_seconds):
    def apply():
        os.setsid()  # its own process group, so a timeout can kill everything it started
        for limit, value in (
            (resource.RLIMIT_CPU, cpu_seconds),
            (resource.RLIMIT_FSIZE, 50 * 2**20),
            (resource.RLIMIT_NOFILE, 1024),
        ):
            try:
                soft, hard = resource.getrlimit(limit)
                resource.setrlimit(limit, (min(value, hard) if hard != resource.RLIM_INFINITY else value, hard))
            except (ValueError, OSError):
                pass

    return apply


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Runner:
    def __init__(self, project, stack, timeout=60, sandbox=True, events=None):
        self.project = Path(project).resolve()
        self.stack = stack
        self.timeout = timeout
        self.events = events
        self.sandbox = bool(sandbox) and sys.platform == "darwin" and bool(shutil.which("sandbox-exec"))
        self._profile = None

    def boxed(self, cmd):
        if not self.sandbox:
            return cmd
        if self._profile is None:
            fd, self._profile = tempfile.mkstemp(prefix="roughcut-local-", suffix=".sb")
            with os.fdopen(fd, "w") as f:
                f.write(PROFILE.format(project=self._quote(self.project), home=self._quote(Path.home().resolve())))
        return ["sandbox-exec", "-f", self._profile, *cmd]

    @staticmethod
    def _quote(path):
        return str(path).replace("\\", "\\\\").replace('"', '\\"')

    def env(self):
        env = {k: os.environ[k] for k in SAFE_ENV if k in os.environ}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(self.project), HOME=str(self.project))
        return env

    async def run(self, test_file):
        """Run one test file. Returns (passed, output)."""
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *self.boxed(self.stack.test_command(sys.executable, test_file)),
            cwd=self.project,
            env=self.env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            preexec_fn=_limits(self.timeout + 5),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.timeout)
            output, passed = out.decode(errors="replace"), proc.returncode == 0
        except asyncio.TimeoutError:
            self._kill(proc.pid)
            await proc.wait()
            output, passed = f"the tests ran longer than {self.timeout}s and were stopped (an endless loop?)", False
        if self.events:
            self.events.emit("tests", target=test_file, passed=passed, seconds=round(time.monotonic() - start, 2))
        return passed, output[-KEEP_OUTPUT:]

    @staticmethod
    def _kill(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    # The page check (web stack)

    def serve(self, port):
        """Serve the project folder on 127.0.0.1, boxed. Returns the Popen; stop it with stop()."""
        cmd = [sys.executable, "-s", "-m", "http.server", str(port), "--bind", "127.0.0.1", "-d", str(self.project)]
        return subprocess.Popen(
            self.boxed(cmd), cwd=self.project, env=self.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=_limits(3600),
        )

    def stop(self, proc):
        self._kill(proc.pid)
        proc.wait()

    def chrome(self):
        return CHROME if os.path.exists(CHROME) else shutil.which("google-chrome") or shutil.which("chromium")

    async def check_page(self, screenshot=None):
        """Load index.html in headless Chrome. Returns (ok, problem). ok means the page script finished.

        The designer's script sets data-ready="1" on <body> as its last step, so a script that
        throws, or imports something that doesn't exist, leaves it unset.
        """
        chrome = self.chrome()
        if not chrome:
            return True, "no Chrome to check the page with; skipped"
        port = free_port()
        server = self.serve(port)
        try:
            await asyncio.sleep(0.6)
            url = f"http://127.0.0.1:{port}/index.html"
            with tempfile.TemporaryDirectory(prefix="roughcut-chrome-") as profile:
                base = [
                    chrome, "--headless=new", "--disable-gpu", f"--user-data-dir={profile}", "--no-first-run",
                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", "--virtual-time-budget=4000",
                    "--window-size=1280,900", "--hide-scrollbars",
                ]
                dom = await asyncio.to_thread(
                    subprocess.run, base + ["--dump-dom", url], capture_output=True, text=True, timeout=60
                )
                if screenshot:
                    Path(screenshot).parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(
                        subprocess.run, base + [f"--screenshot={screenshot}", url], capture_output=True, timeout=60
                    )
        except subprocess.TimeoutExpired:
            return False, "the page didn't finish loading within 60s"
        finally:
            self.stop(server)
        html = dom.stdout
        if 'data-ready="1"' in html:
            return True, ""
        if not html.strip():
            return False, "the page didn't load at all"
        return False, (
            "the page script never finished (body has no data-ready=\"1\"): it threw an error, or imported a "
            "name or file that doesn't exist"
        )
