"""Dry-run agent: turn one inbox note into verified edit PROPOSALS. Never writes to the vault."""
import json
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml

import config
from graph import Vault

CFG = config.load()
VAULT = Path(CFG["paths"]["vault"])
STATE = Path(CFG["paths"]["state_dir"])
PROPOSALS = STATE / "proposals"
M = CFG["model"]
A = CFG["agent"]
EVIDENCE = CFG["evidence"]
import settings

SKIP_PREFIXES = ("raw/", "inbox/")  # raw sources and inbox are evidence, never edit targets
MAX_NOTE_CHARS = 6000


def log(msg):
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "activity.log").open("a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} agent: {msg}\n")


class Budget:
    """Hard cap on model calls per run (config agent.max_tool_calls)."""
    def __init__(self, n):
        self.left = n

    def spend(self):
        if self.left <= 0:
            raise RuntimeError("model call budget used up")
        self.left -= 1


def ask(system, user, budget):
    budget.spend()
    body = {
        "model": M["name"],
        "temperature": settings.get("qwen")["temperature"],
        "max_tokens": settings.get("qwen")["max_tokens"]["ask"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "chat_template_kwargs": {"enable_thinking": settings.get("qwen")["thinking"]},
    }
    import thinklog
    text = thinklog.chat("qwen", M["endpoint"], body, "ask", slot=1).get("content") or ""
    return parse_json(text)


def parse_json(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            try:
                return json.loads(text[start:i + 1])
            except json.JSONDecodeError:
                return None
    return None


def read_inbox_note(path):
    raw = Path(path).read_text(encoding="utf-8")
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
    meta = yaml.safe_load(m.group(1)) if m else {}
    return (meta or {}), raw[m.end():] if m else raw


def confidence_for(source_type):
    """Confidence comes from CODE and the evidence rules, never from the model's say-so."""
    for level in ("high", "medium", "low"):
        if source_type in EVIDENCE[level]:
            return level
    return "low"


EXTRACT_SYS = (
    "You extract facts from a short note for a documentation vault. Reply with ONLY JSON: "
    '{"claims":[{"entity":"<thing the fact is about>","statement":"<one sentence>",'
    '"kind":"removed|added|changed|other"}]}. Max 3 claims. Use the note\'s own words.')

JUDGE_SYS = (
    "You maintain a documentation note. Given a NEW FACT and the NOTE, decide if the note is "
    "now wrong or out of date because of the fact. Reply with ONLY JSON: "
    '{"needs_change":true|false,"old":"<exact text copied from the note>",'
    '"new":"<replacement text, may be empty to delete>","reason":"<one sentence>"}. '
    "'old' MUST be copied character-for-character from the note and be a single line or phrase. "
    "If the note is still correct, answer needs_change false.")


def candidates(vault, entity, limit):
    """Notes that might be affected: the entity's own note, its link neighbours, and unlinked mentions."""
    ranked, seen = [], set()

    def add(rel, why):
        if rel and rel not in seen and not rel.startswith(SKIP_PREFIXES):
            seen.add(rel)
            ranked.append((rel, why))

    own = vault.find(entity)
    if own:
        add(own, "entity note")
        hood = vault.neighborhood(own, __import__("tuning").get("agent.hop_depth"))
        for rel in hood["backlinks"]:
            add(rel, f"links to {own}")
        for rel in hood["within_hops"]:
            add(rel, f"within {__import__('tuning').get('agent.hop_depth')} hop of {own}")
    for hit in vault.find_mentions([entity], limit=limit * 2):
        add(hit["path"], "mentions it unlinked")
    return ranked[:limit]


def verify(proposal, note_text):
    """Mechanical check: the text to replace must exist exactly once."""
    old = proposal.get("old") or ""
    return bool(old.strip()) and note_text.count(old) == 1 and old != proposal.get("new")


def run(note_path):
    budget = Budget(__import__("tuning").get("agent.max_tool_calls"))
    meta, body = read_inbox_note(note_path)
    source_type = meta.get("type", "user-statement")
    conf = confidence_for(source_type)
    vault = Vault(VAULT)
    out = {"source": str(Path(note_path).relative_to(VAULT)) if str(note_path).startswith(str(VAULT))
           else str(note_path),
           "source_type": source_type, "confidence": conf, "dry_run": A.get("dry_run", True),
           "created": datetime.now().isoformat(timespec="seconds"),
           "claims": [], "proposals": [], "rejected": [], "stopped": None}

    extracted = ask(EXTRACT_SYS, body[:MAX_NOTE_CHARS], budget)
    claims = (extracted or {}).get("claims") or []
    out["claims"] = claims[:3]
    log(f"{note_path}: {len(out['claims'])} claim(s), confidence {conf}")

    try:
        for claim in out["claims"]:
            entity, statement = claim.get("entity", ""), claim.get("statement", "")
            if not entity:
                continue
            for rel, why in candidates(vault, entity, __import__("tuning").get("agent.max_candidates")):
                text = vault.body[rel][:MAX_NOTE_CHARS]
                verdict = ask(JUDGE_SYS, f"NEW FACT: {statement}\n\nNOTE ({rel}):\n{text}", budget)
                if not verdict or not verdict.get("needs_change"):
                    continue
                if verify(verdict, vault.body[rel]):
                    out["proposals"].append({
                        "path": rel, "why_considered": why, "old": verdict["old"],
                        "new": verdict.get("new", ""), "reason": verdict.get("reason", ""),
                        "evidence": out["source"], "confidence": conf})
                else:
                    out["rejected"].append({"path": rel, "reason": "old text not found exactly once"})
    except RuntimeError as e:
        out["stopped"] = str(e)

    PROPOSALS.mkdir(parents=True, exist_ok=True)
    dest = PROPOSALS / f"{datetime.now():%Y%m%d-%H%M%S}-{Path(note_path).stem}.json"
    dest.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    log(f"{note_path}: {len(out['proposals'])} proposal(s), {len(out['rejected'])} rejected "
        f"-> {dest.name}" + (f" (stopped: {out['stopped']})" if out["stopped"] else ""))
    return out, dest


def model_ready():
    """llama-server answers /health with 200 only once the model is fully loaded."""
    import swap
    try:
        import remote
        if remote.enabled("qwen"):
            return remote.health("qwen") == "ready"
    except Exception:
        pass
    if swap.holding():
        return True
    try:
        import turns
        if turns.managed("qwen"):   # local and taking turns: a call waits for its turn
            return True
    except Exception:
        pass
    try:
        import power
        if power.off("qwen"):
            return True
    except Exception:
        pass
    base = M["endpoint"].rstrip("/")
    try:
        with urllib.request.urlopen(base[:-3] + "/health" if base.endswith("/v1")
                                    else base + "/health", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def watch():
    """Process each new inbox note once, oldest first. A note is marked done only after
    its run finishes without an exception, so a model that is still loading is retried."""
    done_file = STATE / "agent_done.json"
    log("watcher started")
    while True:
        if not model_ready():
            time.sleep(15)  # model still loading (or down): wait quietly, no failed attempts
            continue
        done =set(json.loads(done_file.read_text())) if done_file.exists() else set()
        failed = False
        for note in sorted((VAULT / "inbox").glob("*.md")):
            if note.name in done:
                continue
            (STATE / "agent_working.json").write_text(json.dumps(note.name))
            try:
                run(note)
            except Exception as e:  # model down, bad file, etc.
                log(f"FAILED {note.name}: {type(e).__name__}: {str(e)[:150]}")
                failed = True
                break
            done.add(note.name)
            done_file.write_text(json.dumps(sorted(done)))
            (STATE / "agent_working.json").unlink(missing_ok=True)
        (STATE / "agent_working.json").unlink(missing_ok=True)
        time.sleep(300 if failed else 30)


if __name__ == "__main__":
    if sys.argv[1:] == ["--watch"]:
        watch()
    elif len(sys.argv) == 2:
        _, dest = run(sys.argv[1])
        print(dest)
    else:
        sys.exit("usage: agent.py --watch | agent.py path/to/inbox-note.md")
