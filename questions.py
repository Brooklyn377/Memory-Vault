"""Clarifying-question queue: the agent asks, the person answers from the web UI.

Questions live as JSON files in <state>/questions/. Answers flow back as evidence:
  free     the answer is written to <vault>/inbox as a high-confidence user statement, which the
           agent watcher then reads like any other note you send;
  merge    "same thing?" questions: the answer updates <state>/merges.yaml (or remembers 'different').
CLI:  questions.py list | dupes
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

import agent
from scrub import scrub

VAULT, STATE = agent.VAULT, agent.STATE
QDIR = STATE / "questions"
NOT_SAME = STATE / "not_same.json"
MERGES = STATE / "merges.yaml"
MAX_OPEN = __import__("tuning").get("questions.max_open")
MAX_ANSWER = 4000
# Cards for failures and results waiting for you: always asked, whatever the Ask queue size limit.
KEEP = ("filecheck", "curatecheck", "inboxcheck", "assistant", "gitpull", "serverfail", "job")


def _path(qid):
    return QDIR / f"{qid}.json"


def _load(p):
    return json.loads(p.read_text())


def ask(question, why="", kind="free", options=None, payload=None):
    """Queue a question. Returns its id, or None if the queue is full. Never asks the same thing twice."""
    question = str(question).strip()[:500]
    qid = hashlib.sha1(f"{kind}|{question}|{json.dumps(payload, sort_keys=True)}".encode()).hexdigest()[:12]
    QDIR.mkdir(parents=True, exist_ok=True)
    if _path(qid).exists():
        return qid  # already asked (open, answered or dismissed): do not nag
    if len(list_questions("open")) >= MAX_OPEN and kind not in KEEP:
        return None   # a full queue drops new questions, never a failure or a result waiting for you
    _path(qid).write_text(json.dumps({
        "id": qid, "created": datetime.now().isoformat(timespec="seconds"), "status": "open",
        "kind": kind, "question": question, "why": str(why)[:500],
        "options": options or [], "payload": payload or {}, "answer": None}, indent=1))
    return qid


def list_questions(status=None):
    if not QDIR.exists():
        return []
    items = [_load(p) for p in QDIR.glob("*.json")]
    if status:
        items = [q for q in items if q["status"] == status]
    return sorted(items, key=lambda q: q["created"], reverse=(status != "open"))


def get(qid):
    p = _path(qid)
    return _load(p) if p.exists() else None


def count_open():
    return len(list_questions("open"))


def _save(q):
    _path(q["id"]).write_text(json.dumps(q, indent=1))


def dismiss(qid):
    q = _load(_path(qid))
    q["status"], q["answered"] = "dismissed", datetime.now().isoformat(timespec="seconds")
    if q["kind"] == "filecheck":
        import filechanges
        filechanges.decide(q["payload"]["name"], "skip")
    elif q["kind"] == "curatecheck":
        import curator
        curator.decide(q["payload"]["rel"], "skip")
    elif q["kind"] == "inboxcheck":
        agent.decide_note(q["payload"]["name"], "skip")
    _save(q)


def answer(qid, text):
    q = _load(_path(qid))
    if q["status"] != "open":
        raise ValueError("question is not open")
    text = str(text).strip()[:MAX_ANSWER]
    if not text:
        raise ValueError("empty answer")
    if q["kind"] == "merge":
        a, b = q["payload"]["a"], q["payload"]["b"]
        if text == "keep_a":      # a is the canonical name, b is the alias
            _add_merge(b, a)
        elif text == "keep_b":
            _add_merge(a, b)
        elif text == "different":
            _add_not_same(a, b)
        else:
            raise ValueError("unknown merge answer")
    elif q["kind"] == "hygiene":
        import hygiene
        if text not in ("keep", "drop"):
            raise ValueError("unknown answer")
        (hygiene.add_block if text == "drop" else hygiene.add_keep)(q["payload"]["entity"])
    elif q["kind"] == "filecheck":
        import filechanges
        if text not in ("retry", "skip"):
            raise ValueError("unknown answer")
        filechanges.decide(q["payload"]["name"], text)
    elif q["kind"] == "curatecheck":
        if text not in ("retry", "skip"):
            raise ValueError("unknown answer")
        import curator
        curator.decide(q["payload"]["rel"], text)
    elif q["kind"] == "inboxcheck":
        if text not in ("retry", "skip"):
            raise ValueError("unknown answer")
        agent.decide_note(q["payload"]["name"], text)
    elif q["kind"] in ("gitpull", "assistant", "serverfail"):   # notices: answering only acknowledges them
        pass
    elif q["kind"] == "job":
        if text not in ("approve", "reject"):
            raise ValueError("unknown answer")
        import jobs
        jobs.decide(q["payload"]["job"], text == "approve")
    else:
        clean, _ = scrub(text)
        inbox = VAULT / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        fm = {"type": "user-statement", "confidence": "high", "created": stamp[:10],
              "question_id": qid}
        (inbox / f"{stamp}-answer-{qid}.md").write_text(
            "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n"
            f"Question asked: {q['question']}\n\nMy answer: {clean}\n", encoding="utf-8")
    q["status"], q["answer"] = "answered", text if q["kind"] == "merge" else scrub(text)[0]
    q["answered"] = datetime.now().isoformat(timespec="seconds")
    _save(q)
    return q


def _add_merge(alias, canon):
    d = (yaml.safe_load(MERGES.read_text()) or {}) if MERGES.exists() else {}
    d[alias] = canon
    MERGES.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True))


def _pair_key(a, b):
    return "|".join(sorted(x.lower().replace(" ", "-") for x in (a, b)))


def _add_not_same(a, b):
    d = json.loads(NOT_SAME.read_text()) if NOT_SAME.exists() else []
    if _pair_key(a, b) not in d:
        d.append(_pair_key(a, b))
    NOT_SAME.write_text(json.dumps(d))


def duplicate_candidates():
    """Entity pairs whose names overlap (one name's words contained in the other's)."""
    names = sorted(p.stem for p in (VAULT / "entities").glob("*.md"))
    tok = {n: set(re.split(r"[-_ ]+", n.lower())) for n in names}
    merged = {k.lower().replace(" ", "-") for k in
              ((yaml.safe_load(MERGES.read_text()) or {}) if MERGES.exists() else {})}
    merged |= {str(v).lower().replace(" ", "-") for v in
               ((yaml.safe_load(MERGES.read_text()) or {}).values() if MERGES.exists() else [])}
    known = set(json.loads(NOT_SAME.read_text())) if NOT_SAME.exists() else set()
    out = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if min(len(a), len(b)) < 3 or not (tok[a] <= tok[b] or tok[b] <= tok[a]):
                continue
            if a.lower() in merged and b.lower() in merged:
                continue
            if _pair_key(a, b) in known:
                continue
            out.append((a, b))
    return out


def queue_duplicates():
    before = len(list(QDIR.glob("*.json"))) if QDIR.exists() else 0
    for a, b in duplicate_candidates():
        da, db = a.replace("-", " "), b.replace("-", " ")
        ask(f"Are \"{da}\" and \"{db}\" the same thing?",
            why="The notes have very similar names. If they are the same, I will combine them.",
            kind="merge", payload={"a": da, "b": db},
            options=[{"label": f"Same, call it \"{da}\"", "value": "keep_a"},
                     {"label": f"Same, call it \"{db}\"", "value": "keep_b"},
                     {"label": "Different things", "value": "different"}])
    return len(list(QDIR.glob("*.json"))) - before if QDIR.exists() else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["dupes"]:
        print("queued", queue_duplicates(), "duplicate question(s);", count_open(), "open")
    else:
        for q in list_questions("open"):
            print(q["id"], q["kind"], q["question"])
