"""Read Claude Code transcripts and write edited forks of them.

Claude Code saves each session as JSON lines in ~/.claude/projects/<folder>/<session-id>.jsonl.
Message lines form a tree through uuid and parentUuid. A compaction starts a new chain with a
"compact_boundary" line; its logicalParentUuid points back to the end of the older chain.

The editor works on Codex-shaped items. This module converts each Claude line into such items
and keeps the source line's uuid on each item ("cc_uuid"). When it writes a fork, a line whose
items did not change is copied as it is; a changed line gets new content built from its items.
"""
from __future__ import annotations

import copy
import glob
import json
import os
import re
import uuid
from datetime import datetime, timezone

CLAUDE_HOME = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")
PROJECTS = os.path.join(CLAUDE_HOME, "projects")
MESSAGE_TYPES = ("user", "assistant", "attachment", "system")
SUMMARY_PREFIX = "This session is being continued from a previous conversation"
# Claude Code model aliases for the fork's resume command.
MODELS = [
    {"id": "opus", "name": "Opus (latest)"},
    {"id": "sonnet", "name": "Sonnet (latest)"},
    {"id": "haiku", "name": "Haiku (latest)"},
    {"id": "opus[1m]", "name": "Opus, 1M context"},
    {"id": "sonnet[1m]", "name": "Sonnet, 1M context"},
]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def models() -> list[dict]:
    return [dict(m, efforts=EFFORTS, defaultEffort="high", isDefault=m["id"] == "opus") for m in MODELS]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}Z"


# ---------------------------------------------------------------- files

def _skip_dir(name: str) -> bool:
    # Temporary folders hold short helper sessions (for example title generators) and not user work.
    return name.startswith("-private-var-folders") or name.startswith("-var-folders") or name.startswith("-tmp-")


def find_session(sid: str) -> str | None:
    if not os.path.isdir(PROJECTS):
        return None
    hits = glob.glob(os.path.join(glob.escape(PROJECTS), "*", sid + ".jsonl"))
    return hits[0] if hits else None


def project_cwd(path: str, lines: list[dict]) -> str | None:
    """The folder that Claude Code files this session under. "claude --resume" must run there.
    The project folder name is that path with every non-alphanumeric character changed to "-"."""
    folder = os.path.basename(os.path.dirname(path))
    cwds = [l["cwd"] for l in lines if isinstance(l.get("cwd"), str)]
    for c in dict.fromkeys(cwds):
        if re.sub(r"[^A-Za-z0-9]", "-", c) == folder:
            return c
    return cwds[0] if cwds else None


def read_file(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return out


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def is_real_user(line: dict) -> bool:
    """A prompt the person typed (not a tool result, summary, hidden note, or command echo)."""
    if line.get("type") != "user" or line.get("isMeta") or line.get("isCompactSummary"):
        return False
    content = line.get("message", {}).get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    t = _text_of(content).lstrip()
    if not t and not (isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "image" for b in content)):
        return False
    return not t.startswith(("<command-", "<local-command-", "<bash-", "<task-notification"))


_list_cache: dict = {}


def _summary(path: str) -> dict | None:
    st = os.stat(path)
    hit = _list_cache.get(path)
    if hit and hit[0] == st.st_mtime:
        return hit[1]
    title = ai_title = first = cwd = model = None
    last_ts = None
    msgs = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            if '"custom-title"' in raw or '"ai-title"' in raw or '"agent-name"' in raw:
                try:
                    l = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if l.get("type") == "custom-title":
                    title = l.get("customTitle") or title
                elif l.get("type") == "ai-title":
                    ai_title = l.get("aiTitle") or ai_title
                elif l.get("type") == "agent-name" and not ai_title:
                    ai_title = l.get("agentName")
                continue
            if not raw.startswith('{"parentUuid"') and '"type":"user"' not in raw and '"type":"assistant"' not in raw:
                continue
            try:
                l = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if l.get("isSidechain") or l.get("type") not in ("user", "assistant"):
                continue
            msgs += 1
            cwd = cwd or l.get("cwd")
            last_ts = l.get("timestamp") or last_ts
            if l.get("type") == "assistant":
                model = l.get("message", {}).get("model") or model
            elif first is None and is_real_user(l):
                first = _text_of(l["message"].get("content")).strip()
    if not msgs:
        res = None
    else:
        first = re.sub(r"\s+", " ", first or "")[:200]
        # Claude Code appends bookkeeping lines (for example last-prompt) to old sessions,
        # so the file time can be much newer than the last message. Use the message time.
        updated = _ts_ms(last_ts) or int(st.st_mtime * 1000)
        res = {"id": os.path.basename(path)[:-6], "title": title or ai_title or first[:80] or "(untitled)",
               "first": first, "cwd": cwd, "model": model if model and not model.startswith("<") else None,
               "updated_ms": updated, "source": "claude"}
    _list_cache[path] = (st.st_mtime, res)
    return res


def _ts_ms(ts) -> int | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def list_sessions(query: str = "", limit: int = 60) -> list[dict]:
    if not os.path.isdir(PROJECTS):
        return []
    files = []
    for d in os.listdir(PROJECTS):
        full = os.path.join(PROJECTS, d)
        if _skip_dir(d) or not os.path.isdir(full):
            continue
        for name in os.listdir(full):
            if name.endswith(".jsonl"):
                p = os.path.join(full, name)
                try:
                    files.append((os.path.getmtime(p), p))
                except OSError:
                    pass
    files.sort(reverse=True)
    q = (query or "").lower().strip()
    out = []
    for i, (_, p) in enumerate(files):
        # The file time is never older than the last message, so once we have enough sessions
        # that are newer than the next file, no later file can enter the list.
        if len(out) >= limit:
            newest = sorted((s["updated_ms"] for s in out), reverse=True)
            if newest[limit - 1] >= files[i][0] * 1000:
                break
        try:
            s = _summary(p)
        except OSError:
            continue
        if not s:
            continue
        if q and q not in " ".join(str(s.get(k) or "") for k in ("id", "title", "first", "cwd")).lower():
            continue
        out.append(s)
    out.sort(key=lambda s: s["updated_ms"], reverse=True)
    return out[:limit]


# ---------------------------------------------------------------- chains and windows

def _chain(by_uuid: dict, leaf: str | None) -> list[dict]:
    out, seen = [], set()
    cur = by_uuid.get(leaf)
    while cur is not None and cur["uuid"] not in seen:
        seen.add(cur["uuid"])
        out.append(cur)
        cur = by_uuid.get(cur.get("parentUuid"))
    out.reverse()
    return out


def _splice_preserved(chain: list[dict], by_uuid: dict) -> list[dict]:
    """After a partial compaction Claude Code keeps some recent messages. It loads them right after the summary."""
    if not chain or chain[0].get("subtype") != "compact_boundary":
        return chain
    pm = (chain[0].get("compactMetadata") or {}).get("preservedMessages") or {}
    keep = [by_uuid[u] for u in pm.get("uuids") or [] if u in by_uuid]
    anchor = pm.get("anchorUuid")
    if not keep or not anchor:
        return chain
    ids = {l["uuid"] for l in keep}
    rest = [l for l in chain if l["uuid"] not in ids]
    at = next((i for i, l in enumerate(rest) if l["uuid"] == anchor), None)
    if at is None:
        return chain
    return rest[:at + 1] + keep + rest[at + 1:]


def windows_of(lines: list[dict]) -> list[list[dict]]:
    """Context windows, oldest first. The last one is the current context."""
    msgs = [l for l in lines if l.get("type") in MESSAGE_TYPES and l.get("uuid") and not l.get("isSidechain")]
    if not msgs:
        return [[]]
    by_uuid = {l["uuid"]: l for l in msgs}
    is_boundary = lambda l: l.get("subtype") == "compact_boundary"
    # The full logical chain: from the newest message back to the start, across compactions.
    seq: list[dict] = []
    cur, seen = msgs[-1]["uuid"], set()
    while cur and cur not in seen:
        seen.add(cur)
        ch = _chain(by_uuid, cur)
        seq = ch + seq
        cur = ch[0].get("logicalParentUuid") if ch and is_boundary(ch[0]) else None
    # A message sent while /compact runs can link to the old chain. Then the compacted branch
    # (boundary, summary, new context) hangs off the old chain. Put it back where it happened.
    kids: dict = {}
    for l in msgs:
        kids.setdefault(l.get("parentUuid"), []).append(l)
    on = {l["uuid"] for l in seq}
    for b in [l for l in msgs if is_boundary(l) and l["uuid"] not in on]:
        pos = next((i for i, l in enumerate(seq) if l["uuid"] == b.get("logicalParentUuid")), None)
        if pos is None or pos == len(seq) - 1:
            continue
        branch, c = [b], b
        while kids.get(c["uuid"]):
            c = kids[c["uuid"]][0]
            if c["uuid"] in on:
                break
            branch.append(c)
        seq = seq[:pos + 1] + branch + seq[pos + 1:]
        on.update(l["uuid"] for l in branch)
    out: list[list[dict]] = [[]]
    for l in seq:
        if is_boundary(l) and out[-1]:
            out.append([])
        out[-1].append(l)
    return [_splice_preserved(w, by_uuid) for w in out]


# ---------------------------------------------------------------- line <-> item conversion

def _img_to_item(b: dict) -> dict:
    src = b.get("source") or {}
    if src.get("type") == "base64":
        return {"type": "input_image", "image_url": f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"}
    return {"type": "cc_raw", "raw": b}


def _img_from_item(c: dict) -> dict:
    m = re.match(r"data:([^;]+);base64,(.*)", c.get("image_url", ""), re.S)
    if not m:
        raise ValueError("An image in the edited context is not a base64 data URL.")
    return {"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}}


def _blocks_to_content(blocks, role: str) -> list[dict]:
    if isinstance(blocks, str):
        return [{"type": "output_text" if role == "assistant" else "input_text", "text": blocks}]
    out = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            out.append({"type": "output_text" if role == "assistant" else "input_text", "text": b.get("text", "")})
        elif b.get("type") == "image":
            out.append(_img_to_item(b))
        else:
            out.append({"type": "cc_raw", "raw": b})
    return out


def _content_to_blocks(content: list[dict]) -> list[dict]:
    out = []
    for c in content or []:
        t = c.get("type")
        if isinstance(c.get("text"), str) and t in ("input_text", "output_text", "text"):
            if c["text"]:
                out.append({"type": "text", "text": c["text"]})
        elif t == "input_image":
            out.append(_img_from_item(c))
        elif t == "cc_raw":
            out.append(c["raw"])
    return out


def _attachment_text(a: dict) -> str:
    t = a.get("type")
    if isinstance(a.get("text"), str) and a["text"]:
        return a["text"]
    if t == "instructions":
        return "\n\n".join(f"Contents of {f.get('path')}:\n\n{f.get('content', '')}" for f in a.get("files") or [])
    if t == "nested_memory":
        c = a.get("content") or {}
        return f"Contents of {c.get('path') or a.get('path')}:\n\n{c.get('content', '')}"
    if t in ("skill_listing",) and isinstance(a.get("content"), str):
        return a["content"]
    if t == "output_style_instructions":
        s = a.get("style") or {}
        return s.get("prompt") or json.dumps(a, ensure_ascii=False, indent=2)
    if t == "session_context":
        return "\n".join(str(v) for v in (a.get("context") or {}).values())
    if t == "date":
        return f"Today's date is {a.get('date')}."
    if t == "edited_text_file":
        return f"{a.get('filename')} was edited:\n{a.get('snippet', '')}"
    if t == "file":
        c = a.get("content") or {}
        f = c.get("file") or {}
        return f"Contents of {a.get('filename')}:\n{f.get('content', '') if isinstance(f, dict) else ''}"
    if t == "queued_command" and isinstance(a.get("prompt"), str):
        return a["prompt"]
    if t in ("deferred_tools_delta", "agent_listing_delta", "mcp_instructions_delta"):
        parts = a.get("addedBlocks") or a.get("addedLines") or []
        return "\n".join(str(p) for p in parts)
    if t == "invoked_skills":
        return "\n\n".join(f"Skill {s.get('name')}:\n{s.get('content', '')}" for s in a.get("skills") or [])
    return json.dumps(a, ensure_ascii=False, indent=2)


def line_items(line: dict) -> list[dict]:
    """The editor items for one Claude line. Lines with no meaning for the model give no items."""
    typ, u = line.get("type"), line.get("uuid")
    msg = line.get("message") or {}
    content = msg.get("content")
    items: list[dict] = []
    if typ == "user" and line.get("isCompactSummary"):
        items.append({"type": "compaction", "text": _text_of(content)})
    elif typ == "user":
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
        rest = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                out = b.get("content")
                if isinstance(out, list):
                    out = _blocks_to_content(out, "user")
                it = {"type": "function_call_output", "call_id": b.get("tool_use_id"), "output": out if out is not None else ""}
                if b.get("is_error"):
                    it["is_error"] = True
                items.append(it)
            else:
                rest.append(b)
        if rest:
            items.insert(0, {"type": "message", "role": "user",
                             "content": _blocks_to_content(content if isinstance(content, str) else rest, "user")})
    elif typ == "assistant":
        for b in content if isinstance(content, list) else [{"type": "text", "text": content or ""}]:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                items.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": b.get("text", "")}]})
            elif bt in ("thinking", "redacted_thinking"):
                txt = b.get("thinking", "")
                items.append({"type": "reasoning", "summary": [{"type": "summary_text", "text": txt}] if txt else [],
                              "content": None, "cc_redacted": bt == "redacted_thinking"})
            elif bt in ("tool_use", "server_tool_use"):
                items.append({"type": "function_call", "name": b.get("name"), "call_id": b.get("id"),
                              "arguments": json.dumps(b.get("input"), ensure_ascii=False, indent=2)})
            else:
                items.append({"type": "cc_block", "raw": b})
    elif typ == "attachment":
        a = line.get("attachment") or {}
        if a.get("type") == "prompt_snapshot":
            return []
        items.append({"type": "message", "role": "user", "cc_attachment": a.get("type"),
                      "content": [{"type": "input_text", "text": _attachment_text(a)}]})
    for it in items:
        it["cc_uuid"] = u
    return items


def system_prompt_line(lines: list[dict]) -> dict | None:
    return next((l for l in reversed(lines) if l.get("type") == "attachment"
                 and (l.get("attachment") or {}).get("type") == "prompt_snapshot"), None)


def system_prompt_text(line: dict | None) -> str | None:
    if not line:
        return None
    sp = line["attachment"].get("systemPrompt")
    if isinstance(sp, list):
        return "\n\n".join(s if isinstance(s, str) else json.dumps(s, ensure_ascii=False) for s in sp)
    return sp if isinstance(sp, str) else None


# ---------------------------------------------------------------- load

def _preview(hist: list[dict], last: bool) -> str:
    seq = reversed(hist) if last else hist
    for l in seq:
        if is_real_user(l):
            return re.sub(r"\s+", " ", _text_of(l["message"].get("content")))[:160]
    return ""


def load_session(sid: str, window: int | None = None) -> dict:
    path = find_session(sid)
    if not path:
        raise ValueError(f"No Claude Code session {sid} in {PROJECTS}.")
    lines = read_file(path)
    wins = windows_of(lines)
    sel = len(wins) if not window or window > len(wins) or window < 1 else int(window)
    hist = wins[sel - 1]
    meta_wins = []
    start_ts = hist[0].get("timestamp") if hist else None
    for n, w in enumerate(wins, 1):
        start_ts = w[0].get("timestamp") if w else None
        end = wins[n].__getitem__(0).get("timestamp") if n < len(wins) and wins[n] else None
        meta_wins.append({"n": n, "start_ts": start_ts, "end_ts": end, "items": len(w), "current": n == len(wins),
                          "first": _preview(w, False), "last": _preview(w, True),
                          "new_items": sum(1 for l in w if l.get("type") in ("user", "assistant"))})
    items = []
    turn = "start"
    after_summary = None
    pm = set()
    if hist and hist[0].get("subtype") == "compact_boundary":
        pm = set(((hist[0].get("compactMetadata") or {}).get("preservedMessages") or {}).get("uuids") or [])
    for l in hist:
        if is_real_user(l):
            turn = l["uuid"]
        for it in line_items(l):
            if it["type"] == "compaction":
                after_summary = True
            items.append({"key": f"i{len(items)}", "turn_id": turn, "ts": l.get("timestamp"),
                          "from_compaction": it["type"] == "compaction" or l["uuid"] in pm,
                          "real_user": is_real_user(l) and it["type"] == "message", "item": it})
    asst = [l for l in hist if l.get("type") == "assistant" and not str(l.get("message", {}).get("model", "")).startswith("<")]
    title = None
    for l in lines:
        if l.get("type") == "custom-title":
            title = l.get("customTitle") or title
    if not title:
        title = next((l.get("aiTitle") for l in reversed(lines) if l.get("type") == "ai-title"), None)
    cwd = project_cwd(path, lines)
    return {
        "id": sid, "source": "claude", "window": sel, "windows": meta_wins,
        "title": title or _preview(hist, False)[:80],
        "cwd": cwd,
        "model": asst[-1]["message"].get("model") if asst else None,
        "effort": next((l.get("effort") for l in reversed(asst) if l.get("effort")), None),
        "rollout_path": path, "line_count": len(lines),
        "system_prompt": system_prompt_text(system_prompt_line(hist)),
        "resume_cmd": resume_cmd(sid, cwd),
        "items": items,
    }


def _q(s: str) -> str:
    return s if re.fullmatch(r"[\w@%+=:,./-]+", s) else "'" + s.replace("'", "'\\''") + "'"


def resume_cmd(sid: str, cwd: str | None, model: str | None = None, effort: str | None = None) -> str:
    cmd = f"claude --resume {sid}"
    if model:
        cmd += f" --model {_q(model)}"
    if effort:
        cmd += f" --effort {_q(effort)}"
    return f"cd {_q(cwd)} && {cmd}" if cwd else cmd


# ---------------------------------------------------------------- fork

def _item_blocks(it: dict, role: str) -> list[dict]:
    """Claude content blocks for one edited item."""
    t = it.get("type")
    if t == "message":
        return _content_to_blocks(it.get("content"))
    if t == "function_call":
        try:
            inp = json.loads(it.get("arguments") or "{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"The input of tool call {it.get('name')} is not valid JSON: {e}")
        return [{"type": "tool_use", "id": it.get("call_id"), "name": it.get("name"), "input": inp}]
    if t == "function_call_output":
        out = it.get("output")
        if isinstance(out, list):
            out = _content_to_blocks(out)
        elif not isinstance(out, str):
            out = json.dumps(out, ensure_ascii=False)
        b = {"type": "tool_result", "tool_use_id": it.get("call_id"), "content": out}
        if it.get("is_error"):
            b["is_error"] = True
        return [b]
    if t == "cc_block":
        return [it["raw"]]
    if t == "compaction":
        return [{"type": "text", "text": it.get("text", "")}]
    return []


TEMPLATE_KEYS = ("userType", "entrypoint", "cwd", "version", "gitBranch")


def write_fork(sid: str, items: list[dict], model: str | None, effort: str | None, window: int | None = None,
               strip_reasoning: bool = True, system: str | None = None, name: str = "") -> dict:
    from ctxedit import _fix_tool_pairs  # same pairing rule as Codex: calls and results must match

    path = find_session(sid)
    if not path:
        raise ValueError(f"No Claude Code session {sid}.")
    lines = read_file(path)
    by_uuid = {l["uuid"]: l for l in lines if l.get("uuid")}
    wins = windows_of(lines)
    hist = wins[(len(wins) if not window or window > len(wins) or window < 1 else int(window)) - 1]
    if strip_reasoning:
        items = [h for h in items if h["item"].get("type") != "reasoning"]
    items = _fix_tool_pairs(items)
    if not items:
        raise ValueError("Nothing to fork after removing unmatched tool calls.")

    base = next((l for l in reversed(hist) if l.get("type") in ("user", "assistant")), None) or \
        next((l for l in reversed(lines) if l.get("type") in ("user", "assistant")), {})
    tmpl = {k: base[k] for k in TEMPLATE_KEYS if k in base}
    src_model = next((l["message"].get("model") for l in reversed(hist) if l.get("type") == "assistant"
                      and not str(l["message"].get("model", "")).startswith("<")), None)
    new_sid = str(uuid.uuid4())
    out: list[dict] = []
    uuid_map: dict[str, str] = {}

    def emit(line: dict, old: str | None = None):
        line = copy.deepcopy(line)
        for k in ("logicalParentUuid", "leafUuid"):
            line.pop(k, None)
        line["parentUuid"] = out[-1]["uuid"] if out else None
        line["uuid"] = str(uuid.uuid4())
        line["sessionId"] = new_sid
        if "session_id" in line:
            line["session_id"] = new_sid
        line["isSidechain"] = False
        if line.get("sourceToolAssistantUUID"):
            line["sourceToolAssistantUUID"] = uuid_map.get(line["sourceToolAssistantUUID"], line["sourceToolAssistantUUID"])
        if old:
            uuid_map[old] = line["uuid"]
        out.append(line)

    # Group consecutive items that came from the same source line.
    groups: list[list[dict]] = []
    for h in items:
        u = h["item"].get("cc_uuid")
        if groups and u and groups[-1][0]["item"].get("cc_uuid") == u:
            groups[-1].append(h)
        else:
            groups.append([h])

    snap = system_prompt_line(hist)
    snap_done = snap is None

    def emit_snapshot():
        nonlocal snap_done
        if snap_done:
            return
        snap_done = True
        s = copy.deepcopy(snap)
        if system is not None:
            s["attachment"]["systemPrompt"] = [system]
        emit(s, snap["uuid"])

    for g in groups:
        it0 = g[0]["item"]
        u = it0.get("cc_uuid")
        orig = by_uuid.get(u) if u else None
        if it0.get("type") == "compaction":
            boundary = by_uuid.get(orig.get("parentUuid")) if orig else None
            if boundary and boundary.get("subtype") == "compact_boundary":
                b = copy.deepcopy(boundary)
                cm = b.get("compactMetadata") or {}
                cm.pop("preservedSegment", None)
                cm.pop("preservedMessages", None)
                emit(b, boundary["uuid"])
            line = copy.deepcopy(orig) if orig else dict(tmpl, type="user", isCompactSummary=True,
                                                         isVisibleInTranscriptOnly=True, timestamp=now_iso())
            line["message"] = {"role": "user", "content": it0.get("text", "")}
            emit(line, u)
            emit_snapshot()
            continue
        emit_snapshot()
        fresh = [h["item"] for h in g]
        if orig is not None and fresh == line_items(orig):
            emit(orig, u)
            continue
        if orig is not None and orig.get("type") == "attachment":
            # An edited attachment becomes a hidden user note with the edited text.
            txt = "\n".join(c.get("text", "") for c in it0.get("content") or [] if isinstance(c.get("text"), str))
            emit(dict({k: orig[k] for k in TEMPLATE_KEYS if k in orig}, type="user", isMeta=True,
                      timestamp=orig.get("timestamp") or now_iso(),
                      message={"role": "user", "content": f"<system-reminder>\n{txt}\n</system-reminder>"}), u)
            continue
        role = "assistant" if (orig and orig.get("type") == "assistant") or (
            not orig and it0.get("type") == "message" and it0.get("role") == "assistant") else "user"
        blocks = [b for h in g for b in _item_blocks(h["item"], role)]
        if orig is not None:
            line = copy.deepcopy(orig)
            msg = line.setdefault("message", {"role": role})
            if role == "user" and isinstance(orig.get("message", {}).get("content"), str) and \
                    len(blocks) == 1 and blocks[0].get("type") == "text":
                msg["content"] = blocks[0]["text"]
            else:
                msg["content"] = blocks
            line.pop("toolUseResult", None)  # display data for the old result; it no longer matches
            emit(line, u)
        elif role == "assistant":
            emit(dict(tmpl, type="assistant", timestamp=now_iso(),
                      message={"model": src_model or "claude", "id": "msg_ctxedit_" + uuid.uuid4().hex[:20],
                               "type": "message", "role": "assistant", "content": blocks,
                               "stop_reason": "end_turn", "stop_sequence": None,
                               "usage": {"input_tokens": 0, "output_tokens": 0}}))
        else:
            meta = it0.get("role") == "developer"
            txt = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            line = dict(tmpl, type="user", timestamp=now_iso(),
                        message={"role": "user", "content": f"<system-reminder>\n{txt}\n</system-reminder>" if meta else
                                 (txt if len(blocks) == 1 and blocks[0].get("type") == "text" else blocks)})
            if meta:
                line["isMeta"] = True
            else:
                line.update(promptId=str(uuid.uuid4()), origin={"kind": "human"}, promptSource="typed")
            emit(line)
    emit_snapshot()

    last_prompt = next((_text_of(l["message"].get("content")) for l in reversed(out) if is_real_user(l)), "")
    tail = [{"type": "last-prompt", "lastPrompt": last_prompt[:500], "leafUuid": out[-1]["uuid"], "sessionId": new_sid}]
    if name:
        tail.append({"type": "custom-title", "customTitle": name, "sessionId": new_sid})
    dst = os.path.join(os.path.dirname(path), new_sid + ".jsonl")
    with open(dst, "w", encoding="utf-8") as f:
        for o in out + tail:
            f.write(json.dumps(o, ensure_ascii=False, separators=(",", ":")) + "\n")
    cwd = project_cwd(path, lines)
    return {"id": new_sid, "path": dst, "cwd": cwd, "item_count": len(items), "source": "claude",
            "turns": len({h.get("turn_id") for h in items}), "warnings": [],
            "resume_cmd": resume_cmd(new_sid, cwd, model, effort)}

