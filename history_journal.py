"""Turns the change journal into History rows, and adds what changed to the action rows."""
import json
from pathlib import Path

import agent
import history

OUT = agent.STATE / "journal"
GAP = 90
OPW = {"created": "Created", "updated": "Updated", "deleted": "Deleted"}
SETTINGS = ("state/settings.json", "state/curator_cfg.json", "state/tuning.json")


def events(last=4000):
    out = []
    for f in sorted(OUT.glob("journal-*.jsonl"))[-3:]:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines[-last:]:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and "t" in r and "path" in r:
                out.append(r)
    out.sort(key=lambda r: r["t"])
    return out[-last:]


def link_for(path):
    if path.startswith("state/"):
        return None
    if path.startswith(("files/derived/", "raw/files/")):
        return history.file_link(path, "View " + Path(path).name)
    if path.endswith(".md"):
        return history.note_link(path, "Open " + Path(path).stem)
    return None


def short(path):
    return path[:-3] if path.endswith(".md") else path


def block(e, cap):
    head = f"{OPW.get(e['op'], e['op'])} {e['path']}  ({e.get('summary', '')})"
    lines = (e.get("diff") or [])[:cap]
    more = len(e.get("diff") or []) - len(lines)
    return head + ("\n" + "\n".join(lines) if lines else "") + (f"\n... and {more} more lines" if more > 0 else "")


def group_rows(evs):
    groups, cur = [], []
    for e in evs:
        if cur and (e["who"] != cur[-1]["who"] or e["t"] - cur[-1]["t"] > GAP):
            groups.append(cur)
            cur = []
        cur.append(e)
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        paths = list(dict.fromkeys(e["path"] for e in g))
        if len(g) == 1 and g[0]["path"] in SETTINGS and g[0]["op"] == "updated":
            text = "Changed settings: " + g[0].get("summary", "")
        elif len(paths) == 1:
            e = g[-1]
            text = f"{OPW.get(e['op'], e['op'])} {short(e['path'])} ({e.get('summary', '')})"
        else:
            names = ", ".join(short(p) for p in paths[:3])
            text = f"Changed {len(paths)} files: {names}" + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")
        links, seen = [], set()
        for e in g:
            lk = link_for(e["path"])
            if lk and lk["href"] not in seen and len(links) < 8:
                seen.add(lk["href"])
                links.append(lk)
        row = history.ev(g[-1]["t"], g[0]["who"], "vault", text, "", links)
        detail = "\n\n".join(block(e, 14) for e in g[:12]) + (f"\n\n... and {len(g) - 12} more changes" if len(g) > 12 else "")
        row["detail"] = detail[:7000]
        out.append(row)
    return out


def attach(rows, evs):
    if not evs:
        return
    first = evs[0]["t"]
    rules = {("task", None): (45, 3, ("tasks/", "state/todo/", "inbox/")),
             ("file", "reviewer"): (45, 3, ("files/", "state/file_queue/")),
             ("fact", "reviewer"): (5, 240, ("entities/", "timelines/", "summaries/")),
             ("fact", "sweeper"): (150, 3, ("entities/", "timelines/", "summaries/"))}
    for r in rows:
        rule = rules.get((r["kind"], r["who"])) or rules.get((r["kind"], None))
        if not rule or r["t"] < first:
            continue
        lo, hi, prefixes = rule
        picks = [e for e in evs if r["t"] - lo <= e["t"] <= r["t"] + hi and e["path"].startswith(prefixes)]
        if picks:
            note = " (written around the same time, so it may include a neighbouring job)" if r["kind"] == "fact" else ""
            r["detail"] = (r.get("detail") or "") + f"\n\nWhat it changed{note}:\n" + "\n\n".join(block(e, 8) for e in picks[:8])
            lk = [x for x in (link_for(e["path"]) for e in picks[:8]) if x]
            r["open"] = (r.get("open") or []) + lk
        else:
            r["detail"] = (r.get("detail") or "") + "\n\nNothing in the vault changed for this."


def merge(rows):
    evs = events()
    if not evs:
        return rows
    attach(rows, evs)
    return rows + group_rows(evs)
