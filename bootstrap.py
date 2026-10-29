"""Overnight bootstrap: read raw documents, write summary notes + linked entity notes.

Writes ONLY under <vault>/summaries and <vault>/entities. Never touches raw/, inbox/, or git.
The model returns JSON (summary + entities); plain code does all file writing and linking.
Resumable: progress is kept in <state>/bootstrap.json.
"""
import json
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import yaml

import agent
from scrub import scrub

VAULT, STATE = agent.VAULT, agent.STATE
PROGRESS = STATE / "bootstrap.json"
HEAD_CHARS, TAIL_CHARS = 2400, 1200
MAX_ENTITIES = 8
MAX_TRIES = 3

SYS = ("You summarize one document for a personal documentation vault. Reply with ONLY JSON: "
       '{"title":"<short title>","summary":"<3-5 factual sentences>",'
       '"entities":[{"name":"<specific thing: host, service, device, project, tool>",'
       '"fact":"<what this document says about it, one short sentence>"}]}. '
       "Max 8 entities. Only specific named things, no generic words. Do not invent facts.")


def slug(text, limit=60):
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", t).strip("-")[:limit] or "untitled"


def clean(text):
    return scrub(str(text))[0].replace("[[", "[ [").replace("]]", "] ]").strip()


def load_progress():
    if PROGRESS.exists():
        return json.loads(PROGRESS.read_text())
    return {"done": [], "failed": {}, "entities": {}}


def save_progress(p):
    tmp = PROGRESS.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, indent=1, ensure_ascii=False))
    tmp.replace(PROGRESS)


def documents():
    """Curated memory notes first (compact, high value), then conversations newest first."""
    raw = VAULT / "raw"
    mem = sorted((raw / "claude-memory").rglob("*.md")) if (raw / "claude-memory").exists() else []
    conv = sorted((raw / "conversations").glob("*.md"), reverse=True) \
        if (raw / "conversations").exists() else []
    return [p.relative_to(VAULT).as_posix() for p in mem]


def read_doc(rel):
    text = (VAULT / rel).read_text(encoding="utf-8", errors="replace")
    m = re.match(r"\A---\s*\n.*?\n---\s*\n", text, re.DOTALL)
    body = text[m.end():] if m else text
    if len(body) > HEAD_CHARS + TAIL_CHARS:
        body = body[:HEAD_CHARS] + "\n\n[...middle omitted...]\n\n" + body[-TAIL_CHARS:]
    return body


def entity_file(prog, name):
    e = prog["entities"].get(slug(name).lower())
    return e["file"] if e else slug(name)


def write_summary(rel, data, prog):
    name = slug(Path(rel).stem, 80)
    out = VAULT / "summaries" / f"{name}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and f"source: {rel}\n" not in out.read_text(encoding="utf-8"):
        # same file name in another folder: name this summary from its full path instead
        name = slug(rel[:-3].replace("raw/", "", 1).replace("/", "-"), 80)
        out = VAULT / "summaries" / f"{name}.md"
    ents = []
    for e in (data.get("entities") or [])[:MAX_ENTITIES]:
        if isinstance(e, dict) and str(e.get("name", "")).strip():
            ents.append((clean(e["name"])[:60], clean(e.get("fact", ""))[:200]))
    fm = {"type": "summary", "source": rel, "generated": f"{datetime.now():%Y-%m-%d}",
          "confidence": "low", "title": clean(data.get("title") or Path(rel).stem)[:100]}
    links = ", ".join(f"[[entities/{entity_file(prog, n)}|{n}]]" for n, _ in ents) or "none"
    out.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n"
                   f"# {fm['title']}\n\n{clean(data.get('summary', ''))}\n\n"
                   f"**Source:** [[{rel[:-3]}]]\n\n**Entities:** {links}\n", encoding="utf-8")
    return f"summaries/{name}", ents


def write_entities(prog):
    d = VAULT / "entities"
    d.mkdir(parents=True, exist_ok=True)
    for e in prog["entities"].values():
        fm = {"type": "entity", "aliases": sorted(set(e["names"])), "confidence": "low"}
        lines = [f"- [[{doc}]]" + (f": {fact}" if fact else "") for doc, fact in e["docs"]]
        (d / f"{e['file']}.md").write_text(
            "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n"
            f"# {e['names'][0]}\n\nAuto-generated from {len(e['docs'])} document(s). "
            "Low confidence until reviewed.\n\n## Mentioned in\n\n" + "\n".join(lines) + "\n",
            encoding="utf-8")


def main():
    prog = load_progress()
    todo = [d for d in documents() if d not in prog["done"]
            and prog["failed"].get(d, 0) < MAX_TRIES]
    agent.log(f"bootstrap: {len(todo)} document(s) to go, {len(prog['done'])} done")
    for rel in todo:
        while not agent.model_ready():
            time.sleep(30)
        body = read_doc(rel)
        if len(body.strip()) < 150:
            prog["done"].append(rel)
            save_progress(prog)
            continue
        t0 = time.time()
        try:
            data = agent.ask(SYS, body, agent.Budget(1))
        except Exception as e:
            data = None
            agent.log(f"bootstrap FAILED {rel}: {type(e).__name__}: {str(e)[:100]}")
            time.sleep(60)
        if not data or not data.get("summary"):
            agent.log(f"bootstrap: no usable JSON for {rel} (try {prog['failed'].get(rel, 0) + 1})")
            prog["failed"][rel] = prog["failed"].get(rel, 0) + 1
            save_progress(prog)
            continue
        doc_link, ents = write_summary(rel, data, prog)
        for name, fact in ents:
            e = prog["entities"].setdefault(
                slug(name).lower(), {"file": slug(name), "names": [], "docs": []})
            if name not in e["names"]:
                e["names"].append(name)
            if not any(d == doc_link for d, _ in e["docs"]):
                e["docs"].append([doc_link, fact])
        write_entities(prog)
        prog["done"].append(rel)
        save_progress(prog)
        agent.log(f"bootstrap: {rel} -> {doc_link}, {len(ents)} entities, {time.time() - t0:.0f}s")
    agent.log("bootstrap: complete")


if __name__ == "__main__":
    main()
