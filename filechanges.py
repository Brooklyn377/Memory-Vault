"""The reviewer reads later chats about a file and records what changed.
The original is never touched. Changes go to files/derived/<name>/<name>, with a log in changes.json.
  filechanges.py NAME [NAME...]    check those files
  filechanges.py --all             check every saved file
  filechanges.py --reset NAME      throw away the derived copy and its log"""
import json
import shutil
import sys
from datetime import datetime

import agent
import filestore
import reviewer

VAULT, ORIG, DER = agent.VAULT, filestore.ORIG, filestore.DER
WIN, MAX_EXCERPT = __import__("tuning").get("files.window"), __import__("tuning").get("files.excerpt_chars")
SYS = ("You track edits to one saved file. You get the CURRENT FILE and EXCERPTS from later chats that mention it. "
       "List only edits that the excerpts clearly make to THIS file: patch scripts with exact old and new text, sed "
       "commands, or a replacement block. Ignore other files and ideas that were not used. "
       "Set confirmed true only if the excerpts show it worked, for example output saying written or matches: 1. "
       'Reply as JSON only: {"changes":[{"what":"<one short sentence>","old":"<exact text now in the file>",'
       '"new":"<exact replacement text>","quote":"<exact words from the excerpts that show it>","confirmed":true}]}. '
       'If none: {"changes":[]}')


def excerpts(name, since, checked):
    out, stems, size, low = [], [], 0, name.lower()
    for p in sorted((VAULT / "raw" / "conversations").glob("*.md")):
        if p.stem in checked or p.name[:10] < since:
            continue
        t = p.read_text(encoding="utf-8", errors="replace")
        tl, spans, i = t.lower(), [], 0
        while len(spans) < 6:
            i = tl.find(low, i)
            if i < 0:
                break
            spans.append((max(0, i - WIN), min(len(t), i + WIN)))
            i += WIN
        if not spans:
            continue
        merged = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], b)
            else:
                merged.append((a, b))
        block = f"=== chat {p.stem} ===\n" + "\n...\n".join(t[a:b] for a, b in merged)
        if out and size + len(block) > MAX_EXCERPT:
            break
        out.append(block)
        stems.append(p.stem)
        size += len(block)
    return out, stems


QUEUE = agent.STATE / "file_queue"


def park(name, stems, why):
    """Set a file aside and ask you what to do. Nothing retries until you answer."""
    import questions
    QUEUE.mkdir(parents=True, exist_ok=True)
    n = len(list(QUEUE.glob("*.json")))
    (QUEUE / f"{name}.json").write_text(json.dumps({"why": why, "stems": stems, "at": datetime.now().isoformat(timespec="seconds")}))
    questions.ask(f'Could not read changes to "{name}"', why=f"Reason: {why}.", kind="filecheck",
                  payload={"name": name, "at": datetime.now().isoformat(timespec="seconds")},
                  options=[{"label": "Try again", "value": "retry"}, {"label": "Skip these chats", "value": "skip"}])
    return f"parked ({why})"


def decide(name, text):
    """Your answer to a parked file. Removing the marker is what wakes the watcher."""
    p = QUEUE / f"{name}.json"
    if not p.exists():
        return
    if text == "skip":
        vers = sorted((ORIG / name).glob("*"))
        cj = DER / name / "changes.json"
        st = json.loads(cj.read_text()) if cj.exists() else {"base": vers[-1].name, "applied": [], "unapplied": [], "checked": []}
        stems = json.loads(p.read_text()).get("stems", [])
        st["checked"] += stems
        st.setdefault("skipped", []).append({"at": datetime.now().isoformat(timespec="seconds"),
            "what": f"Skipped by you: {len(stems)} chat(s) not read for changes", "stems": stems})
        cj.parent.mkdir(parents=True, exist_ok=True)
        cj.write_text(json.dumps(st, indent=1, ensure_ascii=False))
    p.unlink()
    filestore.write_note(name)


def process(name):
    vers = sorted((ORIG / name).glob("*")) if (ORIG / name).is_dir() else []
    if not vers:
        return "no saved original"
    if (QUEUE / f"{name}.json").exists():
        return "parked, waiting for your answer"
    base = vers[-1]
    cdir = DER / name
    cj = cdir / "changes.json"
    st = json.loads(cj.read_text()) if cj.exists() else {}
    if st.get("base") != base.name:
        st = {"base": base.name, "applied": [], "unapplied": [], "checked": []}
        (cdir / name).unlink(missing_ok=True)
    path = cdir / name
    cur = path.read_text(encoding="utf-8") if path.exists() else base.read_text(encoding="utf-8", errors="replace")
    ex, stems = excerpts(name, f"{base.name[:4]}-{base.name[4:6]}-{base.name[6:8]}", set(st["checked"]))
    if not stems:
        return "nothing new to read"
    blob = "\n".join(ex)
    user = f"CURRENT FILE ({name}):\n{cur[:9000]}\n\nEXCERPTS:\n{blob}"
    for attempt in range(2):
        v = reviewer.rask(SYS, user if attempt == 0 else user + "\n\nYour last answer was not valid JSON in the required shape. Reply with the JSON only.", tokens=8000)
        if isinstance(v, dict) and isinstance(v.get("changes"), list):
            break
    else:
        return park(name, stems, "the reviewer gave no usable answer twice")
    now = datetime.now().isoformat(timespec="seconds")
    ok = bad = 0
    for c in v["changes"]:
        if not isinstance(c, dict):
            continue
        old, new, quote = str(c.get("old", "")), str(c.get("new", "")), str(c.get("quote", ""))
        rec = {"at": now, "what": str(c.get("what", ""))[:200], "quote": quote[:300]}
        if not old or old == new or not reviewer.grounded(quote, blob):
            rec["why"] = "no exact old text, or the quote is not in the chat"
        elif cur.count(old) != 1:
            rec["why"] = f"old text found {cur.count(old)} times in the file, not once"
        else:
            cur = cur.replace(old, new)
            sure = (old in blob and new in blob) + bool(c.get("confirmed"))
            rec["confidence"] = ("low", "medium", "high")[sure]
            st["applied"].append(rec)
            ok += 1
            continue
        st["unapplied"].append(rec)
        bad += 1
    st["checked"] += stems
    cdir.mkdir(parents=True, exist_ok=True)
    if ok:
        path.write_text(cur, encoding="utf-8")
    cj.write_text(json.dumps(st, indent=1, ensure_ascii=False))
    filestore.write_note(name)
    return f"read {len(stems)} chat(s): {ok} applied, {bad} not applied"


def main(args):
    if args[:1] == ["--reset"]:
        shutil.rmtree(DER / args[1], ignore_errors=True)
        filestore.write_note(args[1])
        return print("reset", args[1])
    if not reviewer.ready():
        sys.exit("reviewer is down")
    names = sorted(p.name for p in ORIG.iterdir()) if args[:1] == ["--all"] else args
    for n in names:
        print(n + ":", process(n))


def _idle():
    import urllib.request
    import swap
    if swap.holding():
        swap.want()
        return False
    try:
        with urllib.request.urlopen("http://127.0.0.1:8081/slots", timeout=3) as r:
            return not any(x.get("is_processing") for x in json.load(r))
    except Exception:
        return False


def _sig():
    chats = list((VAULT / "raw" / "conversations").glob("*.md"))
    orig = list(ORIG.glob("*/*"))
    newest = max([p.stat().st_mtime for p in chats + orig] or [0])
    try:
        import applier
        newest = f"{newest}:{applier._pid('gpt')}"
    except Exception:
        pass
    return f"{len(chats)}:{len(orig)}:{newest}:{len(list(QUEUE.glob('*.json'))) if QUEUE.is_dir() else 0}"


def watch():
    import time
    agent.log("file changes watcher started")
    seen = None
    while True:
        sig = _sig()
        if sig == seen or not ORIG.is_dir() or not reviewer.ready():
            time.sleep(60)
            continue
        for n in sorted(p.name for p in ORIG.iterdir()):
            while not _idle():
                time.sleep(30)
            try:
                r = process(n)
            except Exception as e:
                r = f"error {type(e).__name__}"
            if r not in ("nothing new to read", "no saved original"):
                agent.log(f"file changes: {n}: {r}")
        seen = sig


if __name__ == "__main__":
    if "--watch" in sys.argv:
        watch()
    else:
        main(sys.argv[1:])
