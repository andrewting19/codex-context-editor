"""Minimal JSON-RPC client for the Codex app-server."""
import json, os, queue, subprocess, threading, time

CODEX_BIN = os.environ.get("CODEX_BIN") or "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"


class AppServer:
    def __init__(self, mode: str = "proxy"):
        args = [CODEX_BIN, "app-server", "proxy"] if mode == "proxy" else [CODEX_BIN, "app-server", "--listen", "stdio://"]
        self.p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, bufsize=1)
        self.q: "queue.Queue[dict]" = queue.Queue()
        self.notes: list[dict] = []
        self.nid = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()
        self.request("initialize", {"clientInfo": {"name": "context_editor", "title": "Context Editor", "version": "0.1"},
                                    "capabilities": {"experimentalApi": True}})
        self._send({"jsonrpc": "2.0", "method": "initialized"})

    def _read(self):
        for line in self.p.stdout:
            try:
                self.q.put(json.loads(line))
            except json.JSONDecodeError:
                pass

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def request(self, method, params, timeout=120):
        with self.lock:
            self.nid += 1
            rid = self.nid
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            end = time.time() + timeout
            while True:
                msg = self.q.get(timeout=max(0.1, end - time.time()))
                if msg.get("id") == rid and "method" not in msg:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                    return msg.get("result")
                if "method" in msg and "id" in msg:
                    # server-to-client request (approval etc.): decline politely
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}})
                else:
                    self.notes.append(msg)

    def wait_note(self, method, pred=lambda p: True, timeout=300):
        end = time.time() + timeout
        for n in self.notes:
            if n.get("method") == method and pred(n.get("params", {})):
                return n
        while time.time() < end:
            try:
                msg = self.q.get(timeout=max(0.1, end - time.time()))
            except queue.Empty:
                break
            self.notes.append(msg)
            if msg.get("method") == method and pred(msg.get("params", {})):
                return msg
        raise TimeoutError(method)

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=3)
        except Exception:
            self.p.kill()

