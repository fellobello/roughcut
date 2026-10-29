"""Past roughcut decisions with known answers, asked of a decision model at Ollama's /v1/systemone.

    python3 -I tools/replay_decider.py [model] [stuck]

The blame cases are the 21 the reviewer judged in the SplitTip runs (truth from
TEST-RUN-NOTES.md); the stuck cases are the stuck-file questions answered with one of
the options. Needs the two projects in ~/roughcut-projects and a decision model pulled.
"""
import glob, io, json, re, subprocess, sys, tarfile, tempfile, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rclocal.blame import settle, tester_part
from rclocal.contracts import file_id
from rclocal.rounds import STUCK_OPTIONS, failure_digest
from rclocal.stacks import STACKS

MODEL = sys.argv[1] if len(sys.argv) > 1 else "nimble"
web = STACKS["web"]
P1, P2 = Path.home() / "roughcut-projects/u-build-tip", Path.home() / "roughcut-projects/u-build-tip-2"
EITHER = {"code", "contract"}
BLAME_TRUTH = {  # from TEST-RUN-NOTES.md
    (P1, 0, "app/bill.js"): "contract", (P1, 0, "app/dark_mode.js"): "contract", (P1, 0, "app/utils.js"): "contract",
    (P1, 1, "app/bill.js"): "code", (P1, 1, "app/bill_parts/split_bill.js"): "code", (P1, 1, "app/dark_mode.js"): "contract",
    (P1, 2, "app/bill_parts/split_bill.js"): "code", (P1, 4, "app/bill.js"): "code",
    (P2, 0, "app/split.js"): "code", (P2, 0, "app/split_parts/split_amount.js"): "code",
    (P2, 0, "app/split_parts/validate_input.js"): "test", (P2, 1, "app/split_parts/calculate_total.js"): "test",
    (P2, 1, "app/split_parts/validate_input.js"): "code", (P2, 2, "app/split_parts/calculate_total.js"): "test",
    (P2, 2, "app/split_parts/validate_input.js"): "test", (P2, 3, "app/split_parts/calculate_total.js"): "test",
    (P2, 3, "app/split_parts/validate_input.js"): "test", (P2, 4, "app/bill.js"): "code",
    (P2, 4, "app/split.js"): EITHER, (P2, 4, "app/split_parts/split_amount.js"): EITHER,
    (P2, 4, "app/split_parts/split_logic.js"): EITHER,
}
BLAME_Q = {"type": "choice", "instructions": (
    "A file keeps failing its tests. Who is wrong? Small models wrote the contract's examples, the tests and the code, "
    "and the owner's rules come first."), "criteria": {
    "code": "The code breaks its contract or the owner's rules, so the code must change.",
    "test": "A test expects something the contract doesn't promise, uses the wrong API, or breaks the owner's rules.",
    "contract": "The contract can't work as written: its examples are wrong, it contradicts the owner's rules, "
                "or a function is missing an input."}}
STUCK_Q = {"type": "choice", "instructions": (
    "A file is stuck: the builder couldn't make its tests pass. Pick what to do. The owner's way: the first time a "
    "file is stuck, try again; if it's stuck again and the code already does what the rules ask, it works; if the "
    "contract asks for something impossible, simplify it; if the file isn't needed, the owner takes it."),
    "criteria": {o["label"]: o["detail"] for o in STUCK_OPTIONS}}


def ask(state, question):
    body = json.dumps({"model": MODEL, "state": state, "questions": {"q": question}, "keep_alive": "10m"}).encode()
    req = urllib.request.Request("http://localhost:11434/v1/systemone", body, {"Content-Type": "application/json"})
    start = time.monotonic()
    answer = json.load(urllib.request.urlopen(req, timeout=600))["answers"]["q"]
    return answer["choice"], answer["confidence"], round(time.monotonic() - start, 2)


def contract_text(c):
    return "\n".join(f"{e['signature']}: {e['description']}" + "".join(f"\n  e.g. {x['call']} returns {x['returns']}"
                                                                     for x in e.get("examples", [])) for e in c["exports"])


def unpack(proj, commit, tmp):
    data = subprocess.run(["git", "-C", proj, "archive", commit], capture_output=True).stdout
    tarfile.open(fileobj=io.BytesIO(data)).extractall(tmp, filter="data")


def builds(proj):
    log = subprocess.run(["git", "-C", proj, "log", "--format=%h %s"], capture_output=True, text=True).stdout
    return {int(m.group(2)): m.group(1) for m in re.finditer(r"^(\w+) Round (\d+) build$", log, re.M)}


rows = {"blame": [], "stuck": []}
for (proj, rnd, path), truth in (BLAME_TRUTH.items() if "stuck" not in sys.argv else []):
    with tempfile.TemporaryDirectory() as tmp:
        unpack(proj, builds(proj)[rnd], tmp)
        t = Path(tmp)
        c = json.loads((t / ".hub/contracts" / f"{file_id(path)}.json").read_text())
        c.setdefault("test_path", web.test_path(path))
        tests = (t / c["test_path"]).read_text()
        code = (t / path).read_text() if (t / path).exists() else ""
        out = subprocess.run(["node", "--test", "--test-reporter=spec", c["test_path"]], cwd=tmp, capture_output=True, text=True)
        failure = (out.stdout + out.stderr)[-4000:]
        rules = json.loads((t / ".hub/brief.json").read_text()).get("rules", []) if (t / ".hub/brief.json").exists() else []
        state = {"owner_rules": rules, "contract": contract_text(c), "tests": tests[:3000], "code": code[:3000],
                 "failure": failure_digest(failure, 1500)}
        facts = settle(web, c, tests, code, failure)
        choice, conf, secs = ask(state, BLAME_Q)
        rows["blame"].append({"case": f"{'run1' if proj == P1 else 'run2'} r{rnd} {path}", "truth": "/".join(sorted(truth)) if isinstance(truth, set) else truth,
                              "nimble": choice, "confidence": conf, "seconds": secs, "facts": facts and facts["blame"]})
        print(rows["blame"][-1], flush=True)

for proj in (P1, P2):
    seen = {}
    for f in sorted(glob.glob(str(proj / ".hub/questions/*.json")), key=lambda f: (f.rsplit("/", 1)[1].rsplit(".", 2)[0], int(f.rsplit(".", 2)[1]))):
        q = json.load(open(f))
        labels = [o["label"] if isinstance(o, dict) else o for o in q.get("options", [])]
        if "Try again" not in labels or q.get("answer") not in labels:
            continue  # free-text answers have no single right option
        task = q["task"]
        before = seen.get(task, 0)
        seen[task] = before + 1
        state = {"question": q["text"][:2500], "times_stuck_before": before, "owner_rules": json.loads((proj / ".hub/brief.json").read_text()).get("rules", [])}
        choice, conf, secs = ask(state, STUCK_Q)
        rows["stuck"].append({"case": f"{'run1' if proj == P1 else 'run2'} {q['id']}", "truth": q["answer"],
                              "nimble": choice, "confidence": conf, "seconds": secs})
        print(rows["stuck"][-1], flush=True)


for kind, rs in [(k, v) for k, v in rows.items() if v]:
    ok = lambda r: r["nimble"] in r["truth"].split("/")
    right = sum(ok(r) for r in rs)
    sure_wrong = sum(1 for r in rs if not ok(r) and r["confidence"] >= 0.7)
    print(f"{kind}: {right}/{len(rs)} right, {sure_wrong} wrong with confidence >= 0.7, "
          f"median {sorted(r['seconds'] for r in rs)[len(rs) // 2]}s")
