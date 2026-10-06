"""End-to-end test for the context editor on one machine.

Usage: python3 selftest.py [--port 7317] [--thread ID]
The server must run on the port. The script makes one fork, runs one real
turn on it, checks the reply, and archives the fork.
"""
import argparse, json, os, sqlite3, sys, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ctxedit
from appserver import AppServer

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=7317)
ap.add_argument("--thread")
a = ap.parse_args()
BASE = f"http://127.0.0.1:{a.port}"

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.load(r)

def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"FAIL {path}: {e.code} {e.read()[:500]}")

def pick_thread():
    con = sqlite3.connect(f"file:{ctxedit.state_db()}?mode=ro", uri=True)
    rows = con.execute("select id from threads where archived=0 order by updated_at desc limit 200").fetchall()
    best = None
    for (tid,) in rows:
        try:
            lines = ctxedit.load_chain(tid)
        except Exception:
            continue
        nc = sum(1 for l in lines if l.get("type") == "compacted")
        nu = sum(1 for l in lines if l.get("type") == "response_item" and ctxedit.is_real_user_message(l["payload"]))
        if nu >= 2 and (best is None or (nc > 0) > (best[1] > 0)):
            best = (tid, nc)
            if nc > 0:
                break
    if not best:
        raise SystemExit("FAIL: no thread with two user messages found")
    return best[0]

info = get("/api/info"); print("INFO", info)
models = get("/api/models"); print("MODELS", len(models), [m["id"] for m in models][:5])
tid = a.thread or pick_thread()
d = get(f"/api/thread?ref=codex://threads/{tid}")
print("THREAD", tid, "items", len(d["items"]), "windows", len(d["windows"]), "model", d["model"], "cwd", d["cwd"])
win = 1 if len(d["windows"]) > 1 else None
if win:
    d = get(f"/api/thread?ref={tid}&window={win}")
    print("WINDOW 1 items", len(d["items"]), "end", d["windows"][0]["end_ts"])
items = d["items"]
users = [i for i, x in enumerate(items) if x["real_user"]]
cut = users[len(users) // 2] if len(users) > 1 else len(items)   # fork from the middle of the window
kept = [x for x in items[:cut] if not x["real_user"] or x is not items[users[0]]]  # also remove the first user message
kept.append({"turn_id": "new1", "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Note for later: the codeword is ZEBRA-42."}]}})
kept.append({"turn_id": "new1", "item": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Noted. The codeword is ZEBRA-42."}]}})
model = next((m["id"] for m in models if m["id"] == d["model"]), None) or next((m["id"] for m in models if m.get("isDefault")), models[0]["id"])
res = post("/api/fork", {"source_id": tid, "items": [{"turn_id": x["turn_id"], "item": x["item"]} for x in kept],
                         "model": model, "effort": "low", "name": "ctxedit e2e test", "strip_reasoning": True, "open": False})
print("FORK", res["id"], "items", res["item_count"], "turns", res["turns"], "warnings", res["warnings"])
f = get(f"/api/thread?ref={res['id']}")
assert f["cwd"] == d["cwd"], "cwd changed"
assert f["model"] == model, f"model is {f['model']}"
print("RELOAD ok: items", len(f["items"]), "model", f["model"], "title", f["title"])

s = AppServer()
ok = False
try:
    s.request("thread/resume", {"threadId": res["id"]})
    s.request("turn/start", {"threadId": res["id"], "input": [{"type": "text", "text": "What is the codeword? Reply with only the codeword. Do not use tools.", "text_elements": []}]})
    s.wait_note("turn/completed", timeout=300)
    replies = [m["params"]["item"].get("text", "") for m in s.notes
               if m.get("method") == "item/completed" and m["params"]["item"].get("type") == "agentMessage"]
    print("REPLY", replies)
    ok = any("ZEBRA-42" in r for r in replies)
    turns = s.request("thread/turns/list", {"threadId": res["id"]}).get("data", [])
    print("TURNS after reply", len(turns))
finally:
    try:
        s.request("thread/archive", {"threadId": res["id"]})
        print("ARCHIVED", res["id"])
    except Exception as e:
        print("ARCHIVE FAILED", e)
    s.close()
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)

