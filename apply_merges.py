"""One-time merge of duplicate entity notes made by bootstrap, using <state>/merges.yaml.
Run with memory-bootstrap and memory-sweep STOPPED (both rewrite these files).
For each alias -> canonical pair: combine 'Mentioned in' lists and names into the canonical
entity, delete the alias note, and repoint every [[entities/<alias>]] link in the vault."""
import json
import re
import sys
from pathlib import Path

import yaml

import agent
import bootstrap
from bootstrap import slug

VAULT, STATE = agent.VAULT, agent.STATE


def main():
    mf = STATE / "merges.yaml"
    merges = (yaml.safe_load(mf.read_text()) or {}) if mf.exists() else {}
    prog = bootstrap.load_progress()
    ents, done = prog["entities"], []
    for alias, canon in merges.items():
        a, c = slug(alias).lower(), slug(canon).lower()
        if a == c or a not in ents:
            continue
        if c not in ents:
            print(f"skip {alias}: no canonical entity '{canon}' in bootstrap notes")
            continue
        ea, ec = ents[a], ents[c]
        for n in ea["names"]:
            if n not in ec["names"]:
                ec["names"].append(n)
        have = {d for d, _ in ec["docs"]}
        ec["docs"] += [x for x in ea["docs"] if x[0] not in have]
        (VAULT / "entities" / f"{ea['file']}.md").unlink(missing_ok=True)
        pat = re.compile(r"\[\[entities/" + re.escape(ea["file"]) + r"(\|[^\]]*)?\]\]")
        for p in VAULT.rglob("*.md"):
            rel = p.relative_to(VAULT).parts
            if rel[0] in ("raw", ".git"):
                continue
            text = p.read_text(encoding="utf-8")
            new = pat.sub(lambda m: f"[[entities/{ec['file']}{m.group(1) or ''}]]", text)
            if new != text:
                p.write_text(new, encoding="utf-8")
        del ents[a]
        done.append(f"{ea['file']} -> {ec['file']}")
    bootstrap.write_entities(prog)
    bootstrap.save_progress(prog)
    print("merged:", ", ".join(done) or "nothing to do")


if __name__ == "__main__":
    main()
