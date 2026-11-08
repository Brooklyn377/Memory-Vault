"""After an accepted chunk, the reviewer closes or updates open tasks that the new facts answer."""
import json
import time
from datetime import datetime

import agent
import reviewer
import todo

LOG = agent.STATE / "task_changes.jsonl"
QUEUE = agent.STATE / "taskcheck_queue"   # approved chunks waiting for their check, one small file each
SYS = ("You keep a person's to-do board honest. You get OPEN TASKS and NEW FACTS from their notes. "
       "If a new fact shows a task is finished or was answered elsewhere, use action close. "
       "If a fact only adds progress, use action update. Otherwise change nothing. "
       "Copy the quote exactly from that fact's quote. Never guess. "
       'Reply as JSON only: {"changes":[{"task":"<id>","action":"close|update","fact":<number>,'
       '"quote":"<exact words>","note":"<one short sentence>"}]}. If nothing applies: {"changes":[]}')


def _apply(rel, facts, ids, c):
    if not isinstance(c, dict) or c.get("action") not in ("close", "update") or c.get("task") not in ids:
        return
    try:
        f = facts[int(c.get("fact"))]
    except (TypeError, ValueError, IndexError):
        return
    quote = str(c.get("quote", ""))
    if not reviewer.grounded(quote, str(f.get("quote", "")) + " " + str(f.get("fact", ""))):
        return
    t = todo.get(c["task"])
    if not t or t["status"] != "open":
        return
    closing = c["action"] == "close"
    note = str(c.get("note", ""))[:200]
    src = rel.split("/")[-1]
    t["updates"].append({"at": datetime.now().isoformat(timespec="seconds"),
                         "text": f"{'Closed' if closing else 'Updated'} by reviewer: {note} (from {src}: \"{quote[:150]}\")",
                         "finished": closing, "processed": True})
    if closing:
        t["status"] = "done"
    todo.save(t)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "task": t["id"],
                             "title": t["title"], "action": c["action"], "quote": quote, "note": note,
                             "source": rel}, ensure_ascii=False) + "\n")
    agent.log(f"task check: {c['action']} '{t['title'][:40]}' - {note[:60]}")


def queue(rel, data):
    """The Sweeper hands over the facts of an approved chunk and goes straight on. The Reviewer's worker checks them later."""
    facts = [f for f in (data.get("facts") or []) if isinstance(f, dict)]
    if not facts:
        return
    QUEUE.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}.json"
    tmp = QUEUE / f".{name}.tmp"   # written whole and then renamed: the worker never reads half a file
    tmp.write_text(json.dumps({"rel": rel, "facts": facts, "at": datetime.now().isoformat(timespec="seconds")},
                              ensure_ascii=False))
    tmp.replace(QUEUE / name)


def _run(rel, facts):
    """Compare the facts with every open task. Raises if the Reviewer call fails."""
    tasks = todo.list_tasks("open")
    if not facts or not tasks:
        return
    tl = "\n".join(f"{t['id']}: {t['title']} - {t['detail']}" for t in tasks)
    fl = "\n".join(f"{i}. {f.get('entity')}: {f.get('fact')}\n   quote: \"{str(f.get('quote', ''))[:200]}\""
                   for i, f in enumerate(facts))
    v = reviewer.rask(SYS, f"OPEN TASKS:\n{tl}\n\nNEW FACTS:\n{fl}", tokens=8000)
    if not isinstance(v, dict):
        return
    ids = {t["id"] for t in tasks}
    for c in v.get("changes") or []:
        _apply(rel, facts, ids, c)


def run_next():
    """Check the oldest queued chunk. -> 1 if it did something, else 0 (nothing to do, or the Reviewer is not up, in which
    case everything stays queued). A check that fails is kept, marked failed, and listed under "Failed, waiting for you";
    nothing is deleted except by Skip."""
    for p in sorted(QUEUE.glob("*.json")) if QUEUE.is_dir() else []:
        try:
            e = json.loads(p.read_text())
        except (OSError, ValueError):
            continue   # not readable now: left alone, never deleted
        if e.get("failed"):
            continue
        if not reviewer.ready():
            return 0
        try:
            _run(e["rel"], e["facts"])
        except Exception as ex:
            e["failed"] = f"{type(ex).__name__}: {str(ex)[:120]}"
            p.write_text(json.dumps(e, ensure_ascii=False))
            agent.log(f"task check failed, waiting for you in the Failed list: {e['failed']}")
            return 1
        p.unlink(missing_ok=True)
        return 1
    return 0


def failed():
    """The checks that failed, as (key, record), oldest first."""
    out = []
    for p in sorted(QUEUE.glob("*.json")) if QUEUE.is_dir() else []:
        try:
            e = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if e.get("failed"):
            out.append((p.stem, e))
    return out


def decide(key, action):
    """retry puts a failed check back in the queue; skip deletes it (only ever on your say-so)."""
    p = QUEUE / f"{key}.json"
    try:
        e = json.loads(p.read_text())
    except (OSError, ValueError):
        return
    if action == "skip":
        p.unlink(missing_ok=True)
        return
    e.pop("failed", None)
    p.write_text(json.dumps(e, ensure_ascii=False))
