"""History of answered Ask cards: status, undo, change, and the 30-day clean-up."""
import json
import shutil

import yaml
from datetime import datetime

import bootstrap
import hygiene
import questions
from questions import STATE, VAULT

KEEP_DAYS = 30
TRASH = STATE / "trash"


def _unlist(path, name):
    s = hygiene._load(path)
    s.discard(hygiene.slug(name).lower())
    path.write_text(json.dumps(sorted(s)))


def restore(name):
    d = TRASH / name
    if not d.is_dir():
        return False
    for sub in ("entities", "timelines"):
        src = d / f"{sub}.md"
        if src.exists():
            dst = VAULT / sub / f"{name}.md"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    try:
        saved = json.loads((d / "meta.json").read_text()).get("progress")
    except (OSError, ValueError):
        saved = None
    if saved:
        prog = bootstrap.load_progress()
        prog["entities"][saved["key"]] = saved["entry"]
        bootstrap.save_progress(prog)
    shutil.rmtree(d, ignore_errors=True)
    return True


def _done_inbox():
    try:
        return set(json.loads((STATE / "agent_done.json").read_text()))
    except (OSError, ValueError):
        return set()


def _working():
    try:
        return json.loads((STATE / "agent_working.json").read_text())
    except (OSError, ValueError):
        return None


def _alias(q):
    p, a = q["payload"], q.get("answer")
    return p["b"] if a == "keep_a" else p["a"] if a == "keep_b" else None


def _merged(alias):
    """True once apply_merges has combined the alias into its canonical note."""
    return bootstrap.slug(alias).lower() not in bootstrap.load_progress()["entities"]


def view(q):
    """A closed question plus what its history row shows: stage, result, which buttons apply."""
    a, kind = q.get("answer"), q["kind"]
    out = dict(q, stage="done", result="", undo=False, flip=None)
    if q["status"] == "dismissed":
        out.update(result="Skipped", undo=kind not in ("filecheck", "inboxcheck", "curatecheck"))
    elif kind == "hygiene":
        ent = q["payload"]["entity"]
        if a == "keep":
            out.update(result="Kept", undo=True, flip="drop")
        else:
            gone = not (VAULT / "entities" / f"{ent}.md").exists()
            out.update(result="Dropped", undo=True, flip="keep", stage="done" if gone else "queued")
    elif kind == "merge":
        p = q["payload"]
        if a == "different":
            out.update(result="Marked as different things", undo=True)
        else:
            res = f"Merged {p['b']} into {p['a']}" if a == "keep_a" else f"Merged {p['a']} into {p['b']}"
            gone = _merged(_alias(q))
            out.update(result=res, stage="done" if gone else "queued", undo=not gone)
    elif kind in ("filecheck", "inboxcheck", "curatecheck"):
        out.update(result="Trying again" if a == "retry" else "Skipped by you")
    elif kind == "curate" and (q.get("payload") or {}).get("act") == "prune":
        out.update(result="Pruned by the curator: " + str(q.get("why", ""))[:120], undo=True)
    else:
        files = list((VAULT / "inbox").glob(f"*-answer-{q['id']}.md"))
        res = "Filed: " + str(a)[:80]
        if not files or files[0].name in _done_inbox():
            out.update(result=res + " (read by the agent)")
        else:
            out.update(result=res, undo=True,
                       stage="working" if _working() == files[0].name else "queued")
    return out


def undo(qid):
    q = questions.get(qid)
    if not q:
        raise FileNotFoundError(qid)
    if q["status"] == "open":
        raise ValueError("already open")
    if q["status"] == "answered":
        kind, a, p = q["kind"], q.get("answer"), q["payload"]
        if kind == "hygiene":
            _unlist(hygiene.KEEP if a == "keep" else hygiene.BLOCK, p["entity"])
            if a == "drop":
                restore(p["entity"])
        elif kind == "merge" and a == "different":
            key = questions._pair_key(p["a"], p["b"])
            d = json.loads(questions.NOT_SAME.read_text()) if questions.NOT_SAME.exists() else []
            questions.NOT_SAME.write_text(json.dumps([x for x in d if x != key]))
        elif kind == "merge":
            if _merged(_alias(q)):
                raise ValueError("already merged, the notes were combined")
            d = (yaml.safe_load(questions.MERGES.read_text()) or {}) if questions.MERGES.exists() else {}
            d.pop(_alias(q), None)
            questions.MERGES.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True))
        elif kind == "curate" and p.get("act") == "prune":
            _unlist(hygiene.BLOCK, p["entity"])
            hygiene.add_keep(p["entity"])
            restore(p["entity"])
            questions._path(qid).unlink(missing_ok=True)
            return q
        elif kind in ("filecheck", "inboxcheck", "curatecheck"):
            raise ValueError("this answer cannot be undone")
        else:
            files = list((VAULT / "inbox").glob(f"*-answer-{qid}.md"))
            if not files or files[0].name in _done_inbox():
                raise ValueError("the agent already read it")
            files[0].unlink()
    q["status"], q["answer"] = "open", None
    q.pop("answered", None)
    questions._save(q)
    return q


def change(qid, new):
    q = questions.get(qid)
    if not q or q["kind"] != "hygiene" or q["status"] != "answered":
        raise ValueError("nothing to change")
    if new not in ("keep", "drop") or new == q.get("answer"):
        raise ValueError("pick the other answer")
    undo(qid)
    return questions.answer(qid, new)


def purge():
    """Erase trash copies and closed cards older than KEEP_DAYS."""
    cut = datetime.now().timestamp() - KEEP_DAYS * 86400
    gone = 0
    if TRASH.is_dir():
        for d in TRASH.iterdir():
            try:
                t = datetime.fromisoformat(json.loads((d / "meta.json").read_text())["dropped"]).timestamp()
            except (OSError, ValueError, KeyError):
                t = d.stat().st_mtime
            if t < cut:
                shutil.rmtree(d, ignore_errors=True)
                gone += 1
    for q in questions.list_questions():
        if q["status"] == "open":
            continue
        try:
            t = datetime.fromisoformat(q["answered"]).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        if t < cut:
            questions._path(q["id"]).unlink(missing_ok=True)
            gone += 1
    return gone


if __name__ == "__main__":
    print("erased", purge())
