"""T3 Code support: find the Codex or Claude session behind a T3 Code thread, and make a
T3 Code thread for a fork so it opens in the T3 Code app.

T3 Code (https://github.com/pingdotgg/t3code) runs Codex or Claude Code under the hood and keeps
its own thread ids. Its database links each T3 thread to the provider's native thread id. To show
a fork in T3 Code, the editor sends T3's own "thread.create" command with "importedNativeThread"
set to the fork's id. This is the same command T3 uses to import sessions. T3 then reads the
transcript from the provider when you open the thread.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import sqlite3
import struct
import subprocess
import uuid

T3_HOME = os.path.expanduser(os.environ.get("T3CODE_HOME") or "~/.t3")
USERDATA = os.path.join(T3_HOME, "userdata")
DB = os.path.join(USERDATA, "statev2.sqlite")
# T3 driver names for the editor's sources.
DRIVERS = {"codex": "codex", "claudeAgent": "claude"}


def available() -> bool:
    return os.path.exists(DB)


def _db():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _native(row) -> tuple[str | None, str | None]:
    try:
        ref = json.loads(row["payload_json"]).get("nativeThreadRef") or {}
    except (TypeError, ValueError):
        return None, None
    return ref.get("nativeId"), ref.get("driver")


def threads() -> list[dict]:
    """Every T3 thread that is linked to a Codex or Claude session."""
    if not available():
        return []
    out = []
    try:
        con = _db()
        rows = con.execute("""
            select t.thread_id, t.title, t.project_id, t.updated_at, t.payload_json as tp, p.payload_json,
                   pr.workspace_root
            from orchestration_v2_projection_threads t
            join orchestration_v2_projection_provider_threads p on p.provider_thread_id = t.active_provider_thread_id
            left join projection_projects pr on pr.project_id = t.project_id
            where t.deleted_at is null""").fetchall()
    except sqlite3.Error:
        return []
    for r in rows:
        nid, driver = _native(r)
        if not nid or driver not in DRIVERS:
            continue
        try:
            tp = json.loads(r["tp"])
        except (TypeError, ValueError):
            tp = {}
        out.append({"t3_id": r["thread_id"], "title": r["title"], "project_id": r["project_id"],
                    "workspace_root": r["workspace_root"], "native_id": nid, "source": DRIVERS[driver],
                    "driver": driver, "updated_at": r["updated_at"],
                    "model_selection": tp.get("modelSelection") or {}})
    return out


def find(tid: str) -> dict | None:
    """The T3 thread with this T3 id or with this native (Codex or Claude) id."""
    tid = tid.lower()
    for t in threads():
        if t["t3_id"].lower() == tid or t["native_id"].lower() == tid:
            return t
    return None


def environment_id() -> str | None:
    try:
        with open(os.path.join(USERDATA, "environment-id")) as f:
            return f.read().strip() or None
    except OSError:
        return None


def link(thread_id: str) -> str | None:
    env = environment_id()
    return f"t3code://threads/{env}/{thread_id}" if env else None


def _runtime() -> dict:
    with open(os.path.join(USERDATA, "server-runtime.json")) as f:
        return json.load(f)


def _cli() -> str:
    for p in (os.path.join(T3_HOME, "bin", "t3"), shutil.which("t3")):
        if p and os.access(p, os.X_OK):
            return p
    raise RuntimeError("The t3 command was not found. Open T3 Code once so it installs ~/.t3/bin/t3.")


def _t3(*args: str) -> str:
    env = dict(os.environ, T3CODE_HOME=T3_HOME)
    r = subprocess.run([_cli(), *args], capture_output=True, text=True, timeout=60, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"t3 {' '.join(args[:3])} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout


# ---------------------------------------------------------------- minimal WebSocket client

class _WS:
    def __init__(self, host: str, port: int, path: str, headers: dict):
        self.s = socket.create_connection((host, port), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        req = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}", "Upgrade: websocket", "Connection: Upgrade",
               f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"] + [f"{k}: {v}" for k, v in headers.items()]
        self.s.sendall(("\r\n".join(req) + "\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.s.recv(4096)
            if not chunk:
                raise RuntimeError("T3 Code closed the connection.")
            buf += chunk
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status + " ":
            raise RuntimeError(f"T3 Code refused the connection: {status} {self.buf[:300].decode(errors='replace')}")

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise RuntimeError("T3 Code closed the connection.")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text: str, opcode: int = 1):
        data = text.encode() if isinstance(text, str) else text
        mask = os.urandom(4)
        n = len(data)
        head = bytes([0x80 | opcode])
        head += bytes([0x80 | n]) if n < 126 else (bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536
                                                      else bytes([0x80 | 127]) + struct.pack(">Q", n))
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> str:
        parts = b""
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            data = self._read(n)
            op = b0 & 0x0F
            if op == 9:
                self.send(data, 10)
                continue
            if op == 8:
                raise RuntimeError("T3 Code closed the connection.")
            if op in (0, 1, 2):
                parts += data
                if b0 & 0x80:
                    return parts.decode()

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


def _dispatch(command: dict) -> dict:
    rt = _runtime()
    issued = json.loads(_t3("auth", "session", "issue", "--scope", "orchestration:read", "--scope",
                            "orchestration:operate", "--ttl", "5m", "--label", "Context Editor", "--json"))
    token = issued.get("token") or issued.get("accessToken") or issued.get("credential")
    if not token:
        raise RuntimeError("t3 auth session issue gave no token.")
    try:
        ws = _WS(rt.get("host") or "127.0.0.1", int(rt["port"]), "/ws?orchestrationProtocol=2",
                 {"Authorization": f"Bearer {token}", "Origin": rt.get("origin") or f"http://127.0.0.1:{rt['port']}"})
        try:
            ws.send(json.dumps({"_tag": "Request", "id": "1", "tag": "orchestration.dispatchCommand",
                                "payload": command, "headers": []}))
            while True:
                msg = json.loads(ws.recv())
                for m in msg if isinstance(msg, list) else [msg]:
                    if m.get("_tag") == "Ping":
                        ws.send(json.dumps({"_tag": "Pong"}))
                    elif m.get("_tag") == "Exit" and str(m.get("requestId")) == "1":
                        ex = m.get("exit") or {}
                        if ex.get("_tag") != "Success":
                            raise RuntimeError("T3 Code rejected the new thread: " + json.dumps(ex)[:600])
                        return ex.get("value") or {}
                    elif m.get("_tag") == "Defect":
                        raise RuntimeError("T3 Code error: " + json.dumps(m)[:600])
        finally:
            ws.close()
    finally:
        sid = issued.get("sessionId") or issued.get("id")
        if sid:
            try:
                _t3("auth", "session", "revoke", sid)
            except Exception:
                pass


def create_thread(src: dict, native_id: str, title: str, model: str | None) -> dict:
    """Make a T3 thread in the source's project that shows the fork. Returns {thread_id, link}."""
    ms = dict(src.get("model_selection") or {})
    ms.setdefault("instanceId", src["driver"])
    if model:
        ms["model"] = model
    tid = str(uuid.uuid4())
    _dispatch({"type": "thread.create", "commandId": str(uuid.uuid4()), "threadId": tid,
               "projectId": src["project_id"], "title": (title or "Edited fork")[:200],
               "modelSelection": ms, "runtimeMode": "full-access", "interactionMode": "default",
               "branch": None, "worktreePath": None,
               "importedNativeThread": {"ref": {"driver": src["driver"], "nativeId": native_id, "strength": "strong"}},
               "createdBy": "user", "creationSource": "web"})
    return {"thread_id": tid, "link": link(tid)}

