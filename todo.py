"""To-do list: things the reviewer found in your documents that you have not finished.

One JSON file per task in <state>/todo/. The reviewer model (reviewer.py) creates tasks and reads your
updates; this module is plain storage with no model calls. Each task also gets a vault note in tasks/.
"""
import hashlib
import json
from datetime import datetime

import yaml

import agent
from bootstrap import clean, slug

VAULT, STATE = agent.VAULT, agent.STATE
TODO = STATE / "todo"
MAX_TEXT = 4000


def _path(tid):
    return TODO / f"{tid}.json"


def _save(t):
    TODO.mkdir(parents=True, exist_ok=True)
    _path(t["id"]).write_text(json.dumps(t, indent=1, ensure_ascii=False))


def get(tid):
    p = _path(tid)
    return json.loads(p.read_text()) if p.exists() else None


def list_tasks(status=None):
    if not TODO.exists():
        return []
    items = [json.loads(p.read_text()) for p in TODO.glob("*.json")]
    if status:
        items = [t for t in items if t["status"] == status]
    return sorted(items, key=lambda t: t["created"])


def count_open():
    return len(list_tasks("open"))


def create(title, detail, source, quote):
    """Add a task found in a note. Returns its id, or None if it already exists (any status)."""
    title = clean(title)[:120]
    tid = hashlib.sha1(title.lower().encode()).hexdigest()[:12]
    if not title or _path(tid).exists():
        return None
    t = {"id": tid, "created": datetime.now().isoformat(timespec="seconds"), "status": "open",
         "title": title, "detail": clean(detail)[:300], "source": source, "quote": clean(quote)[:300],
         "state_line": "", "updates": []}
    _save(t)
    write_note(t)
    return tid


def add_update(tid, text, finished=False):
    """Your update. 'finished' closes the task straight away (you said so); the reviewer still reads it."""
    t = get(tid)
    if not t:
        raise FileNotFoundError(tid)
    text = str(text).strip()[:MAX_TEXT]
    if not text and not finished:
        raise ValueError("empty update")
    t["updates"].append({"at": datetime.now().isoformat(timespec="seconds"),
                         "text": text or "Done.", "finished": bool(finished), "processed": False})
    if finished:
        t["status"] = "done"
    _save(t)
    write_note(t)
    return t


def drop(tid):
    t = get(tid)
    if not t:
        raise FileNotFoundError(tid)
    t["status"] = "dropped"
    _save(t)
    write_note(t)


def pending():
    return [t for t in list_tasks() if any(not u["processed"] for u in t["updates"])]


def save(t):
    _save(t)
    write_note(t)


def write_note(t):
    """The task as a vault note, linked to the note it came from."""
    name = f"{slug(t['title'], 60)}-{t['id'][:6]}"
    fm = {"type": "task", "status": t["status"], "confidence": "medium" if t["updates"] else "low",
          "task_id": t["id"]}
    src = t["source"][:-3] if t["source"].endswith(".md") else t["source"]
    lines = [f"- {u['at'][:10]}: {clean(u['text'])}" for u in t["updates"]]
    out = VAULT / "tasks" / f"{name}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n"
        f"# {t['title']}\n\n{t['detail']}\n\n> {t['quote']}\n\nFound in [[{src}]].\n"
        + (f"\n**Current state:** {t['state_line']}\n" if t["state_line"] else "")
        + ("\n## Your updates\n\n" + "\n".join(lines) + "\n" if lines else ""), encoding="utf-8")


if __name__ == "__main__":
    for t in list_tasks("open"):
        print(t["id"], t["title"])
