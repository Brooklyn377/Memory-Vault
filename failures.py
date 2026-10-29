"""Everything that failed and is waiting for you, in one list, so nothing is forgotten: file checks and Curator notes set
aside, jobs that failed, and chat messages that could not be answered. There is no store of its own: each part keeps
its own record, and Retry / Skip here call that part's own Try again / Skip, the same as answering its Ask card (whose
card is then closed). Nothing retries by itself: you press Retry, Retry all, or say yes after changing settings."""
import json
from pathlib import Path


def _filechecks():
    import filechanges
    out = []
    for p in sorted(filechanges.QUEUE.glob("*.json")) if filechanges.QUEUE.is_dir() else []:
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            d = {}
        out.append({"source": "file", "key": p.stem, "what": f"File changes: {p.stem}", "why": d.get("why", ""), "at": d.get("at", "")})
    return out


def _curator():
    import curator
    out = []
    for p in sorted(curator.QUEUE.glob("*.json")) if curator.QUEUE.is_dir() else []:
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        out.append({"source": "curator", "key": d.get("rel", p.stem), "what": f"Curator review: {Path(d.get('rel', p.stem)).stem}",
                    "why": d.get("why", ""), "at": d.get("at", "")})
    return out


def _reviews():
    import reviewgate
    return [{"source": "review", "key": k, "what": f"Review: {str(e.get('rel', '')).split('/')[-1][:60]}",
             "why": f"the Reviewer gave no usable answer {e.get('unusable', 0)} times", "at": ""}
            for k, e in reviewgate._entries() if e.get("stuck")]


def _jobs():
    import jobs
    return [{"source": "job", "key": j["id"], "what": f"Job: {j['kind']} {j.get('target') or ''}".strip(), "why": j.get("error", ""),
             "at": j.get("created", "")} for j in jobs.all_jobs() if j["status"] == "failed"]


def _chat():
    import ui_chat
    return [{"source": "chat", "key": m["id"], "what": f'Chat: "{m["text"][:80]}"', "why": m.get("reply", ""), "at": m["id"][:15]}
            for m in ui_chat._all() if m["status"] == "error"]


SOURCES = {"file": _filechecks, "curator": _curator, "review": _reviews, "job": _jobs, "chat": _chat}
CARD = {"file": ("filecheck", "name"), "curator": ("curatecheck", "rel"), "review": ("reviewstuck", "key")}


def listing():
    out = []
    for name, fn in SOURCES.items():
        try:
            out += fn()
        except Exception:
            pass
    return sorted(out, key=lambda x: str(x.get("at", "")))


def _close_card(source, key, answer):
    """Close the open Ask card for this item, so it is not asked about twice."""
    if source not in CARD:
        return
    import questions
    kind, field = CARD[source]
    from datetime import datetime
    for q in questions.list_questions("open"):
        if q["kind"] == kind and q.get("payload", {}).get(field) == key:
            q["status"], q["answer"], q["answered"] = "answered", answer, datetime.now().isoformat(timespec="seconds")
            questions._save(q)


def act(source, key, action):
    """retry or skip one item."""
    if action not in ("retry", "skip"):
        raise ValueError("retry or skip")
    if source == "file":
        import filechanges
        filechanges.decide(key, action)
    elif source == "curator":
        import curator
        curator.decide(key, action)
    elif source == "review":
        import reviewgate
        reviewgate.decide_stuck(key, action)
    elif source == "job":
        import jobs
        jobs.requeue(key) if action == "retry" else jobs.drop(key)
    elif source == "chat":
        import ui_chat
        ui_chat.retry(key) if action == "retry" else ui_chat.drop(key)
    else:
        raise ValueError("unknown kind of task")
    _close_card(source, key, action)


def retry_all():
    """Retry everything listed, and have the file checker go over every file again (it also picks up reads that
    failed before failures were set aside)."""
    import filechanges
    filechanges.rescan()
    n = 0
    for x in listing():
        try:
            act(x["source"], x["key"], "retry")
            n += 1
        except (ValueError, OSError):
            pass
    return n
