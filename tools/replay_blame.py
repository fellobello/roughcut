"""Replay the failures past runs sent to the reviewer through rclocal.blame.settle, with no model calls.

    python3 -I tools/replay_blame.py ~/roughcut-projects/<project> [...]

For every (round, file) the reviewer judged, the project's "Round N build" commit is
unpacked into a temporary folder, the file's tests are run again with node, and the facts'
verdict is printed beside the reviewer's. It needs only node and the project's git history.
"""
import json, re, subprocess, sys, tempfile, tarfile, io
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rclocal.blame import settle
from rclocal.stacks import STACKS
from rclocal.contracts import file_id

web = STACKS["web"]
rows = []
for proj in sys.argv[1:]:
    proj = Path(proj).expanduser()
    reviews = {}
    for line in open(proj / ".hub/events.jsonl"):
        e = json.loads(line)
        m = re.match(r"review of (\S+): blame (\w+): (.*)", e.get("text", "")) if e.get("type") == "note" else None
        if m:
            reviews.setdefault((e["round"], m.group(1)), []).append((m.group(2), m.group(3)))
    log = subprocess.run(["git", "-C", proj, "log", "--format=%h %s"], capture_output=True, text=True).stdout
    builds = {int(m.group(2)): m.group(1) for m in re.finditer(r"^(\w+) Round (\d+) build$", log, re.M)}
    for (rnd, path), said in sorted(reviews.items()):
        commit = builds.get(rnd)
        with tempfile.TemporaryDirectory() as tmp:
            data = subprocess.run(["git", "-C", proj, "archive", commit], capture_output=True).stdout
            tarfile.open(fileobj=io.BytesIO(data)).extractall(tmp, filter="data")
            t = Path(tmp)
            cfile = t / ".hub/contracts" / f"{file_id(path)}.json"
            if not cfile.exists():
                rows.append((proj.name, rnd, path, said, None, "no contract at that commit", "")); continue
            contract = json.loads(cfile.read_text())
            test_path = contract.get("test_path") or web.test_path(path)
            contract.setdefault("test_path", test_path)
            tests = (t / test_path).read_text() if (t / test_path).exists() else ""
            code = (t / path).read_text() if (t / path).exists() else ""
            out = subprocess.run(["node", "--test", "--test-reporter=spec", test_path], cwd=tmp, capture_output=True,
                                 text=True, timeout=60)
            failure = (out.stdout + out.stderr)[-4000:]
            v = settle(web, contract, tests, code, failure) if out.returncode else {"blame": "-", "reason": "passes at the build commit"}
            rows.append((proj.name, rnd, path, said, v, failure, tests))
for p, r, f, said, v, *_ in rows:
    model = ", ".join(sorted({b for b, _ in said}))
    print(f"{p[-1]:>1} r{r} {f:42} model:{model:14} facts:{(v or {}).get('blame') or 'reviewer':9} {(v or {}).get('reason', '')[:110]}")
