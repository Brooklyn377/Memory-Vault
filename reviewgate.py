"""Review gate between the sweep model (Qwen) and the vault. See review_queue/ for parked updates."""
import hashlib
import json
from datetime import datetime

import agent
import reviewer

STATE = agent.STATE
QUEUE = STATE / "review_queue"
LOG = STATE / "review_log.jsonl"
CHANGED = STATE / "changed.jsonl"
RETRIES = int(__import__("tuning").get("review.retries"))

GATE_SYS = (
    "You are the reviewer for a notes agent. A small model read one part of a chat and wants to record FACTS "
    "about named entities in a person's notes vault. You see its recap and each fact with the exact "
    "quote it claims supports it. Judge only what you can see: drop a fact if it is not what the quote says, "
    "is about nothing specific (a stray word, a file name, generic advice), or names a thing wrongly. "
    "Reject the whole update only if most of it is wrong or empty. Do not drop a fact just because it is minor. "
    'Reply with ONLY JSON: {"verdict":"accept|reject","drop":[<fact numbers to remove>],'
    '"reason":"<one short sentence>"}.')


def _append(path, row):
    STATE.mkdir(parents=True, exist_ok=True)
    row = {"at": datetime.now().isoformat(timespec="seconds"), **row}
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _key(rel, chunk_hash, clarification):
    extra = (clarification or {}).get("a", "")
    return hashlib.sha1(f"{rel}|{chunk_hash}|{extra}".encode()).hexdigest()[:16]


SEEN = STATE / "review_seen.json"   # verdicts the Sweeper has already picked up: key -> the verdict's time


def _entry(key):
    p = QUEUE / f"{key}.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _write(key, entry):
    QUEUE.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE / f"{key}.tmp"
    tmp.write_text(json.dumps(entry, ensure_ascii=False))
    tmp.replace(QUEUE / f"{key}.json")


def _park(key, rel, data, tries):
    """Set a proposal aside for the Reviewer. The Sweeper does not wait for the review: it goes on with other
    conversations, and picks this one up first when the verdict is in."""
    new = _entry(key) is None
    _write(key, {"rel": rel, "data": data, "tries": tries})
    if new:
        agent.log(f"review gate: {rel.split('/')[-1][:40]} parked for the Reviewer; the Sweeper moves on")


def _clear(key):
    (QUEUE / f"{key}.json").unlink(missing_ok=True)


def queued(rel, chunk_hash, clarification=None):
    """A parked proposal for this chunk (so Qwen does not re-read it), or None."""
    e = _entry(_key(rel, chunk_hash, clarification))
    return e["data"] if e else None


def waiting():
    return len(list(QUEUE.glob("*.json"))) if QUEUE.exists() else 0


def _entries():
    for p in sorted(QUEUE.glob("*.json"), key=lambda x: x.stat().st_mtime) if QUEUE.exists() else []:
        try:
            yield p.stem, json.loads(p.read_text())
        except (OSError, ValueError):
            continue


def _seen():
    try:
        return json.loads(SEEN.read_text())
    except (OSError, ValueError):
        return {}


def ready_docs(unseen=True):
    """Conversations with a finished review the Sweeper has not picked up yet, oldest verdict first, as (rel, key)."""
    seen = _seen()
    out = [(e["verdict"]["at"], e.get("rel"), key) for key, e in _entries()
           if e.get("verdict") and e.get("rel") and (not unseen or seen.get(key) != e["verdict"]["at"])]
    return [(rel, key) for _, rel, key in sorted(out)]


def mark_seen(key):
    e = _entry(key)
    if e and e.get("verdict"):
        seen = {k: v for k, v in _seen().items() if (QUEUE / f"{k}.json").exists()}
        seen[key] = e["verdict"]["at"]
        SEEN.write_text(json.dumps(seen))


def _review(data):
    """The reviewer's verdict dict, or None if it is down. An unusable answer is asked for again until it is usable."""
    facts = [f for f in (data.get("facts") or []) if isinstance(f, dict)]
    if not reviewer.ready():
        return None
    lines = [f"{i}. [{f.get('kind', 'info')}] {f.get('entity')}: {f.get('fact')}\n   quote: \"{str(f.get('quote', ''))[:__import__('tuning').get('review.quote_chars')]}\""
             for i, f in enumerate(facts)]
    user = f"RECAP: {str(data.get('recap', ''))[:__import__('tuning').get('review.recap_chars')]}\n\nFACTS:\n" + "\n".join(lines)
    attempt = 0
    while True:   # asked again, with what was wrong, until the answer is usable
        try:
            v = reviewer.rask(GATE_SYS, user if attempt == 0 else user + '\n\nYour last answer was not valid JSON with a "verdict" of accept or reject. Reply with the JSON only.', tokens=6000)
        except Exception as e:
            agent.log(f"review gate error: {type(e).__name__}: {str(e)[:80]}")
            return None
        if v and v.get("verdict") in ("accept", "reject"):
            break
        attempt += 1
    v["drop"] = sorted({int(n) for n in (v.get("drop") or []) if str(n).lstrip("-").isdigit()})
    return v


def review_next():
    """Run by the Reviewer's worker: review the oldest parked proposal that has no verdict yet, and store the verdict
    with it. -> 1 if it reviewed one, else 0 (nothing to review, or the Reviewer is not available)."""
    for key, e in _entries():
        if e.get("verdict") or e.get("stuck"):
            continue
        v = _review(e["data"])
        now = _entry(key)
        if not now or now.get("data") != e["data"]:   # replaced or removed while the Reviewer was thinking
            return 1
        if v is None:
            return 0   # the Reviewer is not available (or failed): try again later
        now["verdict"] = dict(v, at=datetime.now().isoformat(timespec="seconds"))
        _write(key, now)
        return 1
    return 0


def decide_stuck(key, text):
    """Your answer on a chunk the Reviewer could not judge: try again, or drop its facts (the chunk is read again)."""
    e = _entry(key)
    if not e:
        return
    if text == "skip":
        _clear(key)
    else:
        e.pop("stuck", None)
        e["unusable"] = 0
        _write(key, e)


def settle(rel, idx, chunk_hash, data, redo, clarification=None):
    """Returns (data, wait). wait=True means: nothing may be written yet, this chunk goes on later. The review itself is
    done by the Reviewer's worker; until its verdict is in, the chunk is parked and the Sweeper does something else."""
    key = _key(rel, chunk_hash, clarification)
    facts = [f for f in (data.get("facts") or []) if isinstance(f, dict)]
    if not facts:
        _clear(key)
        return data, False
    e = _entry(key)
    tries = e["tries"] if e else 0
    v = e.get("verdict") if e else None
    if v is None:
        if e is None:
            _park(key, rel, data, tries)
        return None, True
    v = dict(v, drop=set(v.get("drop") or []))
    reason = str(v.get("reason", ""))[:200]
    bad = v["verdict"] == "reject" or bool(v["drop"] & set(range(len(facts))))
    if not bad:
        _append(LOG, {"rel": rel, "chunk": idx, "verdict": "accept", "tries": tries, "kept": len(facts), "reason": reason})
        _clear(key)
        return data, False
    if tries < RETRIES:
        if v["verdict"] == "reject":
            fb = f"The reviewer rejected your whole answer: {reason}"
        else:
            fb = f"The reviewer did not accept these facts ({reason}):\n" + "\n".join(
                f"- {facts[i].get('entity')}: {facts[i].get('fact')}" for i in sorted(v["drop"]) if i < len(facts))
        try:
            new = redo(fb)
        except Exception as ex:
            agent.log(f"review gate: retry failed: {type(ex).__name__}: {str(ex)[:80]}")
            new = None
        if new and "recap" in new:   # the new answer goes to the Reviewer the same way
            _write(key, {"rel": rel, "data": new, "tries": tries + 1})
            return None, True
    out = dict(data)
    out["facts"] = [] if v["verdict"] == "reject" else [f for i, f in enumerate(facts) if i not in v["drop"]]
    _append(LOG, {"rel": rel, "chunk": idx, "verdict": "dropped" if not out["facts"] else "trimmed", "tries": tries,
                  "offered": len(facts), "kept": len(out["facts"]), "reason": reason})
    agent.log(f"review gate: {rel.split('/')[-1][:40]} chunk {idx + 1}: kept {len(out['facts'])}/{len(facts)} after {tries} retry - {reason}")
    _clear(key)
    return out, False


def note_changed(files, why):
    """Record the files an accepted chunk changed, with the reason, for the publisher."""
    if files:
        _append(CHANGED, {"files": sorted(set(files)), "why": why[:300]})
