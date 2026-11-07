"""Turns the change journal into History rows, and adds what changed to the action rows."""
import bisect
import json
from pathlib import Path

import agent
import history

OUT = agent.STATE / "journal"
GAP = 90
OPW = {"created": "Created", "updated": "Updated", "deleted": "Deleted"}
SETTINGS = ("state/settings.json", "state/curator_cfg.json", "state/tuning.json")


_CACHE = {}   # journal file name -> {"size", "pos", "ents"}: each file is read once, a growing one only from where it stopped
_ALL = {"sig": None, "evs": [], "ts": [], "rows": []}


def _read_file(f):
    """The entries of one journal file, without their diff lines (those are read again only for the rows being shown):
    {"t", "path", "op", "who", "summary", "f": file name, "o": where the entry starts, "n": its length}."""
    size = f.stat().st_size
    c = _CACHE.get(f.name)
    if c and c["size"] == size:
        return c["ents"]
    ents, pos = (c["ents"], c["pos"]) if c and size > c["size"] else ([], 0)
    with open(f, "rb") as fh:
        fh.seek(pos)
        data = fh.read()
    for raw in data.splitlines(True):
        if not raw.endswith(b"\n"):
            break   # the entry being written right now: read on the next call
        try:
            r = json.loads(raw)
        except ValueError:
            r = None
        if isinstance(r, dict) and "t" in r and "path" in r:
            ents.append({"t": r["t"], "path": r["path"], "op": r.get("op"), "who": r.get("who") or "system",
                         "summary": r.get("summary", ""), "f": f.name, "o": pos, "n": len(raw)})
        pos += len(raw)
    _CACHE[f.name] = {"size": size, "pos": pos, "ents": ents}
    return ents


def _index():
    """Every entry still on record, oldest first, and the grouped History rows made from them. Rebuilt only when a
    journal file was added, removed or grew."""
    files = sorted(OUT.glob("journal-*.jsonl"))
    sig = tuple((f.name, f.stat().st_size) for f in files)
    if sig != _ALL["sig"]:
        for gone in set(_CACHE) - {f.name for f in files}:
            del _CACHE[gone]
        evs = [e for f in files for e in _read_file(f)]
        evs.sort(key=lambda e: e["t"])
        _ALL.update(sig=sig, evs=evs, ts=[e["t"] for e in evs], rows=group_rows(evs))
    return _ALL["evs"], _ALL["rows"]


def events():
    return _index()[0]


def diff_of(e):
    """The changed lines of one entry, read from its file now."""
    try:
        with open(OUT / e["f"], "rb") as fh:
            fh.seek(e["o"])
            return json.loads(fh.read(e["n"])).get("diff") or []
    except (OSError, ValueError):
        return ["(this entry was deleted to keep the history within its size setting)"]


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


def block(e):
    """One change with every line that changed."""
    head = f"{OPW.get(e['op'], e['op'])} {e['path']}  ({e.get('summary', '')})"
    lines = diff_of(e)
    return head + ("\n" + "\n".join(lines) if lines else "")


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
        row["_ev"] = g   # its lines are read when the row is shown (fill)
        out.append(row)
    return out


def attach(rows, evs):
    """Tie each action row to the vault changes made around it. The changes are looked up by time, so this stays fast
    with every entry on record; their lines are read when the row is shown (fill)."""
    if not evs:
        return
    ts = _ALL["ts"]
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
        a, b = bisect.bisect_left(ts, r["t"] - lo), bisect.bisect_right(ts, r["t"] + hi)
        picks = [e for e in evs[a:b] if e["path"].startswith(prefixes)]
        if picks:
            r["_pick"] = picks
            r["_note"] = " (written around the same time, so it may include a neighboring job)" if r["kind"] == "fact" else ""
            r["open"] = (r.get("open") or []) + [x for x in (link_for(e["path"]) for e in picks[:8]) if x]
        else:
            r["detail"] = (r.get("detail") or "") + "\n\nNothing in the vault changed for this."


def fill(rows):
    """For the rows about to be shown: read their changed lines and put them in the detail. Returns copies."""
    out = []
    for r in rows:
        if "_ev" in r or "_pick" in r or "_note" in r:
            r = dict(r)
            if "_ev" in r:
                r["detail"] = "\n\n".join(block(e) for e in r.pop("_ev"))
            if "_pick" in r:
                r["detail"] = (r.get("detail") or "") + f"\n\nWhat it changed{r.pop('_note', '')}:\n" + "\n\n".join(block(e) for e in r.pop("_pick"))
            r.pop("_note", None)
        out.append(r)
    return out


def merge(rows):
    evs, grouped = _index()
    if not evs:
        return rows
    attach(rows, evs)
    return rows + grouped
