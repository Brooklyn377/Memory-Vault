"""Map pass: when per-note review has nothing left, the curator reads the whole vault and adds links and hub notes.
  mapper.py --size          how big the prompts would be (no model call)
  mapper.py --list          the last changes, with ids
  mapper.py --revert ID     undo one link or hub and never propose it again"""
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import agent
import curator
import hygiene
import reviewer
import reviewgate

VAULT, STATE = curator.VAULT, curator.STATE
MAP, OUT = STATE / "curator_map.json", VAULT / "curator"
SKIP = ("raw/", "inbox/", "curator/")
KINDS = {"entities": "entity", "summaries": "summary", "timelines": "timeline", "tasks": "task", "files": "file"}
SYS = ("You are the curator of one person's homelab notes vault, doing a slow review of the whole vault while nothing "
       "else needs you. You get ALL NOTES (path and title), the titles of the SOURCE CHATS, and DETAILED NOTES with "
       "a type, link count and a short gist. Smaller models wrote these notes a few messages at a time, so many "
       "related notes are not linked. Find (1) pairs of notes that are clearly about related things but are not "
       "linked, and (2) groups of three or more notes that belong to one topic and deserve a hub note that ties them "
       "together, such as one project, one machine or one problem. Use only notes listed. For every link give the "
       "exact words copied from each of the two notes: quote_a from note a and quote_b from note b, copied from the "
       "gists shown. For every hub give a title, a one-sentence summary and, for each member, its path and exact words "
       "copied from that note's gist. Never invent. If nothing is worth adding, return empty lists. Reply with ONLY "
       'JSON: {"links":[{"a":"<path>","b":"<path>","why":"","quote_a":"","quote_b":""}],'
       '"hubs":[{"title":"","summary":"","members":[{"note":"<path>","quote":""}]}]}')


def load():
    try:
        d = json.loads(MAP.read_text())
    except (OSError, ValueError):
        d = {}
    d.setdefault("links", [])
    d.setdefault("hubs", [])
    d.setdefault("blocked", [])
    d.setdefault("pass", {})
    d["stats"] = {**{"links": 0, "hubs": 0, "passes": 0}, **d.get("stats", {})}
    return d


def save(d):
    curator._atomic(MAP, json.dumps(d))


def pkey(a, b):
    return "|".join(sorted((a, b)))


def ref(r):
    return r[:-3] if r.endswith(".md") else r


# ---------- the index ----------
def notes(v):
    blocked = hygiene.blocked()
    out = []
    for rel in sorted(v.body):
        if rel.startswith(SKIP):
            continue
        top = rel.split("/")[0]
        if "/" not in rel or top not in KINDS:
            continue
        if top == "entities" and hygiene.slug(Path(rel).stem).lower() in blocked:
            continue
        out.append(rel)
    return out


def gist(v, rel, n):
    body = curator.strip(v.body[rel])
    text = "; ".join(f for _, f in curator.mentions(body)) or body if rel.startswith("entities/") else body
    text = re.sub(r"(?m)^#+ .*$", "", text)
    return re.sub(r"\s+", " ", text).strip()[:n]


def vault_sig(ids):
    h = hashlib.sha1()
    for r in ids:
        h.update(r.encode())
        h.update(curator.chash(curator.file_text(r)).encode())
    return h.hexdigest()[:16]


def slices(v, ids, budget):
    titles = "\n".join(f"{r} | {v.title(r)[:60]}" for r in ids)
    chats = "\n".join(f"{r} | {v.title(r)[:60]}" for r in sorted(v.body) if r.startswith("raw/conversations/"))
    room = max(9000, budget - len(titles) - len(chats) - 4500)
    parts, cur, used = [], [], 0
    for r in ids:
        n = len(set(v.out.get(r, ())) | set(v.back.get(r, ())))
        line = f"{r} | {KINDS[r.split('/')[0]]} | {v.title(r)[:60]} | links:{n} | {gist(v, r, __import__('tuning').get('map.gist_chars'))}"
        if cur and used + len(line) > room:
            parts.append(cur)
            cur, used = [], 0
        cur.append(line)
        used += len(line) + 1
    if cur:
        parts.append(cur)
    return titles, chats, parts


# ---------- writing the results ----------
def regen(m):
    OUT.mkdir(parents=True, exist_ok=True)
    keep = set()
    for h in m["hubs"]:
        name = f"hub-{h['slug']}.md"
        keep.add(name)
        rows = "\n".join(f"- [[{ref(x['note'])}]]: {x['quote']}" for x in h["members"] if (VAULT / x["note"]).exists())
        (OUT / name).write_text(
            f"---\ntype: hub\nconfidence: medium\ncreated_by: curator\ntitle: {json.dumps(h['title'])}\n---\n"
            f"# {h['title']}\n\n{h['summary']}\n\n## Notes in this topic\n{rows}\n", encoding="utf-8")
    loose = [l for l in m["links"] if not l.get("home")]
    if loose:
        keep.add("connections.md")
        rows = "\n".join(f"- [[{ref(l['a'])}]] and [[{ref(l['b'])}]]: {l['why']}" for l in loose)
        (OUT / "connections.md").write_text(
            f"---\ntype: hub\nconfidence: medium\ncreated_by: curator\ntitle: Curator connections\n---\n"
            f"# Curator connections\n\nLinks between notes that have no entity note to hold them.\n\n{rows}\n", encoding="utf-8")
    for p in OUT.glob("*.md"):
        if p.name not in keep and "created_by: curator" in p.read_text(encoding="utf-8")[:300]:
            p.unlink()


def home(a, b):
    for r, o in ((a, b), (b, a)):
        if r.startswith(("entities/", "files/")):
            return r, o
    return None, None


def apply(v, ids, out, data, m):
    idset, now, added = set(ids), datetime.now().isoformat(timespec="seconds"), 0
    known = {pkey(l["a"], l["b"]) for l in m["links"]} | set(m["blocked"])
    for r, ls in data["links"].items():
        known |= {pkey(r, x["to"]) for x in ls}
    for l in (out.get("links") or [])[:__import__("tuning").get("map.links_per_call")]:
        if not isinstance(l, dict):
            continue
        a, b = str(l.get("a", "")), str(l.get("b", ""))
        qa, qb = str(l.get("quote_a", "")), str(l.get("quote_b", ""))
        why = re.sub(r"\s+", " ", str(l.get("why", "")).replace("[[", "").replace("]]", ""))[:200]
        if a == b or a not in idset or b not in idset or not why or pkey(a, b) in known:
            continue
        if b in set(v.out.get(a, ())) | set(v.back.get(a, ())):
            continue
        if not reviewer.grounded(qa, curator.strip(v.body[a])) or not reviewer.grounded(qb, curator.strip(v.body[b])):
            continue
        mid = hashlib.sha1(pkey(a, b).encode()).hexdigest()[:8]
        h, o = home(a, b)
        if h:
            data["links"].setdefault(h, []).append({"to": o, "why": why, "quote_a": qa[:200], "quote_b": qb[:200],
                                                    "at": now, "map": mid})
            curator.write_links(h, data["links"][h])
        m["links"].append({"id": mid, "a": a, "b": b, "why": why, "quote_a": qa[:200], "quote_b": qb[:200],
                           "at": now, "home": h})
        known.add(pkey(a, b))
        m["stats"]["links"] += 1
        data["stats"]["links"] += 1
        added += 1
        reviewgate.note_changed([h or "curator/connections.md"], f"map pass linked {a} and {b}: {why}")
        curator.log({"act": "map_link", "id": mid, "a": a, "b": b, "why": why, "quote_a": qa[:200], "quote_b": qb[:200]})
    for h in (out.get("hubs") or [])[:__import__("tuning").get("map.hubs_per_call")]:
        if not isinstance(h, dict):
            continue
        title = re.sub(r"\s+", " ", str(h.get("title", ""))).strip()[:80]
        summ = re.sub(r"\s+", " ", str(h.get("summary", ""))).strip()[:300]
        mem = []
        for x in (h.get("members") or [])[:__import__("tuning").get("map.hub_members")]:
            if not isinstance(x, dict) or str(x.get("note", "")) not in idset:
                continue
            n = str(x["note"])
            if n not in [y["note"] for y in mem] and reviewer.grounded(str(x.get("quote", "")), curator.strip(v.body[n])):
                mem.append({"note": n, "quote": str(x["quote"])[:200]})
        slug = curator.slug(title)[:60].lower()
        if not title or not slug or len(mem) < __import__("tuning").get("map.hub_min_members") or "hub:" + slug in m["blocked"]:
            continue
        names = {x["note"] for x in mem}
        if any(e["slug"] == slug or len(names & {y["note"] for y in e["members"]}) >= 0.7 * min(len(names), len(e["members"]))
               for e in m["hubs"]):
            continue
        hid = hashlib.sha1(slug.encode()).hexdigest()[:8]
        m["hubs"].append({"id": hid, "title": title, "slug": slug, "summary": summ, "members": mem, "at": now})
        m["stats"]["hubs"] += 1
        data["stats"]["hubs"] = data["stats"].get("hubs", 0) + 1
        added += 1
        reviewgate.note_changed(["curator/hub-" + slug + ".md"], f"map pass made a hub note: {title}")
        curator.log({"act": "map_hub", "id": hid, "title": title, "members": [x["note"] for x in mem]})
    return added


# ---------- the pass ----------
def wants(cfg, v):
    if not cfg.get("map"):
        return False
    st = load()["pass"]
    return not (st.get("sig") == vault_sig(notes(v)) and st.get("done") and not st.get("added"))


def finish(m, st):
    st["done"] = True
    m["stats"]["passes"] += 1
    save(m)
    curator.log({"act": "map_pass", "pass": st["n"], "added": st["added"], "failed_parts": st.get("failed", 0)})
    if st["added"]:
        curator.status("working", f"map pass {st['n']} added {st['added']}; starting another")
    else:
        curator.status("waiting", "map pass found nothing more to add; waiting for the notes to change")
    return bool(st["added"])


def step(cfg, data, v):
    m, ids = load(), notes(v)
    sig, st = vault_sig(ids), m["pass"]
    titles, chats, parts = slices(v, ids, int(curator.prompt_tokens(cfg) * curator.CHARS_PER_TOKEN))
    if st.get("sig") != sig or st.get("done"):
        st = m["pass"] = {"sig": sig, "slice": 0, "added": 0, "done": False, "failed": 0, "n": st.get("n", 0) + 1}
    i = st["slice"]
    if i >= len(parts):
        return finish(m, st)
    curator.status("working", f"map pass {st['n']}: part {i + 1} of {len(parts)}, reading the whole vault")
    user = (f"ALL NOTES (path | title):\n{titles}\n\nSOURCE CHATS (titles only, not linkable):\n{chats}\n\n"
            f"DETAILED NOTES, part {i + 1} of {len(parts)} (path | type | title | links | gist):\n" + "\n".join(parts[i]))
    out = None
    for attempt in range(2):
        u = user if attempt == 0 else user + "\n\nYour last answer was not valid JSON in the required shape. Reply with the JSON only."
        try:
            out = curator.ask_model(SYS, u)
        except Exception as e:
            agent.log(f"curator: map call failed ({type(e).__name__})")
        if isinstance(out, dict):
            break
    if isinstance(out, dict):
        st["added"] += apply(v, ids, out, data, m)
        regen(m)
        curator.save_data(data)
    else:
        st["failed"] += 1
        curator.log({"act": "map_part_failed", "pass": st["n"], "part": i + 1})
        agent.log(f"curator: map pass part {i + 1} gave no usable answer; skipped")
    st["slice"] = i + 1
    save(m)
    return True


# ---------- command line ----------
def revert(mid):
    m, data = load(), curator.load_data()
    for l in [x for x in m["links"] if x["id"] == mid]:
        m["links"].remove(l)
        m["blocked"].append(pkey(l["a"], l["b"]))
        r = l.get("home")
        if r:
            data["links"][r] = [x for x in data["links"].get(r, []) if x.get("map") != mid]
            curator.write_links(r, data["links"][r])
            if not data["links"][r]:
                data["links"].pop(r)
        m["stats"]["links"] -= 1
    for h in [x for x in m["hubs"] if x["id"] == mid]:
        m["hubs"].remove(h)
        m["blocked"].append("hub:" + h["slug"])
        m["stats"]["hubs"] -= 1
    curator.save_data(data)
    save(m)
    regen(m)
    print("reverted", mid)


if __name__ == "__main__":
    a = sys.argv
    if "--revert" in a:
        revert(a[a.index("--revert") + 1])
    elif "--list" in a:
        m = load()
        print(m["stats"], "pass:", {k: m["pass"].get(k) for k in ("n", "slice", "added", "done")})
        for l in m["links"][-15:]:
            print(l["id"], "link", l["a"], "<->", l["b"], "-", l["why"][:80])
        for h in m["hubs"][-10:]:
            print(h["id"], "hub", h["title"], len(h["members"]), "notes")
    elif "--size" in a:
        v = curator.Vault(VAULT)
        ids = notes(v)
        t, c, p = slices(v, ids, int(curator.load_cfg()["prompt_tokens"]) * 3)
        print(len(ids), "notes,", len(p), "calls per pass; characters:", [len(t) + len(c) + sum(len(x) for x in s) for s in p])
    else:
        sys.exit(__doc__)
