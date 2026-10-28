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


def _entry(key):
    p = QUEUE / f"{key}.json"
    return json.loads(p.read_text()) if p.exists() else None


def _park(key, data, tries):
    QUEUE.mkdir(parents=True, exist_ok=True)
    new = not (QUEUE / f"{key}.json").exists()
    (QUEUE / f"{key}.json").write_text(json.dumps({"data": data, "tries": tries}, ensure_ascii=False))
    if new:
        agent.log("review gate: reviewer not available, update parked until it is back")


def _clear(key):
    (QUEUE / f"{key}.json").unlink(missing_ok=True)


def queued(rel, chunk_hash, clarification=None):
    """A parked proposal for this chunk (so Qwen does not re-read it), or None."""
    e = _entry(_key(rel, chunk_hash, clarification))
    return e["data"] if e else None


def waiting():
    return len(list(QUEUE.glob("*.json"))) if QUEUE.exists() else 0


def _review(data):
    """The reviewer's verdict dict, or None if it is down or gave no usable answer."""
    facts = [f for f in (data.get("facts") or []) if isinstance(f, dict)]
    if not reviewer.ready():
        return None
    lines = [f"{i}. [{f.get('kind', 'info')}] {f.get('entity')}: {f.get('fact')}\n   quote: \"{str(f.get('quote', ''))[:__import__('tuning').get('review.quote_chars')]}\""
             for i, f in enumerate(facts)]
    user = f"RECAP: {str(data.get('recap', ''))[:__import__('tuning').get('review.recap_chars')]}\n\nFACTS:\n" + "\n".join(lines)
    try:
        v = reviewer.rask(GATE_SYS, user, tokens=6000)
    except Exception as e:
        agent.log(f"review gate error: {type(e).__name__}: {str(e)[:80]}")
        return None
    if not v or v.get("verdict") not in ("accept", "reject"):
        return None
    v["drop"] = {int(n) for n in (v.get("drop") or []) if str(n).lstrip("-").isdigit()}
    return v


def settle(rel, idx, chunk_hash, data, redo, clarification=None):
    """Returns (data, wait). wait=True means: nothing may be written yet, try this chunk again later."""
    key = _key(rel, chunk_hash, clarification)
    facts = [f for f in (data.get("facts") or []) if isinstance(f, dict)]
    if not facts:
        _clear(key)
        return data, False
    e = _entry(key)
    tries = e["tries"] if e else 0
    while True:
        v = _review(data)
        if v is None:
            _park(key, data, tries)
            return None, True
        facts = [f for f in (data.get("facts") or []) if isinstance(f, dict)]
        reason = str(v.get("reason", ""))[:200]
        bad = v["verdict"] == "reject" or bool(v["drop"] & set(range(len(facts))))
        if not bad:
            _append(LOG, {"rel": rel, "chunk": idx, "verdict": "accept", "tries": tries, "kept": len(facts), "reason": reason})
            _clear(key)
            return data, False
        if tries < RETRIES:
            tries += 1
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
            if new and "recap" in new:
                data = new
                continue
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
