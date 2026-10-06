#!/usr/bin/env python3
"""Codex Context Editor: local web UI to edit a thread's context and fork it."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ctxedit  # noqa: E402
from appserver import AppServer  # noqa: E402

PORT = int(os.environ.get("CTX_EDITOR_PORT", "7317"))
_models_cache: dict = {"t": 0, "data": None}
_as_lock = threading.Lock()


def with_app_server(fn):
    with _as_lock:
        s = AppServer("private")
        try:
            return fn(s)
        finally:
            s.close()


def get_models():
    if _models_cache["data"] and time.time() - _models_cache["t"] < 600:
        return _models_cache["data"]
    res = with_app_server(lambda s: s.request("model/list", {}))
    data = [{"id": m.get("model") or m.get("id"), "name": m.get("displayName"),
             "efforts": [e.get("reasoningEffort") for e in m.get("supportedReasoningEfforts") or []],
             "defaultEffort": m.get("defaultReasoningEffort"), "isDefault": m.get("isDefault")}
            for m in res.get("data", []) if not m.get("hidden")]
    _models_cache.update(t=time.time(), data=data)
    return data


def do_fork(body: dict) -> dict:
    src = ctxedit.parse_thread_id(body["source_id"])
    items = body["items"]
    if not items:
        raise ValueError("Nothing to fork: every item is removed.")
    model, effort = body.get("model") or None, body.get("effort") or None
    res = ctxedit.write_fork(src, items, model, effort, strip_reasoning=bool(body.get("strip_reasoning", True)))
    name = (body.get("name") or "").strip()

    def register(s: AppServer):
        s.request("thread/read", {"threadId": res["id"]})
        if name:
            s.request("thread/name/set", {"threadId": res["id"], "name": name})
        # One resume builds the turn index that the Codex app shows as the transcript.
        s.request("thread/resume", {"threadId": res["id"], "excludeTurns": True})
        turns = s.request("thread/turns/list", {"threadId": res["id"]})
        try:
            s.request("thread/unsubscribe", {"threadId": res["id"]})
        except Exception:
            pass
        return len(turns.get("data", []))

    res["turns"] = with_app_server(register)
    res["link"] = f"codex://threads/{res['id']}"
    if body.get("open", True):
        subprocess.Popen(["open", res["link"]])
    return res


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[ctx-editor] " + (fmt % args) + "\n")

    def _send(self, code, obj=None, ctype="application/json", raw: bytes | None = None):
        data = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _guard(self, fn):
        try:
            self._send(200, fn())
        except Exception as e:  # report errors to the page
            traceback.print_exc()
            self._send(400, {"error": str(e)})

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, raw=f.read(), ctype="text/html; charset=utf-8")
        if u.path == "/api/models":
            return self._guard(get_models)
        if u.path == "/api/thread":
            w = q.get("window", [""])[0]
            return self._guard(lambda: ctxedit.load_thread(q.get("ref", [""])[0], int(w) if w.isdigit() else None))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if u.path == "/api/fork":
            return self._guard(lambda: do_fork(body))
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Context Editor running at http://127.0.0.1:{PORT}")
    srv.serve_forever()
