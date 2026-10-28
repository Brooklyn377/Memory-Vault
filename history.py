"""History: one list of every action the models and you have taken, built from the logs that already exist.
  history.py        print what it finds (no web page)"""
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import agent

from bootstrap import slug as _slug

STATE, VAULT = agent.STATE, agent.VAULT
WHO = ("sweeper", "reviewer", "curator", "assistant", "you", "system")
KINDS = ("vault", "fact", "task", "link", "review", "prune", "file", "ask", "chat", "publish", "setting",
         "import", "problem", "system", "call")
CALL_WHO = {"qwen": "sweeper", "gpt": "reviewer", "cur": "curator", "pa": "assistant"}
ASK_WHO = {"hygiene": "sweeper", "free": "sweeper", "merge": "system", "filecheck": "reviewer",
           "inboxcheck": "system", "curate": "curator", "curatecheck": "curator"}
LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*)$")
ERR = re.compile(r"\b(error|failed|unusable|could not|parked)\b", re.I)


def ts(s):
    """Times in the logs are UTC (the container runs on UTC)."""
    try:
        d = datetime.fromisoformat(str(s).replace("Z", ""))
    except ValueError:
        return None
    return (d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d).timestamp()


def ev(t, who, kind, text, detail="", links=None):
    return {"t": t, "who": who, "kind": kind, "text": str(text)[:240], "detail": str(detail)[:1200], "open": links or []}


def note_link(rel, label="Open note"):
    import urllib.parse
    return {"label": label, "href": "/graph#" + urllib.parse.quote(str(rel), safe="/")}


def file_link(rel, label="View file"):
    import urllib.parse
    return {"label": label, "href": "/file?path=" + urllib.parse.quote(str(rel), safe="")}


def file_links(name):
    out = []
    if (VAULT / "files" / "derived" / name / name).exists():
        out.append(file_link(f"files/derived/{name}/{name}", "View changed file and what changed"))
    odir = VAULT / "raw" / "files" / name
    vers = sorted(odir.glob("*")) if odir.is_dir() else []
    if vers:
        out.append(file_link(vers[-1].relative_to(VAULT).as_posix(), "View original"))
    if (VAULT / "files" / f"{name}.md").exists():
        out.append(note_link(f"files/{name}.md", "Open the file note"))
    return out


def _tasks():
    out = []
    for p in (STATE / "todo").glob("*.json"):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            pass
    return out


def task_links(r):
    lk, tid = [], str(r.get("task", ""))
    for p in (VAULT / "tasks").glob(f"*{tid[:6]}.md") if tid else []:
        lk.append(note_link(p.relative_to(VAULT).as_posix(), "Open the task note"))
    if r.get("source"):
        lk.append(note_link(str(r["source"]), "Open the source"))
    return lk


def task_event(t, text, raw):
    s = re.match(r"scanned (\S+): (\d+) new task", raw.strip())
    if s:
        made = [x["title"] for x in _tasks() if x.get("source") == s.group(1)]
        detail = ("Tasks from this note:\n- " + "\n- ".join(made[:8])) if made else ""
        return ev(t, "reviewer", "task", text, detail, [note_link(s.group(1), "Open the summary")])
    m = re.match(r"task '(.*)' -> (\w+)$", raw.strip())
    if not m:
        return ev(t, "reviewer", "task", text)
    title, status = m.group(1), m.group(2)
    word = {"done": "Closed", "dropped": "Dropped", "open": "Kept open"}.get(status, "Updated")
    task = next((x for x in _tasks() if x["title"][:40] == title), None)
    if not task:
        return ev(t, "reviewer", "task", f'{word} task "{title.strip()}"')
    ups = task.get("updates") or []
    last = ups[-1] if ups else {}
    lines = []
    if last.get("text"):
        lines.append("Your update: " + last["text"])
    if task.get("state_line"):
        lines.append("Reviewer's summary: " + task["state_line"])
    lines.append("Status: " + str(task.get("status")))
    if task.get("quote"):
        lines.append('Quote: "' + task["quote"] + '"')
    lk = []
    for p in (VAULT / "tasks").glob(f"*{task['id'][:6]}.md"):
        lk.append(note_link(p.relative_to(VAULT).as_posix(), "Open the task note"))
    if task.get("source"):
        lk.append(note_link(task["source"], "Open the source note"))
    return ev(t, "reviewer", "task", f'{word} task "{task["title"]}"', "\n".join(lines), lk)


def clean(s):
    s = str(s).strip()
    return s[:1].upper() + s[1:]


def chat_name(rel):
    s = re.sub(r"^\d{4}-\d\d-\d\d-", "", Path(str(rel)).stem)
    return re.sub(r"-[0-9a-f]{8}$", "", s).replace("-", " ")


def short(rel):
    return Path(str(rel)).stem.replace("-", " ")


def rows(path, last=4000):
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-last:]:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def who_of(body):
    for pre, w in (("reviewer", "reviewer"), ("review gate", "reviewer"), ("task check", "reviewer"),
                   ("file changes", "reviewer"), ("chat:", "reviewer"), ("sweep", "sweeper"),
                   ("curator", "curator"), ("settings:", "you")):
        if body.startswith(pre):
            return w
    return "system"


def classify(t, msg):
    if msg == "login ok":
        return None
    if "failed login" in msg:
        return ev(t, "system", "problem", "Failed login attempt")
    body = msg[7:] if msg.startswith("agent: ") else msg
    if body.startswith(("review gate:", "task check:")):
        return None  # shown from their own logs, which have more detail
    who = who_of(body)
    if body.endswith(" started"):
        return ev(t, "system", "system", clean(body))
    if ERR.search(body):
        return ev(t, who, "problem", clean(body))
    if body.startswith("reviewer: "):
        return task_event(t, clean(body[10:]), body[10:])
    if body.startswith("sweep"):
        return ev(t, "sweeper", "fact", clean(body))
    if body.startswith("curator:"):
        return ev(t, "curator", "review", clean(body[8:]))
    if body.startswith("file changes:"):
        rest = body[13:].strip()
        fname = rest.split(":", 1)[0].strip()
        lk = [note_link(f"files/{fname}.md", "Open the file note")] if (VAULT / "files" / f"{fname}.md").exists() else []
        return ev(t, "reviewer", "file", "Checked " + rest, links=lk)
    if body.startswith("chat:"):
        return ev(t, "reviewer", "chat", clean(body[5:]))
    if body.startswith("settings:"):
        return ev(t, "you", "setting", clean(body[9:]))
    if body.startswith("publisher"):
        return ev(t, "system", "publish", clean(body))
    if body.startswith(("imported", "purged", "importer", "processed")):
        return ev(t, "system", "import", clean(body))
    return ev(t, who, "system", clean(body))


def from_log():
    try:
        lines = (STATE / "activity.log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-6000:]:
        m = LINE.match(line)
        if m and ts(m.group(1)) is not None:
            e = classify(ts(m.group(1)), m.group(2).strip())
            if e:
                out.append(e)
    return out


def from_review():
    out = []
    for r in rows(STATE / "review_log.jsonl"):
        name, n, v = chat_name(r.get("rel")), int(r.get("chunk", 0) or 0) + 1, r.get("verdict")
        kept = r.get("kept", 0)
        off = r.get("offered", kept)
        if v == "dropped":
            text = f'Dropped everything from part {n} of "{name}"'
        elif v == "trimmed":
            text = f'Kept {kept} of {off} facts from part {n} of "{name}"'
        else:
            text = f'Accepted part {n} of "{name}" ({kept} facts)'
        retry = f"\nThe Sweeper was asked to retry {r.get('tries')} time(s)." if r.get("tries") else ""
        out.append(ev(ts(r.get("at")), "reviewer", "fact", text, f"{r.get('reason', '')}{retry}",
                      [note_link(str(r["rel"]), "Open the chat")] if r.get("rel") else []))
    return out


def from_tasks():
    out = []
    for r in rows(STATE / "task_changes.jsonl"):
        verb = "Closed" if r.get("action") == "close" else "Updated"
        out.append(ev(ts(r.get("at")), "reviewer", "task", f'{verb} task "{str(r.get("title", ""))[:80]}"',
                      f'{r.get("note", "")}\nQuote: "{r.get("quote", "")}"\nFrom: {r.get("source", "")}', task_links(r)))
    return out


def from_curator():
    out = []
    for r in rows(STATE / "curator_log.jsonl"):
        t, a = ts(r.get("at")), r.get("act")
        q = f'\nQuotes: "{r.get("quote_a", "")}" / "{r.get("quote_b", "")}"'
        if a == "link":
            out.append(ev(t, "curator", "link", f'Linked {short(r.get("from"))} to {short(r.get("to"))}', f'{r.get("why", "")}{q}', [note_link(str(r.get("from")), "Open the first note"), note_link(str(r.get("to")), "Open the second note")]))
        elif a == "prune":
            out.append(ev(t, "curator", "prune", f'Pruned "{short(r.get("note"))}"', r.get("reason", "")))
        elif a == "map_link":
            out.append(ev(t, "curator", "link", f'Map pass linked {short(r.get("a"))} and {short(r.get("b"))}',
                          f'{r.get("why", "")}{q}\nUndo: mapper.py --revert {r.get("id")}', [note_link(str(r.get("a")), "Open the first note"), note_link(str(r.get("b")), "Open the second note")]))
        elif a == "map_hub":
            mem = r.get("members") or []
            out.append(ev(t, "curator", "file", f'Map pass made a hub note: {r.get("title")}',
                          f'{len(mem)} notes: ' + ", ".join(short(x) for x in mem[:12]) + f'\nUndo: mapper.py --revert {r.get("id")}',
                          [note_link("curator/hub-" + _slug(str(r.get("title", "")))[:60].lower() + ".md", "Open the hub note")]))
        elif a == "map_pass":
            out.append(ev(t, "curator", "review", f'Finished map pass {r.get("pass")}: {r.get("added")} added'))
        elif a == "map_part_failed":
            out.append(ev(t, "curator", "problem", f'Map pass {r.get("pass")}: part {r.get("part")} gave no usable answer, skipped'))
    return out


def from_questions():
    out, d = [], STATE / "questions"
    for p in d.glob("*.json") if d.is_dir() else []:
        try:
            q = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        payload = q.get("payload") or {}
        if q.get("kind") == "curate" and payload.get("act") == "prune":
            continue  # already shown from the curator log
        text = str(q.get("question", ""))
        out.append(ev(ts(q.get("created")), ASK_WHO.get(q.get("kind"), "system"), "ask", f"Asked you: {text}", q.get("why", "")))
        if q.get("answered") and q.get("status") in ("answered", "dismissed"):
            ans = "Skipped" if q["status"] == "dismissed" else f'Answered "{str(q.get("answer"))[:80]}"'
            out.append(ev(ts(q["answered"]), "you", "ask", f"{ans}: {text}"))
    return out


def from_files():
    out = []
    for cj in (VAULT / "files" / "derived").glob("*/changes.json"):
        try:
            st = json.loads(cj.read_text())
        except (OSError, ValueError):
            continue
        name = cj.parent.name
        for c in st.get("applied", []):
            out.append(ev(ts(c.get("at")), "reviewer", "file", f'Changed {name}: {str(c.get("what", ""))[:120]}',
                          f'Confidence: {c.get("confidence")}\nQuote: "{c.get("quote", "")}"', file_links(name)))
        for c in st.get("skipped", []):
            out.append(ev(ts(c.get("at")), "you", "file", f"Skipped changes to {name}: {c.get('what', '')}"))
    for p in (VAULT / "raw" / "files").glob("*/*"):
        try:
            out.append(ev(p.stat().st_mtime, "system", "file", f"Saved original of {p.parent.name}", p.name,
                          [file_link(p.relative_to(VAULT).as_posix(), "View original"), note_link(f"files/{p.parent.name}.md", "Open the file note")]))
        except OSError:
            pass
    return out


def from_calls(files=6):
    out, whats = [], {}
    for f in sorted((STATE / "think").glob("think-*.jsonl"))[-files:]:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"k": "start"' in line:
                        try:
                            r0 = json.loads(line)
                            whats[r0["c"]] = r0.get("w", "")
                        except (ValueError, KeyError):
                            pass
                        continue
                    if '"k": "end"' not in line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    label, _, secs = str(r.get("s", "")).partition(" ")
                    out.append(ev(int(r["t"]) / 1e9, CALL_WHO.get(r.get("m"), "system"), "call",
                                  (whats.get(r.get("c")) or f"Model call ({label})") + f": {r.get('n', 0)} tokens, {secs}"))
        except (OSError, KeyError, ValueError):
            pass
    return out


def collect(calls=False):
    out = (from_log() + from_review() + from_tasks() + from_curator() + from_questions() + from_files())
    if calls:
        out += from_calls()
    out = [e for e in out if e["t"] is not None]
    try:
        import history_journal
        out = history_journal.merge(out)
    except Exception:
        pass
    return out


def query(who, kinds, offset, limit):
    evs = [e for e in collect("call" in kinds) if e["who"] in who and e["kind"] in kinds]
    evs.sort(key=lambda e: -e["t"])
    return evs[offset:offset + limit], len(evs) > offset + limit


if __name__ == "__main__":
    evs = sorted(collect(True), key=lambda e: -e["t"])
    for (w, k), n in sorted(Counter((e["who"], e["kind"]) for e in evs).items()):
        print(f"{n:6} {w:9} {k}")
    print("newest:")
    for e in evs[:12]:
        print(datetime.fromtimestamp(e["t"], timezone.utc).strftime("%m-%d %H:%M"), e["who"], e["kind"], e["text"][:80])
    print("startup / unsorted lines:")
    for e in [x for x in evs if x["kind"] == "system"][:8]:
        print("  ", e["text"][:100])
