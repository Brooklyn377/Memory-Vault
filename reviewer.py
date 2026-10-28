"""Reviewer model (gpt-oss-20b on its own llama-server): finds what you have not finished, reads your updates.

  reviewer.py --watch     keep running (the memory-review-worker service)
  reviewer.py --once      one pass: handle your updates, then scan new/changed summaries for unfinished tasks
Needs the reviewer server on the configured endpoint (default http://127.0.0.1:8081/v1).
"""
import hashlib
import json
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import agent
import questions
import todo
from bootstrap import clean, slug

R = agent.CFG.get("reviewer", {})
ENDPOINT = R.get("endpoint", "http://127.0.0.1:8081/v1").rstrip("/")
NAME = R.get("name", "gpt-oss-20b")
VAULT, STATE = agent.VAULT, agent.STATE
SCANNED = STATE / "todo_scanned.json"
NOTE_CHARS = __import__("tuning").get("reviewer.note_chars")

SCAN_SYS = (
    "You read one note from a person's notes vault and list things the PERSON still has to do or "
    "finish: unfinished repairs, setup steps not done yet, pending decisions, plans not started. Leave out "
    "anything the note says was carried out, and generic advice. A task is only done if the note says it was actually carried out (installed, fixed, configured, tested). A plan, a design, a decision or an explanation that was 'established' or 'agreed' is not a finished task: its follow-up steps still count. If the note lists next steps, list those. Do not debate wording; decide quickly from what the note says. Reply with ONLY JSON: "
    '{"tasks":[{"title":"<short imperative>","detail":"<one sentence of context>",'
    '"quote":"<exact text copied from the note that shows it is unfinished>"}]}. '
    "At most 5 tasks. Use [] if there are none.")

UPDATE_SYS = (
    "You keep track of one unfinished task for a person's notes vault. You get the TASK, the earlier updates and "
    "the person's NEW UPDATE. Trust the person over the notes. Reply with ONLY JSON: "
    '{"status":"open|done|dropped","summary":"<one factual sentence of where things stand, in the person\'s '
    'own words>","question":"<one short question for the person only if something needed to understand the '
    'update is missing, otherwise an empty string>"}.')


def ready():
    import swap
    try:
        import remote
        if remote.enabled("gpt"):
            return remote.health("gpt") == "ready"
    except Exception:
        pass
    if swap.holding():
        return True
    try:
        import power
        if power.off("gpt"):
            return True
    except Exception:
        pass
    try:
        with urllib.request.urlopen(ENDPOINT[:-3] + "/health", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def rask(system, user, tokens=None):
    import settings
    S = settings.get("gpt")
    body = {"model": NAME, "temperature": S["temperature"], "max_tokens": tokens or S["max_tokens"]["review"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "chat_template_kwargs": {"reasoning_effort": S["reasoning_effort"]}}
    import thinklog
    msg = thinklog.chat("gpt", ENDPOINT, body, "review", slot=0)
    return agent.parse_json(msg.get("content") or "")


def norm(s):
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def grounded(quote, text):
    q = norm(quote)
    return len(q) >= __import__("tuning").get("review.min_quote_chars") and q in norm(text)


def body_of(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"\A---\s*\n.*?\n---\s*\n", text, re.DOTALL)
    return text[m.end():] if m else text


def process_updates():
    """Read each update you typed, update the task and its note, and pass your words on to the agent."""
    n = 0
    for t in todo.pending():
        for u in [u for u in t["updates"] if not u["processed"]]:
            history = "\n".join(f"- {x['at'][:10]}: {x['text']}" for x in t["updates"] if x["processed"]) or "(none)"
            data = rask(UPDATE_SYS, f"TASK: {t['title']}\nCONTEXT: {t['detail']}\nFROM THE NOTES: {t['quote']}\n"
                                    f"EARLIER UPDATES:\n{history}\n\nNEW UPDATE: {u['text']}"
                                    + ("\n(The person marked this task finished.)" if u["finished"] else ""))
            if not data or not str(data.get("summary", "")).strip():
                agent.log(f"reviewer: unusable reply for task {t['id']}, will retry")
                return n
            status = data.get("status") if data.get("status") in ("open", "done", "dropped") else t["status"]
            if not u["finished"]:
                t["status"] = status
            t["state_line"] = clean(str(data["summary"]))[:300]
            u["processed"] = True
            q = str(data.get("question") or "").strip()
            if len(q) > 10:
                questions.ask(clean(q), why=f"About your task: {t['title']}", kind="free", payload={"task": t["id"]})
            inbox = VAULT / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            (inbox / f"{stamp}-task-{t['id'][:6]}.md").write_text(
                "---\ntype: user-statement\nconfidence: high\ncreated: " + stamp[:10] + "\n---\n\n"
                f"Task update — {t['title']}: {clean(u['text'])}\n", encoding="utf-8")
            todo.save(t)
            agent.log(f"reviewer: task '{t['title'][:40]}' -> {t['status']}")
            n += 1
    return n


def scan_note(path):
    text = body_of(path)[:NOTE_CHARS]
    data = rask(SCAN_SYS, text)
    if data is None:
        return None
    made = 0
    rel = path.relative_to(VAULT).as_posix()
    for item in (data.get("tasks") or [])[:5]:
        if isinstance(item, dict) and item.get("title") and grounded(item.get("quote"), text):
            made += bool(todo.create(item["title"], item.get("detail", ""), rel, item["quote"]))
    return made


def scan(limit=None):
    done = json.loads(SCANNED.read_text()) if SCANNED.exists() else {}
    n = 0
    for p in sorted((VAULT / "summaries").glob("*.md")):
        rel = p.relative_to(VAULT).as_posix()
        h = hashlib.sha1(p.read_bytes()).hexdigest()
        if done.get(rel) == h:
            continue
        if limit is not None and n >= limit:
            break
        made = scan_note(p)
        if made is None:
            agent.log(f"reviewer: no usable reply scanning {rel}")
            continue
        done[rel] = h
        SCANNED.write_text(json.dumps(done))
        agent.log(f"reviewer: scanned {rel}: {made} new task(s)")
        n += 1
        process_updates()  # your updates come first
    return n


def one_pass():
    return process_updates() + scan()


def watch():
    agent.log("reviewer worker started")
    while True:
        if not ready():
            time.sleep(30)
            continue
        try:
            work = one_pass()
        except Exception as e:
            agent.log(f"reviewer: {type(e).__name__}: {str(e)[:120]}")
            work = 0
            time.sleep(120)
        time.sleep(30 if work else 300)


if __name__ == "__main__":
    if "--watch" in sys.argv:
        watch()
    elif "--once" in sys.argv:
        print("did", one_pass(), "item(s)")
    else:
        sys.exit("usage: reviewer.py --watch | --once")
