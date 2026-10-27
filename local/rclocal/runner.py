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
(allow file-read-metadata {parents})
"""
KEEP_OUTPUT = 4000  # characters of output kept for the model; the end is where the failures are
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SAFE_ENV = ("PATH", "LANG", "LC_ALL", "TMPDIR")
PROBE_PAGE = ".probe.html"  # a throwaway copy of index.html with the probe added; git-ignored

# Runs before the app's script, so it sees errors the app throws while loading.
PROBE_HEAD = """<script>window.__probeErrors = [];
addEventListener('error', e => __probeErrors.push(String(e.message || e)));
addEventListener('unhandledrejection', e => __probeErrors.push(String(e.reason)));</script>"""
# Runs after the app's module script (modules run in order).
PROBE_TAIL = """<script type="module">
const sleep = ms => new Promise(r => setTimeout(r, ms));
const report = r => { document.body.dataset.probe = JSON.stringify(r); };
for (let i = 0; i < 40 && document.body.dataset.ready !== '1'; i++) await sleep(50);
const main = document.querySelector('main') || document.body;
const before = main.innerText;
const prefilled = [...document.querySelectorAll('.stat-value, .list li, output')]
  .filter(e => e.offsetParent && /[1-9]/.test(e.innerText)).map(e => e.innerText.trim()).slice(0, 3);
const fields = [...document.querySelectorAll('input, select, textarea')].filter(el => el.type !== 'hidden');
const filled = [];
for (const el of fields) {
  if (el.type === 'checkbox' || el.type === 'radio') { if (!el.checked) el.click(); filled.push(el.id || el.name || el.type); continue; }
  if (el.tagName === 'SELECT') { if (el.options.length > 1) el.selectedIndex = 1; }
  else el.value = el.dataset.sample ?? (el.type === 'number' || el.inputMode === 'decimal' ? '12' : 'Sample');
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  filled.push(`${el.id || el.name || el.tagName.toLowerCase()}=${el.value}`);
  await sleep(30);
}
const seg = document.querySelector('.seg button[aria-pressed="false"]');
if (seg) seg.click();
const primary = document.querySelector('.btn-primary');
if (primary) primary.click();
await sleep(300);
const errorEl = [...document.querySelectorAll('.error')].find(e => e.offsetParent && e.innerText.trim());
const shown = [...document.querySelectorAll('.stat-value, .list li, output')]
  .filter(e => e.offsetParent && /[0-9]/.test(e.innerText)).length;
const name = e => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + [...e.classList].map(c => '.' + c).join('');
const stillHidden = [...document.querySelectorAll('[hidden]')]
  .filter(e => e.matches('.stats, .stat, .list, output, .stat-value') || e.querySelector('.stat-value, .stat, .list, output'))
  .map(name).slice(0, 4);
report({ ready: document.body.dataset.ready === '1', errors: window.__probeErrors, filled, prefilled,
         changed: main.innerText !== before, error: errorEl ? errorEl.innerText.trim() : '', stillHidden, shown,
         hasResults: !!document.querySelector('.stat-value, .list, output') });
</script>"""


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
            # Node checks every folder above a file it loads, so those folders' metadata (not
            # their contents) stays visible.
            parents = " ".join(f'(literal "{self._quote(p)}")' for p in self.project.parents) or '(literal "/")'
            with os.fdopen(fd, "w") as f:
                f.write(PROFILE.format(
                    project=self._quote(self.project), home=self._quote(Path.home().resolve()), parents=parents,
                ))
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
        """Load index.html in headless Chrome and use it like a person would. Returns (ok, problem).

        A copy of the page gets a probe: it collects script errors, waits for the app's
        data-ready="1", then fills each field in turn (its data-sample value, or a plain
        default), firing input/change after each, clicks the main button if there is one, and
        checks that the page changed and shows no .error. A page that only looks right fails.
        """
        chrome = self.chrome()
        if not chrome:
            return True, "no Chrome to check the page with; skipped"
        index = self.project / "index.html"
        probe_page = self.project / PROBE_PAGE
        html = index.read_text(encoding="utf-8")
        probe_page.write_text(_with_probe(html), encoding="utf-8")
        port = free_port()
        server = self.serve(port)
        try:
            await asyncio.sleep(0.6)
            base = f"http://127.0.0.1:{port}/"
            with tempfile.TemporaryDirectory(prefix="roughcut-chrome-") as profile:
                args = [
                    chrome, "--headless=new", "--disable-gpu", f"--user-data-dir={profile}", "--no-first-run",
                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", "--virtual-time-budget=6000",
                    "--window-size=1280,900", "--hide-scrollbars",
                ]
                dom = await asyncio.to_thread(
                    subprocess.run, args + ["--dump-dom", base + PROBE_PAGE], capture_output=True, text=True, timeout=60
                )
                if screenshot:
                    Path(screenshot).parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(
                        subprocess.run, args + [f"--screenshot={screenshot}", base + "index.html"],
                        capture_output=True, timeout=60,
                    )
        except subprocess.TimeoutExpired:
            return False, "the page didn't finish loading within 60s"
        finally:
            self.stop(server)
            probe_page.unlink(missing_ok=True)
        return _probe_verdict(dom.stdout)


def _with_probe(html):
    head = html.find("<head>")
    html = html[: head + 6] + PROBE_HEAD + html[head + 6:] if head != -1 else PROBE_HEAD + html
    end = html.rfind("</body>")
    return html[:end] + PROBE_TAIL + html[end:] if end != -1 else html + PROBE_TAIL


def _probe_verdict(dom):
    import html as html_lib
    import json
    import re

    if not dom.strip():
        return False, "the page didn't load at all"
    found = re.search(r'data-probe="([^"]*)"', dom)
    if not found:
        if 'data-ready="1"' not in dom:
            return False, (
                "the page script never finished (body has no data-ready=\"1\"): it threw an error, or imported "
                "a name or file that doesn't exist"
            )
        return False, "the page check couldn't run its probe"
    r = json.loads(html_lib.unescape(found.group(1)))
    filled = ", ".join(r["filled"]) or "no fields"
    if r["errors"]:
        return False, f"after filling in {filled}, the page threw: {'; '.join(r['errors'][:3])}"
    if not r["ready"]:
        return False, 'the page script never set document.body.dataset.ready = "1"'
    if r.get("prefilled"):
        return False, (
            f"before the user entered anything, the results already showed {', '.join(r['prefilled'])}: "
            "don't put example numbers in the HTML; show the .empty state until the inputs are valid"
        )
    if r["error"]:
        return False, f"after filling in {filled} with sensible values, the page showed the error: {r['error']}"
    if r.get("hasResults") and not r.get("shown"):
        hidden = r.get("stillHidden") or []
        why = f" These still have the hidden attribute: {', '.join(hidden)}; don't hide results." if hidden else ""
        return False, f"after filling in {filled}, no result value with a number is visible.{why}"
    if not r["changed"]:
        hidden = r.get("stillHidden") or []
        why = (f" These result elements still have the hidden attribute: {', '.join(hidden)}. Remove hidden from "
               "every element that contains the results, not only the inner ones.") if hidden else ""
        return False, f"after filling in {filled}, nothing on the page changed: results must update as the user types.{why}"
    return True, ""
