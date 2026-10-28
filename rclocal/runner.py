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
# Shared by the probe and the test-user script: how a control is named, and what the page shows.
PAGE_HELPERS = """const sleep = ms => new Promise(r => setTimeout(r, ms));
const report = r => { document.body.dataset.probe = JSON.stringify(r); };
const squash = s => (s || '').replace(/\\s+/g, ' ').trim();
const labelOf = el => squash((el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label')
  || el.placeholder || (el.tagName === 'BUTTON' ? el.innerText : '') || el.name || el.id);
const visible = el => !!el.offsetParent;
const controls = () => [...document.querySelectorAll('input, select, textarea, button')]
  .filter(e => visible(e) && e.type !== 'hidden')
  .map(e => ({ id: e.id, kind: e.tagName === 'BUTTON' ? 'button' : e.tagName === 'SELECT' ? 'select' : (e.type || 'text'),
               label: labelOf(e).slice(0, 60), value: e.type === 'checkbox' ? String(e.checked) : (e.value || '').slice(0, 30),
               options: e.tagName === 'SELECT' ? [...e.options].map(o => squash(o.text)).slice(0, 12) : undefined,
               pressed: e.getAttribute('aria-pressed') || undefined }));
const results = () => [...document.querySelectorAll('.stat, .list li, output, .error')]
  .filter(e => visible(e) && squash(e.innerText)).map(e => squash(e.innerText).slice(0, 80)).slice(0, 12);
for (let i = 0; i < 40 && document.body.dataset.ready !== '1'; i++) await sleep(50);
const main = document.querySelector('main') || document.body;"""

# Runs after the app's module script (modules run in order).
PROBE_TAIL = """<script type="module">
""" + PAGE_HELPERS + """
const inventory = { controls: controls(), results: results() };
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
         hasResults: !!document.querySelector('.stat-value, .list, output'), inventory });
</script>"""

# A test user's steps, carried out one at a time. After each step it records what the
# results show, any visible error, and whether the text the user expected is on the page.
USER_TAIL = """<script type="module">
""" + PAGE_HELPERS + """
const steps = __STEPS__;
const norm = s => squash(s).toLowerCase();
const find = target => {
  const t = norm(target).replace(/^#/, '');
  const all = [...document.querySelectorAll('input, select, textarea, button')].filter(e => visible(e) && e.type !== 'hidden');
  return all.find(e => norm(e.id) === t) || all.find(e => norm(labelOf(e)) === t)
    || all.find(e => t && norm(labelOf(e)).includes(t)) || all.find(e => norm(labelOf(e)).length > 2 && t.includes(norm(labelOf(e))));
};
const log = [];
const typed = [];
for (const s of steps) {
  if ((s.value ?? '') !== '' && !isNaN(Number(s.value))) typed.push(Number(s.value));
  const entry = { do: s.do, target: s.target, value: s.value, expect: s.expect };
  const el = find(s.target);
  if (!el) { entry.result = 'there is no such control on the page'; log.push(entry); continue; }
  const field = el.tagName !== 'BUTTON' && (s.value ?? '') !== '';  // a value on a field is typed, whatever the step says
  if (el.tagName === 'BUTTON' || (s.do === 'click' && !field)) el.click();
  else if (el.type === 'checkbox' || el.type === 'radio') {
    const want = !/^(false|no|off|0|uncheck)/i.test(s.value || '');
    if (el.checked !== want) el.click();
  } else if (el.tagName === 'SELECT') {
    const option = [...el.options].find(o => norm(o.text) === norm(s.value) || norm(o.value) === norm(s.value))
      || [...el.options].find(o => norm(o.text).includes(norm(s.value)));
    if (option) el.value = option.value; else entry.result = `no option "${s.value}"`;
  } else el.value = s.value ?? '';
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  await sleep(200);
  entry.result = entry.result || 'done';
  entry.shows = results();
  if (s.expect) {
    // Numbers are what matter: every number the user expected must be on the page, in any format.
    const text = norm(main.innerText).replace(/,/g, '');
    // Numbers the user typed in are in the fields, not the page text: expecting those checks nothing.
    const nums = (s.expect.replace(/,/g, '').match(/\\d+(\\.\\d+)?/g) || []).filter(n => !typed.includes(Number(n)));
    const missing = nums.filter(n => !new RegExp('(^|[^\\\\d.])' + n.replace('.', '\\\\.') + '(?![\\\\d])').test(text)
      && !(n.includes('.') && text.includes(Number(n).toFixed(2))));
    entry.saw_expected = nums.length ? missing.length === 0 : text.includes(norm(s.expect));
    if (missing.length) entry.missing = missing;
  }
  log.push(entry);
}
report({ ready: document.body.dataset.ready === '1', errors: window.__probeErrors, log, controls: controls() });
</script>"""

# What the probe's first failure means for the page: which part a fix should rewrite.
SCRIPT_PART, MARKUP_PART = "script", "markup"


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

        After a call, self.last_check holds what the probe saw: which part of the page a fix
        should rewrite ("script" or "markup"), what it reported, and the page's controls and
        results before anything was typed (what the test users get to see).
        """
        self.last_check = {"part": SCRIPT_PART, "probe": None, "inventory": None, "hidden": []}
        if not self.chrome():
            return True, "no Chrome to check the page with; skipped"
        html = (self.project / "index.html").read_text(encoding="utf-8")
        try:
            dom = await self._load(_with_tail(html, PROBE_TAIL), screenshot=screenshot)
        except subprocess.TimeoutExpired:
            return False, "the page didn't finish loading within 60s"
        ok, problem, part, probe = _probe_verdict(dom)
        self.last_check = {
            "part": part, "probe": probe, "inventory": (probe or {}).get("inventory"),
            "hidden": (probe or {}).get("stillHidden") or [],
        }
        return ok, problem

    async def try_as_user(self, steps, viewport=(1280, 900), screenshot=None):
        """Carry out a test user's steps in headless Chrome. Returns the probe report, or None if it didn't run."""
        import json

        if not self.chrome():
            return None
        html = (self.project / "index.html").read_text(encoding="utf-8")
        tail = USER_TAIL.replace("__STEPS__", json.dumps(steps).replace("</", "<\\/"))
        try:
            dom = await self._load(_with_tail(html, tail), viewport=viewport, screenshot=screenshot, after=True)
        except subprocess.TimeoutExpired:
            return {"ready": False, "errors": ["the page didn't finish within 60s"], "log": [], "controls": []}
        return _probe_report(dom)

    async def _load(self, page_html, viewport=(1280, 900), screenshot=None, after=False):
        """Serve the project, open page_html (a throwaway copy of index.html) and return its DOM.

        The screenshot is of index.html as a visitor first sees it, or with after=True of the
        copy, so it shows the page once the script in it has run.
        """
        probe_page = self.project / PROBE_PAGE
        probe_page.write_text(page_html, encoding="utf-8")
        port = free_port()
        server = self.serve(port)
        try:
            await asyncio.sleep(0.6)
            base = f"http://127.0.0.1:{port}/"
            with tempfile.TemporaryDirectory(prefix="roughcut-chrome-") as profile:
                args = [
                    self.chrome(), "--headless=new", "--disable-gpu", f"--user-data-dir={profile}", "--no-first-run",
                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", "--virtual-time-budget=8000",
                    f"--window-size={viewport[0]},{viewport[1]}", "--hide-scrollbars",
                ]
                dom = await asyncio.to_thread(
                    subprocess.run, args + ["--dump-dom", base + PROBE_PAGE], capture_output=True, text=True, timeout=60
                )
                if screenshot:
                    Path(screenshot).parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(
                        subprocess.run, args + [f"--screenshot={screenshot}", base + (PROBE_PAGE if after else "index.html")],
                        capture_output=True, timeout=60,
                    )
        finally:
            self.stop(server)
            probe_page.unlink(missing_ok=True)
        return dom.stdout


def _with_tail(html, tail):
    head = html.find("<head>")
    html = html[: head + 6] + PROBE_HEAD + html[head + 6:] if head != -1 else PROBE_HEAD + html
    end = html.rfind("</body>")
    return html[:end] + tail + html[end:] if end != -1 else html + tail


def _with_probe(html):
    return _with_tail(html, PROBE_TAIL)


def _probe_report(dom):
    import html as html_lib
    import json
    import re

    found = re.search(r'data-probe="([^"]*)"', dom or "")
    return json.loads(html_lib.unescape(found.group(1))) if found else None


def _probe_verdict(dom):
    """Return (ok, problem, part, report): part is the page part a fix should rewrite."""
    if not (dom or "").strip():
        return False, "the page didn't load at all", SCRIPT_PART, None
    r = _probe_report(dom)
    if r is None:
        if 'data-ready="1"' not in dom:
            return False, (
                "the page script never finished (body has no data-ready=\"1\"): it threw an error, or imported "
                "a name or file that doesn't exist"
            ), SCRIPT_PART, None
        return False, "the page check couldn't run its probe", SCRIPT_PART, None
    filled = ", ".join(r["filled"]) or "no fields"
    if r["errors"]:
        return False, f"after filling in {filled}, the page threw: {'; '.join(r['errors'][:3])}", SCRIPT_PART, r
    if not r["ready"]:
        return False, 'the page script never set document.body.dataset.ready = "1"', SCRIPT_PART, r
    if r.get("prefilled"):
        return False, (
            f"before the user entered anything, the results already showed {', '.join(r['prefilled'])}: "
            "don't put example numbers in the HTML or compute results before the user types; show \"—\" until the "
            "inputs are valid"
        ), MARKUP_PART, r
    if r["error"]:
        return False, f"after filling in {filled} with sensible values, the page showed the error: {r['error']}", SCRIPT_PART, r
    if r.get("hasResults") and not r.get("shown"):
        hidden = r.get("stillHidden") or []
        why = f" These still have the hidden attribute: {', '.join(hidden)}; don't hide results." if hidden else ""
        return False, f"after filling in {filled}, no result value with a number is visible.{why}", (
            MARKUP_PART if hidden else SCRIPT_PART), r
    if not r["changed"]:
        hidden = r.get("stillHidden") or []
        why = (f" These result elements still have the hidden attribute: {', '.join(hidden)}. Remove hidden from "
               "every element that contains the results, not only the inner ones.") if hidden else ""
        return False, (
            f"after filling in {filled}, nothing on the page changed: results must update as the user types.{why}"
        ), SCRIPT_PART, r
    return True, "", None, r
