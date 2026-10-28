"""Change journal: records every file created, changed or deleted in the vault and a few state files, with what changed.
Event-driven (inotify), no polling. The History page reads it.
  journal.py --watch    run as a service
  journal.py --recent   print the last few records"""
import difflib
import json
import os
import sys
import time
from pathlib import Path

from inotify_simple import INotify, flags

import agent

VAULT, STATE = agent.VAULT, agent.STATE
OUT = STATE / "journal"
MAX_READ = 400000
MASK = flags.CLOSE_WRITE | flags.MOVED_TO | flags.MOVED_FROM | flags.CREATE | flags.DELETE
STATE_DIRS = ("prompts", "todo", "questions", "proposals", "trash", "inbox_queue", "file_queue", "curator_queue", "review_queue")
STATE_FILES = {"settings.json", "curator_cfg.json", "tuning.json", "entity_blocklist.json", "entity_keep.json", "merges.yaml", "not_same.json"}
NICE = {"qwen": "Sweeper", "gpt": "Reviewer", "cur": "Curator"}
SKIP_SUFFIX = (".tmp", ".part", ".swp", "~", ".lock")
MISSING = object()


def skip_name(n):
    return n.startswith(".") or n.endswith(SKIP_SUFFIX)


def in_scope(p):
    try:
        parts = p.relative_to(VAULT).parts
        if not parts:
            return True
        if parts[0] == ".git":
            return False
        if parts[0] == "raw":
            return len(parts) == 1 or parts[1] == "files"
        return True
    except ValueError:
        pass
    try:
        parts = p.relative_to(STATE).parts
    except ValueError:
        return False
    if not parts:
        return True
    return parts[0] in STATE_DIRS or (len(parts) == 1 and p.name in STATE_FILES)


def rel_path(p):
    try:
        return p.relative_to(VAULT).as_posix()
    except ValueError:
        return "state/" + p.relative_to(STATE).as_posix()


def read_text(p):
    try:
        if p.stat().st_size > MAX_READ:
            return None
        return p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def fmt(v):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= 60 else s[:57] + "..."


def jdiff(a, b, pre):
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            label = k if pre else NICE.get(k, k)
            out += jdiff(a.get(k, MISSING), b.get(k, MISSING), f"{pre}.{label}" if pre else label)
    elif a != b:
        out.append(f"{pre}: {'(not set)' if a is MISSING else fmt(a)} → {'(removed)' if b is MISSING else fmt(b)}")
    return out


def describe(p, op, old, new):
    if (op != "deleted" and new is None) or (op == "deleted" and old is None):
        return 0, 0, [], "not a text file, or too large to compare"
    if op == "updated" and p.name in ("settings.json", "curator_cfg.json", "tuning.json"):
        try:
            ch = jdiff(json.loads(old), json.loads(new), "")
            return len(ch), 0, ch[:40], "; ".join(ch[:4])
        except ValueError:
            pass
    if op == "updated" and p.name in ("entity_blocklist.json", "entity_keep.json"):
        try:
            a, b = set(json.loads(old)), set(json.loads(new))
            ch = [f"added {x}" for x in sorted(b - a)] + [f"removed {x}" for x in sorted(a - b)]
            return len(ch), 0, ch[:40], "; ".join(ch[:4])
        except (ValueError, TypeError):
            pass
    d = [l for l in difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(), lineterm="", n=0)
         if not l.startswith(("---", "+++", "@@"))]
    add, rem = sum(1 for l in d if l.startswith("+")), sum(1 for l in d if l.startswith("-"))
    return add, rem, [l[:200] for l in d[:40]], f"+{add} -{rem}"


def who_for(rel, lines):
    text = "\n".join(lines)
    added = [l for l in lines if l.startswith("+")]
    if rel.startswith("curator/") or "curator:start" in text or "Related (added by the curator)" in text:
        return "curator"
    if rel.startswith("state/todo/"):
        return "you" if any('"processed": false' in l for l in added) else "reviewer"
    if rel.startswith("state/questions/"):
        return "you" if any('"status": "answered"' in l or '"status": "dismissed"' in l for l in added) else "system"
    if rel.startswith("state/"):
        return "you" if rel.split("/")[1] in STATE_FILES or rel.split("/")[1] == "prompts" else "system"
    if rel.startswith("tasks/"):
        return "reviewer"
    if rel.startswith("inbox/"):
        return "you" if ("-chat-" in rel or "-answer-" in rel) else "reviewer"
    if rel.startswith("raw/files/"):
        return "system"
    if rel.startswith("files/"):
        return "reviewer"
    if rel.startswith(("entities/", "summaries/", "timelines/")):
        return "sweeper"
    return "system"


class Journal:
    def __init__(self):
        self.ino = INotify()
        self.wd, self.cache = {}, {}

    def write(self, row):
        OUT.mkdir(parents=True, exist_ok=True)
        f = OUT / time.strftime("journal-%Y%m.jsonl", time.gmtime())
        fresh = not f.exists()
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if fresh:
            for old in sorted(OUT.glob("journal-*.jsonl"))[:-6]:
                old.unlink(missing_ok=True)

    def record(self, p, op, old, new):
        rel = rel_path(p)
        add, rem, lines, summary = describe(p, op, old, new)
        self.write({"t": time.time(), "path": rel, "op": op, "add": add, "rem": rem, "summary": summary,
                    "diff": lines, "who": who_for(rel, lines)})

    def add_dir(self, d, scan=False):
        for root, dirs, files in os.walk(d):
            r = Path(root)
            dirs[:] = [x for x in dirs if in_scope(r / x)]
            if not in_scope(r):
                continue
            try:
                self.wd[self.ino.add_watch(str(r), MASK)] = r
            except OSError as e:
                print("cannot watch", r, e, flush=True)
                continue
            for f in files:
                p = r / f
                if skip_name(f) or not in_scope(p) or p in self.cache:
                    continue
                self.cache[p] = read_text(p)
                if scan:
                    self.record(p, "created", None, self.cache[p])

    def changed(self, p):
        new, old = read_text(p), self.cache.get(p, MISSING)
        if old is not MISSING and old == new and new is not None:
            return
        self.cache[p] = new
        self.record(p, "created" if old is MISSING else "updated", None if old is MISSING else old, new)

    def deleted(self, p):
        old = self.cache.pop(p, MISSING)
        if old is not MISSING:
            self.record(p, "deleted", old, None)

    def boot(self):
        self.add_dir(VAULT)
        try:
            self.wd[self.ino.add_watch(str(STATE), MASK)] = STATE
        except OSError as e:
            print("cannot watch", STATE, e, flush=True)
        for n in STATE_FILES:
            if (STATE / n).exists():
                self.cache[STATE / n] = read_text(STATE / n)
        for d in STATE_DIRS:
            if (STATE / d).is_dir():
                self.add_dir(STATE / d)
        print(f"journal: watching {len(self.wd)} folders and {len(self.cache)} files", flush=True)

    def run(self):
        self.boot()
        while True:
            for ev in self.ino.read():
                if ev.mask & flags.Q_OVERFLOW:
                    self.boot()
                    continue
                d = self.wd.get(ev.wd)
                if d is None or not ev.name:
                    continue
                p = d / ev.name
                if ev.mask & flags.ISDIR:
                    if ev.mask & (flags.CREATE | flags.MOVED_TO) and in_scope(p):
                        self.add_dir(p, scan=True)
                    continue
                if skip_name(ev.name) or not in_scope(p):
                    continue
                if ev.mask & (flags.CLOSE_WRITE | flags.MOVED_TO):
                    self.changed(p)
                elif ev.mask & (flags.DELETE | flags.MOVED_FROM):
                    self.deleted(p)


if __name__ == "__main__":
    if "--recent" in sys.argv:
        rows = []
        for f in sorted(OUT.glob("journal-*.jsonl"))[-1:]:
            rows += [json.loads(l) for l in f.read_text().splitlines()[-8:]]
        for r in rows:
            print(time.strftime("%H:%M:%S", time.localtime(r["t"])), r["who"], r["op"], r["path"], r["summary"])
    elif "--watch" in sys.argv:
        Journal().run()
    else:
        sys.exit(__doc__)
