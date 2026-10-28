"""The studio: one local web page for everything. `roughcut-local studio` opens it.

The page talks only to the desk (desk.py), never to the hub, over a small JSON API on
127.0.0.1. Two guards keep other things off it: requests must name this server as their
Host (so a web page can't reach it through a DNS trick), and every change needs a token
that is only in the page this server hands out (so another site can't post to it).
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import project
from .desk import Desk
from .hub_local import HubError
from .llm import LLMError, OllamaClient
from .status import resources, snapshot

PAGE = Path(__file__).with_name("studio.html")
SCREEN = re.compile(r"^[a-z0-9-]+\.png$")


def projects():
    rows = sorted(project.registry().items(), key=lambda kv: kv[1]["updated"], reverse=True)
    return [{"path": p, **e} for p, e in rows if Path(p).exists()]


def full_state(desk):
    state = desk.state()
    hub = desk.hub
    state["projects"] = projects()
    if hub:
        state["status"] = snapshot(hub, desk.llm_for(hub))
        state["screens"] = sorted(p.name for p in (hub.root / "screens").glob("*.png")) if (hub.root / "screens").exists() else []
        state["questions"] = [q for q in hub.questions() if q["status"] == "open" or (q["status"] == "decided" and q["critical"])]
    else:
        state["status"] = {"resources": resources(OllamaClient())}
        state["screens"], state["questions"] = [], []
    return state


def make_server(desk, port=8770, host="127.0.0.1"):
    token = secrets.token_urlsafe(24)
    allowed_hosts = set()  # filled in once the port is known

    actions = {
        "open": lambda b: desk.open(b["path"]),
        "new": lambda b: desk.new(b["idea"], b.get("stack", "web")),
        "bring-in": lambda b: desk.bring_in(b["target"]),
        "say": lambda b: desk.say(b["text"]),
        "answer": lambda b: desk.answer(int(b["index"]), b.get("reply", "")),
        "keep-talking": lambda b: desk.keep_talking(),
        "build": lambda b: desk.build(int(b["rounds"]) if b.get("rounds") else None),
        "stop": lambda b: desk.stop(),
        "question": lambda b: desk.answer_question(b["id"], b.get("text"), bool(b.get("keep"))),
        "app": lambda b: desk.app_url(),
        "model": lambda b: desk.set_model(b["role"], b["model"]),
    }

    class Handler(BaseHTTPRequestHandler):
        def send(self, code, body, kind="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def host_ok(self):
            if self.headers.get("Host") in allowed_hosts:
                return True
            self.send(403, {"error": "wrong host"})
            return False

        def do_GET(self):
            if not self.host_ok():
                return
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                html = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", token)
                self.send(200, html.encode(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self.send(200, full_state(desk))
            elif path == "/api/models":
                self.send(200, desk.models())
            elif path.startswith("/screens/") and desk.hub:
                name = path.rsplit("/", 1)[1]
                file = desk.hub.root / "screens" / name
                if SCREEN.match(name) and file.exists():
                    self.send(200, file.read_bytes(), "image/png")
                else:
                    self.send(404, {"error": "no such screenshot"})
            else:
                self.send(404, {"error": "not found"})

        def do_POST(self):
            if not self.host_ok():
                return
            if not secrets.compare_digest(self.headers.get("X-Token", ""), token):
                self.send(403, {"error": "missing or wrong token"})
                return
            name = self.path.removeprefix("/api/")
            if name not in actions:
                self.send(404, {"error": f"no action {name}"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                result = actions[name](body)
                self.send(200, result)
            except (HubError, LLMError, RuntimeError, KeyError, ValueError, IndexError) as e:
                self.send(400, {"error": str(e)})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    real = server.server_address[1]
    allowed_hosts.update({f"127.0.0.1:{real}", f"localhost:{real}"})
    server.token = token
    return server


def main(args):
    desk = Desk()
    if args.project_path:
        desk.open(args.project_path)
    server = make_server(desk, args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Roughcut Local studio: {url}  (Ctrl-C to stop)", flush=True)
    if not args.no_open:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        desk.stop_app()
    return {"stopped": True}
