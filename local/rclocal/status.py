"""The status page: what the round is doing right now, and what it costs the machine.

It only reads: the hub's task files, the event log and live readings from the system and
Ollama. So it can run in its own terminal next to a round, or on its own between rounds.
Open http://127.0.0.1:8765 after `roughcut-local ui`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .llm import LLMError


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def memory():
    """Total and used memory in bytes (macOS vm_stat), or None where it isn't available."""
    total = _run(["sysctl", "-n", "hw.memsize"]).strip()
    stat = _run(["vm_stat"])
    page = re.search(r"page size of (\d+) bytes", stat)
    if not (total.isdigit() and page):
        return None

    def pages(label):
        m = re.search(rf"{label}:\s+(\d+)", stat)
        return int(m.group(1)) if m else 0

    used = (pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")) * int(page.group(1))
    return {"total": int(total), "used": used}


def ollama_process():
    """CPU percent (100 = one core) and resident memory of every Ollama process together."""
    cpu, rss = 0.0, 0
    for line in _run(["ps", "-A", "-o", "%cpu=,rss=,comm="]).splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and "ollama" in parts[2].lower():
            cpu += float(parts[0])
            rss += int(parts[1]) * 1024
    return {"cpu": round(cpu, 1), "rss": rss}


def resources(client):
    try:
        loaded = [{"name": m["name"], "size": m.get("size", 0), "vram": m.get("size_vram", 0)} for m in client.loaded()]
        ollama_up = True
    except LLMError:
        loaded, ollama_up = [], False
    return {
        "memory": memory(),
        "load": [round(x, 2) for x in os.getloadavg()],
        "cores": os.cpu_count(),
        "ollama": {**ollama_process(), "up": ollama_up, "loaded": loaded},
    }


def snapshot(hub, client):
    events = hub.events.read(last=2000)
    current = hub.config()["round"]
    starts = [e for e in events if e["type"] == "round_start"]
    running_round = starts[-1]["round"] if starts else None
    in_round = [e for e in events if e.get("round") == running_round]
    ended = any(e["type"] == "round_end" for e in in_round)
    phase = next((e["name"] for e in reversed(in_round) if e["type"] == "phase"), None)
    open_calls = {}
    for e in in_round:
        if e["type"] == "llm_start":
            open_calls[e["call"]] = e
        elif e["type"] in ("llm_end", "llm_error"):
            open_calls.pop(e["call"], None)
    now = time.time()
    ends = [e for e in in_round if e["type"] == "llm_end"]
    per_model = {}
    for e in ends:
        m = per_model.setdefault(e["model"], {"calls": 0, "seconds": 0.0, "output_tokens": 0})
        m["calls"] += 1
        m["seconds"] += e["seconds"]
        m["output_tokens"] += e["output_tokens"]
    for m in per_model.values():
        m["tokens_per_second"] = round(m["output_tokens"] / m["seconds"], 1) if m["seconds"] else 0
        m["seconds"] = round(m["seconds"], 1)
    status = hub.status()
    return {
        "project": hub.project.name,
        "round": running_round if running_round is not None else current,
        "running": bool(starts) and not ended,
        "phase": phase if not ended else "finished",
        "started": starts[-1]["ts"] if starts else None,
        "now": now,
        "tasks": [
            {k: t[k] for k in ("id", "kind", "module", "status", "attempts", "summary")}
            for t in sorted(hub.tasks(), key=lambda t: (t["module"], t["kind"] != "test"))
        ],
        "questions": {"open": len(status["open_questions"]), "guesses": len(status["guesses_to_confirm"])},
        "active": [{**e, "elapsed": round(now - e["ts"], 1)} for e in open_calls.values()],
        "models": per_model,
        "events": [e for e in in_round if e["type"] not in ("llm_start",)][-40:],
        "resources": resources(client),
    }


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Roughcut Local</title>
<style>
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1c1c1a;--muted:#6b6b66;--line:#e3e2dd;--accent:#2f6fde;--ok:#1f8a4c;--warn:#b7791f;--bad:#c53030;--chip:#efeee9}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--panel:#1d1d1b;--ink:#ecebe6;--muted:#9a9993;--line:#2e2e2b;--accent:#6b9cf0;--ok:#4fbf7a;--warn:#e0a84a;--bad:#ef6b6b;--chip:#2a2a27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:20px;margin:0}h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:0 0 10px}
.top{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline;margin-bottom:16px}.sub{color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;min-width:0}
.big{font-size:22px;font-weight:600}.bar{height:6px;background:var(--chip);border-radius:3px;margin-top:8px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px 8px;border-top:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500;border-top:0}
.wrap{overflow-x:auto}.chip{display:inline-block;padding:1px 8px;border-radius:999px;background:var(--chip);font-size:12px}
.done{color:var(--ok)}.in_progress{color:var(--accent)}.failed{color:var(--bad)}.blocked{color:var(--warn)}
code,.mono{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}.log{max-height:320px;overflow:auto}.log div{padding:3px 0;border-top:1px solid var(--line)}
.muted{color:var(--muted)}
</style></head><body><main>
<div class="top"><h1 id="title">Roughcut Local</h1><span class="sub" id="where"></span></div>
<div class="grid">
 <div class="card"><h2>Memory</h2><div class="big" id="mem">-</div><div class="bar"><i id="membar" style="width:0"></i></div></div>
 <div class="card"><h2>CPU load</h2><div class="big" id="load">-</div><div class="sub" id="ocpu"></div></div>
 <div class="card"><h2>Loaded models</h2><div id="loaded" class="mono">-</div></div>
 <div class="card"><h2>Questions</h2><div class="big" id="qs">-</div><div class="sub">open · guesses to confirm</div></div>
</div>
<div class="card" style="margin-bottom:12px"><h2>Running now</h2><div id="active" class="mono muted">Nothing.</div></div>
<div class="card" style="margin-bottom:12px"><h2>Tasks</h2><div class="wrap"><table><thead><tr><th>File</th><th>Job</th><th>Status</th><th>Tries</th><th>Summary</th></tr></thead><tbody id="tasks"></tbody></table></div></div>
<div class="grid">
 <div class="card"><h2>Model speed this round</h2><div class="wrap"><table><thead><tr><th>Model</th><th>Calls</th><th>Time</th><th>tok/s</th></tr></thead><tbody id="models"></tbody></table></div></div>
 <div class="card"><h2>Recent events</h2><div class="log mono" id="log"></div></div>
</div>
</main><script>
const gb=b=>(b/2**30).toFixed(1)+' GB', esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function line(e){const t=new Date(e.ts*1000).toLocaleTimeString();const f={phase:`phase: ${e.name}`,task_start:`start ${e.task} (try ${e.attempt})`,task_end:`${e.task}: ${e.status}. ${e.summary}`,llm_end:`${e.role} ${e.model} ${e.label}: ${e.seconds}s, ${e.tokens_per_second} tok/s`,llm_error:`${e.role} ${e.label} failed: ${e.error}`,tests:`tests ${e.target}: ${e.passed?'pass':'fail'} (${e.seconds}s)`,contract:`contract ${e.path} v${e.version}: ${e.reason}`,question:`${e.blocking?'question':'decision'} ${e.id}: ${e.text}`,note:e.text,round_start:'round started',round_end:'round finished'}[e.type]||e.type;return `<div><span class="muted">${t}</span> ${esc(f)}</div>`}
async function tick(){try{const s=await (await fetch('api/state')).json();
document.getElementById('title').textContent=s.project;
document.getElementById('where').textContent=`Round ${s.round} · ${s.running?('running: '+(s.phase||'starting')):(s.phase||'idle')}`+(s.running&&s.started?` · ${Math.round((s.now-s.started)/60)} min`:'');
const r=s.resources,m=r.memory;if(m){document.getElementById('mem').textContent=`${gb(m.used)} / ${gb(m.total)}`;document.getElementById('membar').style.width=(100*m.used/m.total).toFixed(0)+'%'}
document.getElementById('load').textContent=`${r.load[0]} / ${r.cores} cores`;document.getElementById('ocpu').textContent=r.ollama.up?`Ollama: ${r.ollama.cpu}% CPU, ${gb(r.ollama.rss)} RAM`:'Ollama is not running';
document.getElementById('loaded').innerHTML=r.ollama.loaded.length?r.ollama.loaded.map(x=>`${esc(x.name)} <span class="muted">${gb(x.size)}</span>`).join('<br>'):'<span class="muted">none</span>';
document.getElementById('qs').textContent=`${s.questions.open} · ${s.questions.guesses}`;
document.getElementById('active').innerHTML=s.active.length?s.active.map(a=>`${esc(a.role)} · ${esc(a.model)} · ${esc(a.label)} · ${a.elapsed}s`).join('<br>'):'Nothing.';
document.getElementById('tasks').innerHTML=s.tasks.map(t=>`<tr><td class="mono">${esc(t.module)}</td><td>${t.kind==='test'?'tests':'code'}</td><td><span class="chip ${t.status}">${t.status.replace('_',' ')}</span></td><td>${t.attempts}</td><td>${esc(t.summary)}</td></tr>`).join('')||'<tr><td colspan="5" class="muted">No tasks yet.</td></tr>';
document.getElementById('models').innerHTML=Object.entries(s.models).map(([k,v])=>`<tr><td class="mono">${esc(k)}</td><td>${v.calls}</td><td>${v.seconds}s</td><td>${v.tokens_per_second}</td></tr>`).join('')||'<tr><td colspan="4" class="muted">No calls yet.</td></tr>';
const log=document.getElementById('log');log.innerHTML=s.events.slice().reverse().map(line).join('');
}catch(e){document.getElementById('where').textContent='Status server unreachable'}}
tick();setInterval(tick,2000);
</script></body></html>"""


def serve(hub, client, port=8765, host="127.0.0.1"):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/state"):
                body = json.dumps(snapshot(hub, client)).encode()
                kind = "application/json"
            elif self.path in ("/", "/index.html"):
                body, kind = PAGE.encode(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    return server
