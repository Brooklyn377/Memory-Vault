"""Entity hygiene: find entity notes that are probably noise, ask you keep/drop, remember the answer.

Rules are plain code (no model). Nothing is deleted without your answer on the Ask page.
  <state>/entity_keep.json       names you said to keep (never asked again)
  <state>/entity_blocklist.json  names you said to drop (deleted, links turned to plain text, never recreated)
CLI:  hygiene.py list | queue
"""
import json
import re
import sys

import agent
import bootstrap
import questions
from bootstrap import slug

VAULT, STATE = agent.VAULT, agent.STATE
BLOCK, KEEP = STATE / "entity_blocklist.json", STATE / "entity_keep.json"
GENERIC = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
           "january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december", "today", "tomorrow", "yesterday", "week", "day", "night",
           "red", "green", "blue", "yellow", "white", "black", "orange", "purple", "gray", "grey", "wire",
           "wires", "cable", "cables", "tape", "thing", "things", "stuff", "item", "items", "file", "files",
           "command", "commands", "shell", "script", "code", "config", "setting", "settings", "step", "steps",
           "yes", "no", "user", "users", "name", "value", "error", "output", "input", "text", "list"}
FILE_SUFFIX = re.compile(r"-(py|ts|json|md|yaml|yml|txt|sh|bin|docx|pdf|csv|html|css|conf|log|zip)$", re.I)


def _load(p):
    try:
        return set(json.loads(p.read_text())) if p.exists() else set()
    except ValueError:
        return set()


def blocked():
    return _load(BLOCK)


def blocklist_text():
    return BLOCK.read_text() if BLOCK.exists() else ""


def _add(p, name):
    s = _load(p)
    s.add(slug(name).lower())
    p.write_text(json.dumps(sorted(s)))


def add_block(name):
    _add(BLOCK, name)


def add_keep(name):
    _add(KEEP, name)


def _is_agent_note(text):
    return text.startswith("---") and "confidence: low" in text[:400]


def reason(name, docs, has_timeline):
    """Why this entity looks like noise, or None. Higher rules first."""
    tokens = [t for t in re.split(r"[-_ ]+", name.lower()) if t]
    if tokens and all(t in GENERIC for t in tokens):
        return "a generic word, not a specific thing", 1
    if any(t in GENERIC for t in tokens) and len(tokens) <= 2 and docs <= 1:
        return "mostly generic words and mentioned in only one chat", 2
    if FILE_SUFFIX.search(name):
        return "looks like a file name rather than a thing", 3
    if name.isupper() and len(name) <= 3:
        return "a very short abbreviation", 4
    return None


def candidates():
    keep, drop, out = _load(KEEP), blocked(), []
    for p in sorted((VAULT / "entities").glob("*.md")):
        text = p.read_text(encoding="utf-8")
        name = p.stem
        if slug(name).lower() in keep | drop or not _is_agent_note(text):
            continue
        if FILE_SUFFIX.search(name):
            continue
        docs = len(re.findall(r"^- \[\[", text, re.M))
        has_tl = (VAULT / "timelines" / p.name).exists()
        r = reason(name, docs, has_tl)
        if r:
            facts = re.findall(r"^- \[\[[^\]]+\]\]: (.+)$", text, re.M)
            out.append((r[1], name, r[0], facts[0] if facts else ""))
    return sorted(out)


def queue(limit=None):
    """Ask about the best candidates, as many as the Ask queue has room for."""
    n = 0
    for _, name, why, fact in candidates():
        if limit is not None and n >= limit:
            break
        label = name.replace("-", " ")
        qid = questions.ask(
            f'Keep "{label}" as its own note?',
            why="It looks like " + why.removeprefix("looks like ") + "." + (f' The only thing noted: "{fact[:160]}"' if fact else ""),
            kind="hygiene", payload={"entity": name},
            options=[{"label": "Keep it", "value": "keep"}, {"label": "Drop it", "value": "drop"}])
        if qid is None:  # queue full: try again later
            break
        n += 1
    return n


def apply_blocklist():
    """Delete blocked entity notes and their timelines, and turn links to them into plain text."""
    drop = blocked()
    if not drop:
        return 0
    prog = bootstrap.load_progress()
    files = set()
    saved = {}
    for k in drop:
        e = prog["entities"].pop(k, None)
        if e:
            files.add(e["file"])
            saved[e["file"]] = {"key": k, "entry": e}
    for p in (VAULT / "entities").glob("*.md"):
        if slug(p.stem).lower() in drop and _is_agent_note(p.read_text(encoding="utf-8")):
            files.add(p.stem)
    import json, shutil
    from datetime import datetime
    for f in files:
        d = STATE / "trash" / f
        got = f in saved
        for sub in ("entities", "timelines"):
            src = VAULT / sub / f"{f}.md"
            if src.exists():
                d.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, d / f"{sub}.md")
                got = True
            src.unlink(missing_ok=True)
        if got:
            d.mkdir(parents=True, exist_ok=True)
            (d / "meta.json").write_text(json.dumps({"dropped": datetime.now().isoformat(timespec="seconds"), "progress": saved.get(f)}))
    if files:
        pat = re.compile(r"\[\[(?:entities|timelines)/(" + "|".join(re.escape(f) for f in files)
                         + r")(?:\|([^\]]*))?\]\]")
        for p in VAULT.rglob("*.md"):
            if p.relative_to(VAULT).parts[0] in ("raw", ".git"):
                continue
            t = p.read_text(encoding="utf-8")
            new = pat.sub(lambda m: m.group(2) or m.group(1).replace("-", " "), t)
            if new != t:
                p.write_text(new, encoding="utf-8")
    bootstrap.save_progress(prog)
    return len(files)


if __name__ == "__main__":
    if sys.argv[1:] == ["queue"]:
        print("queued", queue(), "question(s);", questions.count_open(), "open")
    else:
        for pri, name, why, _ in candidates():
            print(pri, name, "-", why)
