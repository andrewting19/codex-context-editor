"""Core logic for the Codex context editor.

Reads a Codex thread's rollout chain, rebuilds the model-visible context,
and writes an edited copy as a new thread that the Codex app can open.
"""
from __future__ import annotations

import copy
import glob
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone

CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
THREAD_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


# ---------------------------------------------------------------- helpers

def uuid7() -> str:
    ms = int(time.time() * 1000)
    rand = secrets.token_bytes(10)
    b = ms.to_bytes(6, "big") + bytes([0x70 | (rand[0] & 0x0F), rand[1]]) + bytes([0x80 | (rand[2] & 0x3F)]) + rand[3:10]
    return str(uuid.UUID(bytes=b))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def parse_thread_id(text: str) -> str:
    m = THREAD_ID_RE.findall(text or "")
    if not m:
        raise ValueError("No thread id found. Paste a codex://threads/<id> link or a thread id.")
    return m[-1].lower()


def state_db() -> str | None:
    def ver(p: str) -> int:
        m = re.search(r"state_(\d+)\.sqlite$", p)
        return int(m.group(1)) if m else -1
    dbs = sorted(glob.glob(os.path.join(CODEX_HOME, "state_*.sqlite")), key=ver)
    return dbs[-1] if dbs else None


def db_thread_row(tid: str) -> dict | None:
    db = state_db()
    if not db:
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        r = con.execute("select * from threads where id=?", (tid,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def list_threads(query: str = "", limit: int = 60) -> list[dict]:
    """Recent threads for the start page. Uses only columns that exist in this Codex version."""
    db = state_db()
    if not db:
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute("pragma table_info(threads)")}
        want = [c for c in ("id", "name", "title", "first_user_message", "preview", "cwd", "model", "updated_at",
                            "updated_at_ms", "recency_at_ms") if c in cols]
        where, args = [], []
        if "archived" in cols:
            where.append("archived = 0")
        # Hide subagent and review threads. They can still be loaded by id.
        if "agent_role" in cols:
            where.append("(agent_role is null or agent_role = '')")
        if "thread_source" in cols:
            where.append("coalesce(thread_source, '') not in ('subagent', 'guardian_review')")
        if "source" in cols:
            where.append("coalesce(source, '') not like '{%subagent%'")
        q = (query or "").strip()
        if q:
            m = THREAD_ID_RE.search(q)
            if m:
                where, args = ["id = ?"], [m.group(0).lower()]
            else:
                text_cols = [c for c in ("name", "title", "first_user_message", "cwd") if c in cols]
                where.append("(" + " or ".join(f"{c} like ?" for c in text_cols) + ")")
                args += [f"%{q}%"] * len(text_cols)
        # recency_at_ms is the last real activity. updated_at changes for many reasons.
        order = next(c for c in ("recency_at_ms", "updated_at_ms", "updated_at") if c in cols)
        sql = f"select {', '.join(want)} from threads"
        if where:
            sql += " where " + " and ".join(where)
        sql += f" order by {order} desc limit ?"
        rows = con.execute(sql, args + [int(limit)]).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        d = dict(r)
        first = " ".join((d.get("first_user_message") or d.get("preview") or "").split())[:160]
        ts = d.get("recency_at_ms") or d.get("updated_at_ms") or ((d.get("updated_at") or 0) * 1000)
        out.append({"id": d["id"], "title": d.get("name") or d.get("title") or first[:80] or "(untitled)",
                    "first": first, "cwd": d.get("cwd") or "", "model": d.get("model") or "", "updated_ms": ts})
    return out


def find_rollout(tid: str) -> str:
    row = db_thread_row(tid)
    if row and row.get("rollout_path") and os.path.exists(row["rollout_path"]):
        return row["rollout_path"]
    for base in ("sessions", "archived_sessions"):
        hits = glob.glob(os.path.join(CODEX_HOME, base, "**", f"*{tid}.jsonl"), recursive=True)
        if hits:
            return sorted(hits)[-1]
    raise FileNotFoundError(f"No rollout file found for thread {tid}")


def read_lines(path: str, end: int | None = None) -> list[dict]:
    with open(path, "rb") as f:
        data = f.read() if end is None else f.read(end)
    out = []
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            pass  # partial trailing line of a live thread
    return out


def find_base_segment(tid: str, current_path: str) -> str:
    """A history_base names a thread or window id. One thread can have several
    segment files (X.jsonl, X_Y.jsonl, ...). Pick the newest one before the current file."""
    cur = os.path.basename(current_path)
    hits = set()
    for base in ("sessions", "archived_sessions"):
        root = os.path.join(CODEX_HOME, base, "**")
        hits.update(glob.glob(os.path.join(root, f"*{tid}.jsonl"), recursive=True))
        hits.update(glob.glob(os.path.join(root, f"*{tid}_*.jsonl"), recursive=True))
    cands = [h for h in hits if os.path.basename(h) < cur and os.path.abspath(h) != os.path.abspath(current_path)]
    if not cands:
        raise FileNotFoundError(f"No base rollout segment found for {tid}")
    return max(cands, key=os.path.basename)


def load_chain(tid: str) -> list[dict]:
    """All rollout lines that make up a thread, oldest first, parents resolved."""
    return _load_path(find_rollout(tid), None, 0)


def _load_path(path: str, end: int | None, depth: int) -> list[dict]:
    if depth > 50:
        raise RuntimeError("history_base chain is too deep")
    lines = read_lines(path, end)
    if not lines or lines[0].get("type") != "session_meta":
        return lines
    hb = lines[0]["payload"].get("history_base")
    if hb and hb.get("thread_id"):
        base = _load_path(find_base_segment(hb["thread_id"], path), hb.get("end_byte_offset"), depth + 1)
        return [lines[0]] + [l for l in base if l.get("type") != "session_meta"] + lines[1:]
    return lines


# ---------------------------------------------------------------- context rebuild

def item_text(item: dict) -> str:
    t = item.get("type")
    if t in ("message", "agent_message"):
        return "\n".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict) and "text" in c)
    if t in ("function_call_output", "custom_tool_call_output"):
        o = item.get("output")
        if isinstance(o, str):
            return o
        if isinstance(o, dict):
            o = o.get("content", o.get("body", o))
        if isinstance(o, list):
            return "\n".join(c.get("text", "") for c in o if isinstance(c, dict) and "text" in c)
        return json.dumps(o)
    if t == "function_call":
        return item.get("arguments", "")
    if t == "custom_tool_call":
        return item.get("input", "")
    if t == "reasoning":
        return "\n".join(s.get("text", "") for s in item.get("summary") or [] if isinstance(s, dict))
    return ""


CONTEXT_PREFIXES = ("<environment_context", "# AGENTS.md", "<user_instructions", "<permissions", "<turn_aborted",
                    "<skills_instructions", "<collaboration_mode", "<codex_", "<world_state", "<user_shell_command",
                    "<subagent_notification", "<multi_agent", "<app-context", "<personality", "<model_switch",
                    "<external_codex_apps", "<codex_apps")


def is_real_user_message(item: dict) -> bool:
    if item.get("type") != "message" or item.get("role") != "user":
        return False
    txt = item_text(item).lstrip()
    return bool(txt) and not txt.startswith(CONTEXT_PREFIXES)


def effective_history(lines: list[dict], windows: list | None = None) -> list[dict]:
    """Replay the rollout the same way Codex does to get the model-visible items.

    If windows is a list, append to it the full history as it was just
    before each compaction (one entry per context window)."""
    hist: list[dict] = []
    cur_turn = None
    item_turn: dict[str, str] = {}
    for d in lines:
        t, p = d.get("type"), d.get("payload")
        if not isinstance(p, dict):
            continue
        if t == "event_msg" and p.get("type") == "task_started":
            cur_turn = p.get("turn_id") or cur_turn
        elif t == "turn_context":
            cur_turn = p.get("turn_id") or cur_turn
        elif t == "response_item":
            hist.append({"item": p, "turn_id": cur_turn, "ts": d.get("timestamp")})
            if p.get("id"):
                item_turn[p["id"]] = cur_turn
        elif t == "compacted":
            if windows is not None:
                windows.append({"hist": list(hist), "end_ts": d.get("timestamp")})
            rh = p.get("replacement_history")
            if rh is not None:
                hist = [{"item": it, "turn_id": item_turn.get(it.get("id")) or "compacted", "ts": d.get("timestamp"),
                         "from_compaction": True} for it in rh]
            else:
                users = [h for h in hist if is_real_user_message(h["item"])]
                summ = {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": p.get("message", "")}]}
                hist = users + [{"item": summ, "turn_id": "compacted", "ts": d.get("timestamp"), "from_compaction": True}]
        elif t == "event_msg" and p.get("type") == "thread_rolled_back":
            n = int(p.get("num_turns") or 0)
            idxs = [i for i, h in enumerate(hist) if is_real_user_message(h["item"])]
            if n > 0 and idxs:
                hist = hist[: idxs[-n] if n <= len(idxs) else 0]
    return hist


def _preview(hist: list[dict], last: bool) -> str:
    users = [h for h in hist if is_real_user_message(h["item"]) and not h.get("from_compaction")]
    if not users:
        users = [h for h in hist if is_real_user_message(h["item"])]
    if not users:
        return ""
    full = item_text((users[-1] if last else users[0])["item"])
    txt = full
    if "## My request:" in full:
        txt = full.split("## My request:", 1)[1]
    elif "</response-annotations>" in full:
        txt = full.split("</response-annotations>", 1)[1]
    if not txt.strip():
        notes = re.findall(r'"annotation":"((?:[^"\\]|\\.)*)"', full)
        txt = "[annotations] " + " / ".join(notes) if notes else full
    return " ".join(txt.split())[:90]


def load_thread(ref: str, window: int | None = None) -> dict:
    """Load a thread. window selects a context window: 1 is the context just
    before the first compaction; None or the last number is the current context."""
    tid = parse_thread_id(ref)
    lines = load_chain(tid)
    meta = lines[0]["payload"] if lines and lines[0].get("type") == "session_meta" else {}
    snaps: list = []
    current = effective_history(lines, snaps)
    snaps.append({"hist": current, "end_ts": None})
    start_ts = meta.get("timestamp")
    windows = []
    for n, s in enumerate(snaps, 1):
        h = s["hist"]
        new = [x for x in h if not x.get("from_compaction")]
        windows.append({"n": n, "start_ts": start_ts, "end_ts": s["end_ts"], "items": len(h),
                        "current": n == len(snaps), "first": _preview(h, False), "last": _preview(h, True),
                        "new_items": len(new)})
        start_ts = s["end_ts"]
    sel = len(snaps) if not window or window > len(snaps) or window < 1 else int(window)
    hist = snaps[sel - 1]["hist"]
    row = db_thread_row(tid) or {}
    last_tc = next((l["payload"] for l in reversed(lines) if l.get("type") == "turn_context"), {})
    items = []
    for i, h in enumerate(hist):
        it = h["item"]
        items.append({
            "key": f"i{i}",
            "turn_id": h["turn_id"],
            "ts": h["ts"],
            "from_compaction": h.get("from_compaction", False),
            "real_user": is_real_user_message(it),
            "item": it,
        })
    return {
        "id": tid,
        "window": sel,
        "windows": windows,
        "title": row.get("name") or row.get("title") or "",
        "cwd": meta.get("cwd") or row.get("cwd"),
        "model": last_tc.get("model") or row.get("model"),
        "effort": last_tc.get("effort") or row.get("reasoning_effort"),
        "rollout_path": find_rollout(tid),
        "line_count": len(lines),
        "items": items,
    }


# ---------------------------------------------------------------- fork writer

def _strip_reasoning(items: list[dict]) -> list[dict]:
    return [h for h in items if h["item"].get("type") != "reasoning"]


def _fix_tool_pairs(items: list[dict]) -> list[dict]:
    """Drop tool calls without outputs and outputs without calls; the API rejects them."""
    calls, outs = {}, {}
    for h in items:
        it = h["item"]
        cid = it.get("call_id")
        if not cid:
            continue
        if it.get("type", "").endswith("_output"):
            outs[cid] = True
        elif it.get("type", "").endswith("_call") or it.get("type") in ("function_call", "custom_tool_call"):
            calls[cid] = True
    keep = []
    for h in items:
        it = h["item"]
        cid = it.get("call_id")
        typ = it.get("type", "")
        if cid and typ in ("function_call", "custom_tool_call", "local_shell_call") and cid not in outs:
            continue
        if cid and typ.endswith("_output") and cid not in calls:
            continue
        keep.append(h)
    return keep


def write_fork(source_id: str, items: list[dict], model: str | None, effort: str | None,
               strip_reasoning: bool = True) -> dict:
    """Write a new rollout file with the edited items. Returns {id, path}."""
    lines = load_chain(source_id)
    meta = copy.deepcopy(lines[0]["payload"])
    last_tc = copy.deepcopy(next((l["payload"] for l in reversed(lines) if l.get("type") == "turn_context"), None))
    last_settings = copy.deepcopy(next((l["payload"] for l in reversed(lines)
                                        if l.get("type") == "event_msg" and l["payload"].get("type") == "thread_settings_applied"), None))

    if strip_reasoning:
        items = _strip_reasoning(items)
    items = _fix_tool_pairs(items)

    new_id = uuid7()
    now = datetime.now()
    for k in ("history_base", "context_window", "forked_from_ordinal_exclusive", "forked_from_id"):
        meta.pop(k, None)
    meta.update({"id": new_id, "session_id": new_id, "timestamp": now_iso(), "thread_source": "user"})

    out: list[dict] = []

    def emit(typ: str, payload: dict):
        out.append({"timestamp": now_iso(), "ordinal": len(out), "type": typ, "payload": payload})

    emit("session_meta", meta)
    if last_settings:
        last_settings["thread_id"] = new_id
        ts = last_settings.get("thread_settings", {})
        if model:
            ts["model"] = model
            cm = ts.get("collaboration_mode", {}).get("settings")
            if isinstance(cm, dict):
                cm["model"] = model
        if effort:
            ts["reasoning_effort"] = effort
            cm = ts.get("collaboration_mode", {}).get("settings")
            if isinstance(cm, dict):
                cm["reasoning_effort"] = effort
        emit("event_msg", last_settings)

    # group consecutive items by original turn
    groups: list[list[dict]] = []
    prev = object()
    for h in items:
        if h.get("turn_id") != prev or not groups:
            groups.append([])
            prev = h.get("turn_id")
        groups[-1].append(h)

    started = int(time.time())
    for g in groups:
        turn_id = uuid7()
        emit("event_msg", {"type": "task_started", "turn_id": turn_id, "root_turn_id": turn_id, "started_at": started,
                           "model_context_window": None, "collaboration_mode_kind": "default"})
        if last_tc:
            tc = copy.deepcopy(last_tc)
            tc["turn_id"] = turn_id
            tc["root_turn_id"] = turn_id
            if model:
                tc["model"] = model
            if effort:
                tc["effort"] = effort
            emit("turn_context", tc)
        last_agent = None
        for h in g:
            it = copy.deepcopy(h["item"])
            it.pop("internal_chat_message_metadata_passthrough", None)
            if it.get("type") == "message" and not it.get("id"):
                it["id"] = "msg_" + uuid7()
            emit("response_item", it)
            ms = int(time.time() * 1000)
            if is_real_user_message(it):
                emit("event_msg", {"type": "item_completed", "thread_id": new_id, "turn_id": turn_id,
                                   "item": {"type": "UserMessage", "id": uuid7(),
                                            "content": [{"type": "text", "text": item_text(it), "text_elements": []}]},
                                   "started_at_ms": ms, "completed_at_ms": ms})
            elif it.get("type") == "message" and it.get("role") == "assistant":
                last_agent = item_text(it)
                emit("event_msg", {"type": "item_completed", "thread_id": new_id, "turn_id": turn_id,
                                   "item": {"type": "AgentMessage", "id": it["id"],
                                            "content": [{"type": "Text", "text": last_agent}],
                                            "phase": it.get("phase") or "final_answer"},
                                   "started_at_ms": ms, "completed_at_ms": ms})
        emit("event_msg", {"type": "task_complete", "turn_id": turn_id, "last_agent_message": last_agent,
                           "started_at": started, "completed_at": started, "duration_ms": 0})

    day_dir = os.path.join(CODEX_HOME, "sessions", now.strftime("%Y"), now.strftime("%m"), now.strftime("%d"))
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{new_id}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for o in out:
            f.write(json.dumps(o, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {"id": new_id, "path": path, "cwd": meta.get("cwd"), "item_count": len(items)}
