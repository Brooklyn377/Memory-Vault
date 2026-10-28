"""Queue tab data: what each model is doing now and what is waiting for it. Read-only."""
import json
import time
import urllib.request
from pathlib import Path

import agent
from history import chat_name

STATE, VAULT = agent.STATE, agent.VAULT
KEY = {"sweeper": "qwen", "reviewer": "gpt", "curator": "cur"}
PORT = {"sweeper": 8080, "reviewer": 8081, "curator": 8082}
JOB = {"sweep": "Reading a chat part", "ask": "Reading an inbox note", "review": "Reviewing",
       "chat": "Answering a chat message", "curate": "Reviewing in depth"}
LIMIT = 200
_cache = {}


def cached(name, secs, fn):
    hit = _cache.get(name)
    if hit and time.time() - hit[0] < secs:
        return hit[1]
    try:
        val = fn()
    except Exception as e:
        val = {"error": f"{type(e).__name__}: {str(e)[:100]}"}
    _cache[name] = (time.time(), val)
    return val


def jread(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def busy(port):
    """Number of running calls on a server, or None if it does not answer."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/slots", timeout=2) as r:
            return sum(1 for x in json.load(r) if x.get("is_processing"))
    except Exception:
        return None


def inflight():
    """Calls that started and have not ended, newest first."""
    starts, ended = {}, set()
    for f in sorted((STATE / "think").glob("think-*.jsonl"))[-2:]:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        if '"k": "start"' in line:
                            r = json.loads(line)
                            starts[r["c"]] = r
                        elif '"k": "end"' in line:
                            ended.add(json.loads(line)["c"])
                    except (ValueError, KeyError):
                        continue
        except OSError:
            continue
    return sorted((r for c, r in starts.items() if c not in ended), key=lambda r: -r["t"])


def now_state():
    import swap
    calls, held = inflight(), swap.holding()
    marker = jread(STATE / "sweep_now.json", {})
    out = {}
    for m in KEY:
        try:
            import remote
            ext = remote.enabled(KEY[m])
        except Exception:
            ext = False
        b = (min(8, sum(1 for x in calls if x["m"] == KEY[m] and time.time() - x["t"] / 1e9 < 10800)) if ext else busy(PORT[m]))
        if b is None:
            if m == "curator":
                state = "unloaded (loads when it runs)"
            else:
                state = "unloaded while the curator works" if held else ("unloaded by you" if __import__("power").off(KEY[m]) else "not answering")
            out[m] = {"state": state, "calls": []}
            continue
        items = []
        for r in [x for x in calls if x["m"] == KEY[m]][:b]:
            what = r.get("w") or ""
            if r.get("s") == "sweep" and marker.get("rel"):
                what = f'part {marker["chunk"]} of {marker["of"]} of "{chat_name(marker["rel"])}"'
            items.append({"job": JOB.get(r.get("s"), r.get("s", "Working")), "what": what, "since": r["t"] / 1e9})
        if b and not items:
            items = [{"job": "Working", "what": "", "since": None}]
        out[m] = {"state": "working" if b else "idle", "calls": items}
    return out


def sweeper_next():
    import questions
    import sweep
    st = sweep.load_state()
    items, waiting, left = [], [], 0
    for p in sorted(sweep.CONV_DIR.glob("*.md")):
        rel = p.relative_to(agent.VAULT).as_posix()
        meta, body = sweep.read_chat(rel)
        d = (st.get("docs") or {}).get(rel) or {}
        chunks = sweep.make_chunks(body, d)
        hs = d.get("hashes") or []
        i = 0
        while i < len(hs) and i < len(chunks) and hs[i] == chunks[i]["hash"]:
            i += 1
        for key, qid in sorted((d.get("flags") or {}).items(), key=lambda kv: int(kv[0])):
            q = questions.get(qid)
            if q and q["status"] == "open":
                waiting.append({"text": chat_name(rel), "sub": f"part {int(key) + 1} is waiting for your answer in Ask"})
            elif q and q["status"] == "answered":
                items.insert(0, {"text": chat_name(rel), "sub": f"part {int(key) + 1} will be re-read with your answer"})
        if len(chunks) > i:
            items.append({"text": chat_name(rel), "sub": f"{len(chunks) - i} of {len(chunks)} parts left to read"})
            left += len(chunks) - i
    done = set(jread(STATE / "agent_done.json", []))
    parked = STATE / "inbox_queue"
    for p in sorted((VAULT / "inbox").glob("*.md")):
        if p.name not in done and not (parked / f"{p.name}.json").exists():
            items.append({"text": p.name, "sub": "inbox note waiting for the agent"})
    notes = [f"{left} chat parts left to read. Chats go oldest first, then inbox notes."]
    return {"items": items[:LIMIT], "total": len(items), "waiting": waiting[:LIMIT], "notes": notes}


def reviewer_next():
    import todo
    items = []
    chat = STATE / "chat"
    for p in sorted(chat.glob("*.json")) if chat.is_dir() else []:
        m = jread(p, {})
        if m.get("status") in ("queued", "working"):
            items.append({"text": "Chat: " + " ".join(str(m.get("text", "")).split())[:70], "sub": "message " + m["status"]})
    rq = STATE / "review_queue"
    parked = list(rq.glob("*.json")) if rq.is_dir() else []
    if parked:
        items.append({"text": f"{len(parked)} chunk update(s) from the Sweeper", "sub": "parked until the Reviewer is free"})
    for t in todo.pending():
        items.append({"text": "To-do: " + t["title"], "sub": "your update is waiting to be read"})
    scanned = jread(STATE / "todo_scanned.json", {})
    for p in sorted((VAULT / "summaries").glob("*.md")):
        if f"summaries/{p.name}" not in scanned:
            items.append({"text": "Scan for tasks: " + p.stem, "sub": "summary not scanned yet"})
    fq = STATE / "file_queue"
    waiting = [{"text": p.stem, "sub": "file check parked, waiting for your answer in Ask"}
               for p in (fq.glob("*.json") if fq.is_dir() else [])]
    return {"items": items[:LIMIT], "total": len(items), "waiting": waiting[:LIMIT],
            "notes": ["Chat messages first, then updates, then summaries. File checks run whenever the Reviewer is idle."]}


def curator_next():
    import curator
    import mapper
    cfg, data = curator.load_cfg(), curator.load_data()
    v = curator.Vault(curator.VAULT)
    ents = sorted(r for r in v.body if r.startswith("entities/"))
    parked = {p.stem for p in curator.QUEUE.glob("*.json")} if curator.QUEUE.is_dir() else set()
    blocked, pen, bad = curator.hygiene.blocked(), curator.penalties(), curator.bad_facts()
    fresh = []
    for rel in ents:
        name = Path(rel).stem
        if name in parked or curator.hygiene.slug(name).lower() in blocked:
            continue
        rec = data["notes"].get(rel)
        if not rec or rec["hash"] != curator.chash(curator.file_text(rel)):
            score, why = curator.uncertainty(v, rel, pen, bad)
            fresh.append((-score, rel, why))
    fresh.sort()
    st = curator.load_status()
    m = mapper.load()
    notes = ["Switched off." if not cfg["enabled"] else "Status: " + str(st.get("state", "")) + (" - " + st["why"] if st.get("why") else ""),
             "Least sure first. Once every note is checked it starts map passes." if cfg.get("map") else "Map pass is switched off."]
    p = m["pass"]
    if p.get("n"):
        notes.append(f'Map pass {p["n"]}: part {p.get("slice", 0)} done, {p.get("added", 0)} added so far'
                     + (" (finished)" if p.get("done") else ""))
    return {"items": [{"text": v.title(r), "sub": why or "nothing flagged"} for _, r, why in fresh][:LIMIT],
            "total": len(fresh), "notes": notes, "waiting": []}


def snapshot():
    return {"now": cached("now", 4, now_state),
            "next": {"sweeper": cached("sw", 30, sweeper_next), "reviewer": cached("rv", 10, reviewer_next),
                     "curator": cached("cu", 60, curator_next)},
            "at": time.time()}


if __name__ == "__main__":
    print(json.dumps(snapshot(), indent=1, default=str)[:3500])
