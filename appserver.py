"""Small JSON-RPC client for "codex app-server" over stdio."""
from __future__ import annotations

import json
import os
import platform
import queue
import shutil
import subprocess
import threading
import time

# Places where a Codex binary is often found, after $CODEX_BIN and $PATH.
_MAC_APP_BINS = [
    "/Applications/Codex.app/Contents/Resources/codex",
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
    os.path.expanduser("~/Applications/Codex.app/Contents/Resources/codex"),
]


def find_codex() -> str:
    """Return the path of the codex binary, or raise a clear error."""
    env = os.environ.get("CODEX_BIN")
    if env:
        if not os.path.exists(env):
            raise FileNotFoundError(f"CODEX_BIN is set to {env}, but that file does not exist.")
        return env
    on_path = shutil.which("codex")
    if on_path:
        return on_path
    if platform.system() == "Darwin":
        for p in _MAC_APP_BINS:
            if os.path.exists(p):
                return p
    raise FileNotFoundError("Could not find the codex binary. Install the Codex CLI, or set CODEX_BIN.")


class AppServerError(RuntimeError):
    pass


class AppServer:
    """Start a private "codex app-server" process and talk JSON-RPC to it."""

    def __init__(self, codex_bin: str | None = None, timeout: float = 60):
        self.bin = codex_bin or find_codex()
        self.p = subprocess.Popen([self.bin, "app-server", "--listen", "stdio://"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, bufsize=1)
        self.q: queue.Queue = queue.Queue()
        self.notes: list[dict] = []
        self.nid = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()
        self.info = self.request("initialize", {
            "clientInfo": {"name": "codex_context_editor", "title": "Codex Context Editor", "version": "0.2"},
            "capabilities": {"experimentalApi": True}}, timeout=timeout)
        self._send({"jsonrpc": "2.0", "method": "initialized"})

    def _read(self):
        for line in self.p.stdout:
            try:
                self.q.put(json.loads(line))
            except json.JSONDecodeError:
                pass
        self.q.put({"_eof": True})

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def _next(self, end: float) -> dict:
        try:
            msg = self.q.get(timeout=max(0.1, end - time.time()))
        except queue.Empty:
            raise TimeoutError("codex app-server did not answer in time")
        if msg.get("_eof"):
            raise AppServerError("codex app-server stopped. Run 'codex app-server' by hand to see the error.")
        return msg

    def request(self, method: str, params: dict, timeout: float = 120):
        with self.lock:
            self.nid += 1
            rid = self.nid
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            end = time.time() + timeout
            while True:
                msg = self._next(end)
                if msg.get("id") == rid and "method" not in msg:
                    if "error" in msg:
                        raise AppServerError(f"{method}: {msg['error'].get('message')}")
                    return msg.get("result")
                if "method" in msg and "id" in msg:
                    # A request from the server (for example an approval). This client does not support them.
                    self._send({"jsonrpc": "2.0", "id": msg["id"],
                                "error": {"code": -32601, "message": "not supported by this client"}})
                else:
                    self.notes.append(msg)

    def wait_note(self, method: str, pred=lambda p: True, timeout: float = 300) -> dict:
        for n in self.notes:
            if n.get("method") == method and pred(n.get("params", {})):
                return n
        end = time.time() + timeout
        while True:
            msg = self._next(end)
            self.notes.append(msg)
            if msg.get("method") == method and pred(msg.get("params", {})):
                return msg

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=3)
        except Exception:
            self.p.kill()

