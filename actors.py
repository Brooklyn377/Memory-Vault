"""Who made a change to the vault, for the History page.
The change journal runs as its own service and only sees that a file changed, so it cannot tell who changed it. The part of
the app that writes says so first: claim(path, who) adds a line here, and the journal reads it when it records the change.
A change nobody claimed falls back to the journal's guess from the path."""
import json
import time
from pathlib import Path

import agent

FILE = agent.STATE / "journal" / "claims.jsonl"
WINDOW = 60      # seconds a claim stays good for the write that follows it
KEEP = 2000      # lines kept; the file is only a short memory


def claim(rel, who):
    """Call just before writing the file at vault path rel (or state/... for a state file)."""
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "path": rel, "who": who}) + "\n")
        if FILE.stat().st_size > 400000:
            lines = FILE.read_text(encoding="utf-8").splitlines()[-KEEP:]
            tmp = FILE.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp.replace(FILE)
    except OSError:
        pass   # a history label must never stop a note from being saved


def who_of(rel, now=None):
    """The newest claim for this path made in the last WINDOW seconds: the name, or None."""
    now = now or time.time()
    try:
        lines = FILE.read_text(encoding="utf-8").splitlines()[-300:]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("path") == rel and 0 <= now - float(r.get("t", 0)) <= WINDOW:
            return r.get("who")
    return None
