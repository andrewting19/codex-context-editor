#!/usr/bin/env python3
"""Codex Context Editor: a local web page to edit a Codex thread's context and fork it."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ctxedit  # noqa: E402
from appserver import AppServer, AppServerError  # noqa: E402

MAX_BODY = 200 * 1024 * 1024
_models_cache: dict = {"t": 0, "data": None}
_as_lock = threading.Lock()
ALLOWED_HOSTS: set[str] = set()


def with_app_server(fn):
    with _as_lock:
        s = AppServer()
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


def can_open_links() -> bool:
    """True when this machine can open a codex:// link in a desktop app."""
    if platform.system() == "Darwin":
        return True
    if platform.system() == "Windows":
        return True
    return bool(shutil.which("xdg-open") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")))


def open_link(link: str) -> None:
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", link])
    elif system == "Windows":
        os.startfile(link)  # type: ignore[attr-defined]
    elif can_open_links():
        subprocess.Popen(["xdg-open", link], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def register_fork(thread_id: str, name: str) -> tuple[int | None, list[str]]:
    """Make Codex index the new rollout file. Returns (turn count, warnings)."""
    warnings: list[str] = []

    def run(s: AppServer):
        s.request("thread/read", {"threadId": thread_id})
        if name:
            try:
                s.request("thread/name/set", {"threadId": thread_id, "name": name})
            except AppServerError as e:
                warnings.append(f"Could not set the name: {e}")
        turns = None
        try:
            # A resume builds the turn index that the Codex app shows as the transcript.
            s.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
            # The list is paged. Follow the cursor to count all turns.
            turns, cursor = 0, None
            for _ in range(500):
                params = {"threadId": thread_id}
                if cursor:
                    params["cursor"] = cursor
                page = s.request("thread/turns/list", params)
                turns += len(page.get("data", []))
                cursor = page.get("nextCursor")
                if not cursor or not page.get("data"):
                    break
        except AppServerError as e:
            warnings.append(f"Could not build the transcript index: {e}")
        try:
            s.request("thread/unsubscribe", {"threadId": thread_id})
        except AppServerError:
            pass
        return turns

    turns = with_app_server(run)
    return turns, warnings


def do_fork(body: dict) -> dict:
    src = ctxedit.parse_thread_id(body["source_id"])
    items = body.get("items") or []
    if not items:
        raise ValueError("Nothing to fork: every item is removed.")
    res = ctxedit.write_fork(src, items, body.get("model") or None, body.get("effort") or None,
                             strip_reasoning=bool(body.get("strip_reasoning", True)))
    res["turns"], res["warnings"] = register_fork(res["id"], (body.get("name") or "").strip())
    res["link"] = f"codex://threads/{res['id']}"
    if body.get("open") and can_open_links():
        open_link(res["link"])
    return res


def get_info() -> dict:
    try:
        from appserver import find_codex
        codex = find_codex()
    except Exception as e:
        codex = None
        err = str(e)
    else:
        err = None
    return {"codex_home": ctxedit.CODEX_HOME, "codex_bin": codex, "codex_error": err,
            "can_open_links": can_open_links()}


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexContextEditor"

    def log_message(self, fmt, *args):
        sys.stderr.write("[context-editor] " + (fmt % args) + "\n")

    def _send(self, code, obj=None, ctype="application/json", raw: bytes | None = None):
        data = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        # Block DNS rebinding: other sites must not reach this server under their own host name.
        host = (self.headers.get("Host") or "").lower()
        # Remove the port. Any port is allowed, so SSH tunnels to another local port work.
        name = host[1:].split("]", 1)[0] if host.startswith("[") else host.rsplit(":", 1)[0] if ":" in host else host
        if name not in ALLOWED_HOSTS:
            self._send(403, {"error": "Host not allowed"})
            return False
        return True

    def _guard(self, fn):
        try:
            self._send(200, fn())
        except Exception as e:
            traceback.print_exc()
            self._send(400, {"error": str(e)})

    def do_GET(self):
        if not self._host_ok():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, raw=f.read(), ctype="text/html; charset=utf-8")
        if u.path == "/api/info":
            return self._guard(get_info)
        if u.path == "/api/models":
            return self._guard(get_models)
        if u.path == "/api/threads":
            return self._guard(lambda: ctxedit.list_threads(q.get("q", [""])[0]))
        if u.path == "/api/thread":
            w = q.get("window", [""])[0]
            return self._guard(lambda: ctxedit.load_thread(q.get("ref", [""])[0], int(w) if w.isdigit() else None))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return
        # Only accept JSON. Browsers must send a CORS preflight for this type, which this server never approves,
        # so other web pages cannot make forks.
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._send(415, {"error": "Content-Type must be application/json"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._send(413, {"error": "Request too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "Invalid JSON"})
        if urlparse(self.path).path == "/api/fork":
            return self._guard(lambda: do_fork(body))
        self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description="Edit a Codex thread's context and fork it.")
    ap.add_argument("thread", nargs="?", help="Thread id or codex://threads/<id> link to open")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CTX_EDITOR_PORT", "7317")))
    ap.add_argument("--host", default="127.0.0.1", help="Address to listen on (default 127.0.0.1)")
    ap.add_argument("--no-browser", action="store_true", help="Do not open a browser window")
    args = ap.parse_args()

    ALLOWED_HOSTS.update({"127.0.0.1", "localhost", "::1", args.host.lower()})
    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        sys.exit(f"Could not listen on {args.host}:{args.port} ({e}). Use --port to choose another port.")
    url = f"http://127.0.0.1:{args.port}/"
    if args.thread:
        url += "?thread=" + ctxedit.parse_thread_id(args.thread)
    info = get_info()
    print(f"Codex Context Editor: {url}")
    print(f"  Codex home: {info['codex_home']}")
    print(f"  codex binary: {info['codex_bin'] or 'NOT FOUND - ' + str(info['codex_error'])}")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
