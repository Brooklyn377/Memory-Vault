"""After an accepted chunk, the reviewer closes or updates open tasks that the new facts answer."""
import json
from datetime import datetime

import agent
import reviewer
import todo

LOG = agent.STATE / "task_changes.jsonl"
MAX_TASKS = 30
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


def check(rel, data):
    try:
        facts = [f for f in (data.get("facts") or []) if isinstance(f, dict)]
        tasks = todo.list_tasks("open")[:__import__("tuning").get("taskcheck.max_tasks")]
        if not facts or not tasks or not reviewer.ready():
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
    except Exception as e:
        agent.log(f"task check error: {type(e).__name__}: {str(e)[:80]}")
