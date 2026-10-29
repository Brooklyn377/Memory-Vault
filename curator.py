"""Curator: its own model (port 8082) reads entity notes in depth while the Sweeper and Reviewer are idle.
It takes their memory (swap.py), works through the notes the Sweeper was least sure about, verifies facts against
the source chats, finds missed connections, asks about doubts and prunes notes that hold nothing useful.
A note is never aborted. When another job needs the models it finishes its note and swaps them back.
  curator.py --watch | --release | --once | --show REL | --rank"""
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

import agent
import applier
import gguf_info
import hygiene
import questions
import reviewer
import reviewgate
import settings
import swap
import thinklog
from bootstrap import slug
from graph import Vault, as_list

VAULT, STATE = agent.VAULT, agent.STATE
CFG_FILE, DATA = STATE / "curator_cfg.json", STATE / "curator.json"
STATUS, LOG, QUEUE = STATE / "curator_status.json", STATE / "curator_log.jsonl", STATE / "curator_queue"
ENDPOINT, UNIT = "http://127.0.0.1:8082/v1", applier.UNIT["cur"]
IDLE_FOR, SAMPLE, POLL = 60, 5, 20
START, END = "<!-- curator:start -->", "<!-- curator:end -->"
CHARS_PER_TOKEN = 2.2  # notes full of IPs and paths use many short tokens; measure with the tokenize check
BLOCK = re.compile(re.escape(START) + r".*?" + re.escape(END) + r"\n?", re.S)
MENTION = re.compile(r"^- \[\[([^\]|]+)(?:\|[^\]]*)?\]\]:\s*(.*)$", re.M)
DEFAULT = {"enabled": False, "verify": True, "connect": True, "prune": True, "cycle": True, "map": True, "prompt_tokens": 40000}
STOP = set("this that with from have what when which about there their them they then than been were was are "
           "and for the not can could would should into your you our auto generated document low confidence "
           "until reviewed mentioned".split())


class Full(Exception):
    pass


def _atomic(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def load_cfg():
    try:
        c = json.loads(CFG_FILE.read_text())
    except (OSError, ValueError):
        c = {}
    return {**DEFAULT, **{k: c[k] for k in DEFAULT if k in c}}


def save_cfg(c):
    _atomic(CFG_FILE, json.dumps(c, indent=1))


def load_data():
    try:
        d = json.loads(DATA.read_text())
    except (OSError, ValueError):
        d = {}
    d.setdefault("notes", {})
    d.setdefault("links", {})
    d["pairs"] = set(d.get("pairs", []))
    d["stats"] = {**{"verified": 0, "links": 0, "asked": 0, "pruned": 0}, **d.get("stats", {})}
    return d


def save_data(d):
    _atomic(DATA, json.dumps({**d, "pairs": sorted(d["pairs"])}))


def load_status():
    try:
        return json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return {}


def status(state, why="", note=""):
    _atomic(STATUS, json.dumps({"state": state, "why": why, "note": note,
                                "at": datetime.now().isoformat(timespec="seconds")}))


def log(row):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **row}, ensure_ascii=False) + "\n")


# ---------- swapping the models ----------
def unit_active():
    return subprocess.run(["systemctl", "is-active", "--quiet", UNIT], timeout=10).returncode == 0


def give_up(why):
    c = load_cfg()
    c["enabled"] = False
    save_cfg(c)
    status("off", why + " The switch was turned off.")
    agent.log("curator: " + why)
    return False


def acquire():
    s = settings.get("cur")
    need = gguf_info._fp_safe(s["model_file"], s["context"], s["parallel"])
    limit = gguf_info.total_memory() - gguf_info.HEADROOM
    over = need > limit   # a recommendation only: it loads anyway, with a warning
    if over:
        agent.log(f"curator: loading although about {need / gguf_info.GIB:.0f} GB is estimated, over the "
                  f"{limit / gguf_info.GIB:.0f} GB safe limit")
    if not swap.take():
        return False
    applier.write_env("cur", s)
    subprocess.run(["systemctl", "start", "--no-block", UNIT], timeout=30)
    status("loading", "the Sweeper and Reviewer are unloaded; loading the curator model"
           + (f" (warning: about {need / gguf_info.GIB:.0f} GB estimated, over the {limit / gguf_info.GIB:.0f} GB safe limit)" if over else ""))
    end = time.time() + 900
    while time.time() < end:
        if applier.health("cur") == "ready":
            return True
        time.sleep(5)
    release()
    return give_up("The curator model did not load in 15 minutes (journalctl -u memory-curator-llm).")


def release():
    subprocess.run(["systemctl", "stop", UNIT], timeout=300)
    if swap.holding():
        status("loading", "unloading the curator; loading the Sweeper and Reviewer")
        swap.restore()


# ---------- notes and how unsure the Sweeper was ----------
def file_text(rel):
    return (VAULT / rel).read_text(encoding="utf-8", errors="replace")


def strip(text):
    return BLOCK.sub("", text)


def chash(text):
    return hashlib.sha1(strip(text).encode()).hexdigest()[:16]


def mentions(text):
    return MENTION.findall(strip(text))


def toks(text):
    return {w for w in re.findall(r"[a-z0-9][a-z0-9.\-]{3,}", text.lower()) if w not in STOP}


def pk(a, b):
    return hashlib.sha1("|".join(sorted((a, b))).encode()).hexdigest()[:10]


def src_of(v, target):
    s = v.resolve(target)
    if s not in v.meta:
        s = target if target in v.meta else target + ".md"
    src = (v.meta.get(s) or {}).get("source")
    return src if src and src in v.body else None


def sources_of(v, rel):
    out = []
    for target, _ in mentions(v.body[rel]):
        s = src_of(v, target)
        if s and s not in out:
            out.append(s)
    return out


def merges_map():
    try:
        d = yaml.safe_load((STATE / "merges.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        d = {}
    return {slug(str(k)).lower(): slug(str(x)).lower() for k, x in d.items()}


def penalties():
    """Source chat -> how much the review gate had to trim or drop."""
    pen = {}
    try:
        rows = (STATE / "review_log.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return pen
    for line in rows:
        try:
            r = json.loads(line)
            p = 3 if r.get("verdict") == "dropped" else 0
            if r.get("verdict") == "trimmed":
                p = 1 + max(0, int(r.get("offered", 0)) - int(r.get("kept", 0)))
            p += 1 if int(r.get("tries", 0) or 0) > 0 else 0
        except (ValueError, TypeError):
            continue
        if p:
            pen[r.get("rel")] = pen.get(r.get("rel"), 0) + p
    return pen


def bad_facts():
    """Entity key -> number of facts that failed the Sweeper's own quote check."""
    out, mm = {}, merges_map()
    try:
        st = json.loads((STATE / "sweep.json").read_text())
    except (OSError, ValueError):
        return out
    for d in (st.get("docs") or {}).values():
        for f in d.get("facts") or []:
            if isinstance(f, dict) and f.get("ok") is False:
                k = slug(str(f.get("entity", ""))).lower()
                k = mm.get(k, k)
                out[k] = out.get(k, 0) + 1
    return out


def uncertainty(v, rel, pen, bad):
    parts = []
    if str((v.meta.get(rel) or {}).get("confidence")) == "low":
        parts.append(("auto-written, low confidence", 2))
    if len(mentions(v.body[rel])) <= 1:
        parts.append(("only one mention", 3))
    p = min(10, sum(pen.get(s, 0) for s in sources_of(v, rel)))
    if p:
        parts.append(("the review gate trimmed or dropped parts of its source chats", p))
    b = bad.get(slug(Path(rel).stem).lower(), 0)
    if b:
        parts.append((f"{b} fact(s) failed the quote check", 2 * b))
    return sum(w for _, w in parts), "; ".join(t for t, _ in parts)


def find_cands(v, data, rel, ents, S, T, n=None):
    n = __import__("tuning").get("curator.candidates") if n is None else n
    linked = set(v.out[rel]) | set(v.back[rel]) | {l["to"] for l in data["links"].get(rel, [])}
    scored = []
    for o in ents:
        if o == rel or o in linked or (S[o] & S[rel]) or pk(rel, o) in data["pairs"]:
            continue
        scored.append((-len(T[rel] & T[o]), hashlib.sha1((rel + o).encode()).hexdigest(), o))
    scored.sort()
    return [o for _, _, o in scored[:n]]


def next_note(v, data, cfg, ents, S, T):
    parked = {p.stem for p in QUEUE.glob("*.json")} if QUEUE.is_dir() else set()
    blocked, pen, bad = hygiene.blocked(), penalties(), bad_facts()
    fresh, old = [], []
    for rel in ents:
        name = Path(rel).stem
        if name in parked or hygiene.slug(name).lower() in blocked:
            continue
        rec = data["notes"].get(rel)
        if not rec or rec["hash"] != chash(file_text(rel)):
            score, why = uncertainty(v, rel, pen, bad)
            fresh.append((-score, rel, why))
        else:
            old.append((rec["at"], rel))
    fresh.sort()
    if fresh:
        _, rel, why = fresh[0]
        return rel, find_cands(v, data, rel, ents, S, T), "least sure first: " + (why or "nothing flagged")
    if cfg["connect"] and cfg["cycle"]:
        for _, rel in sorted(old):
            c = find_cands(v, data, rel, ents, S, T)
            if c:
                return rel, c, "looking for connections it has not been shown yet"
    return None, [], ""


def windows(text, terms, share):
    if len(text) <= share:
        return text
    low, spans = text.lower(), []
    for term in terms:
        i = low.find(term)
        while i >= 0 and len(spans) < 12:
            spans.append((max(0, i - 2500), min(len(text), i + 3000)))
            i = low.find(term, i + 5500)
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    out, used = [], 0
    for a, b in merged:
        b = min(b, a + max(0, share - used))
        if b <= a:
            break
        out.append(text[a:b])
        used += b - a
    return "\n...\n".join(out) or text[:share]


def cand_line(v, o):
    facts = "; ".join(f for _, f in mentions(v.body[o]))
    return f"- {o} | {v.title(o)} | {re.sub(chr(92) + 's+', ' ', facts or strip(v.body[o]))[:380]}"


def prompt_tokens(cfg):
    """The prompt size setting, clamped so the prompt plus the answer always fit in the context."""
    s = settings.get("cur")
    return max(2000, min(int(cfg["prompt_tokens"]), int(s["context"]) - int(s["max_tokens"]["curate"]) - 2500))


def build(v, rel, cands, cfg):
    budget = int(prompt_tokens(cfg) * CHARS_PER_TOKEN)
    body = strip(v.body[rel])
    lines = mentions(body)
    facts = "\n".join(f"{i + 1}. {f[:300]}" for i, (_, f) in enumerate(lines))
    head = (f"NOTE {rel} (title: {v.title(rel)}):\n{body[:__import__('tuning').get('curator.note_chars')]}\n\nFACT LINES:\n{facts or '(none)'}\n\n")
    linked = sorted(o for o in (set(v.out[rel]) | set(v.back[rel])) if o.startswith("entities/") and o != rel)[:__import__("tuning").get("curator.linked_shown")]
    ltxt = "\n".join(cand_line(v, o) for o in linked) or "(none)"
    ctxt = "\n".join(cand_line(v, o) for o in cands) or "(none)"
    srcs = sources_of(v, rel)[:__import__("tuning").get("curator.source_chats")]
    terms = {Path(rel).stem.replace("-", " ").lower(), v.title(rel).lower()}
    terms |= {str(a).lower() for a in as_list((v.meta.get(rel) or {}).get("aliases"))}
    terms = [t for t in terms if len(t) >= 3]
    room = max(8000, budget - len(head) - len(ltxt) - len(ctxt) - 3000)
    ex = "\n\n".join(f"=== {s} ===\n" + windows(v.body[s], terms, room // max(1, len(srcs))) for s in srcs)
    user = (head + f"NOTES THIS ONE ALREADY LINKS TO:\n{ltxt}\n\nSOURCE CHATS (the notes were written from these):\n"
            f"{ex or '(none found)'}\n\nCANDIDATE NOTES (not linked to this one):\n{ctxt}")
    blob = body + "\n" + ex + "\n" + "\n".join(strip(v.body[o]) for o in linked)
    return user, blob, set(cands), set(cands) | set(linked), lines


def system(cfg):
    import prompts
    return prompts.curator_system(cfg, _system_default)


def _system_default(cfg):
    jobs = []
    if cfg["verify"]:
        jobs.append("VERIFY: for each fact line say supported, unsupported or contradicted by the SOURCE CHATS. "
                    "For contradicted, quote the chat text that shows it.")
    if cfg["connect"]:
        jobs.append("CONNECT: smaller models wrote these notes from a few messages at a time and missed links across "
                    "chats. Read the source chats for things the notes missed, and propose a link to a CANDIDATE only "
                    "if the two notes are clearly about related things; quote exact words from this note or the source "
                    "chats (quote_a) and exact words from the candidate (quote_b). Also report a CONFLICT if a "
                    "candidate or a linked note states something that disagrees with this note.")
    if cfg["prune"]:
        jobs.append("PRUNE: decide keep, prune or unsure. Prune only if the note records nothing useful: a stray word, "
                    "a generic term, a file name, or something mentioned in passing with no lasting information. "
                    "Set certain true only if you are sure.")
    return ("You are the curator of one person's notes vault. Take your time and be thorough; nothing else "
            "needs you. You get ONE entity note, numbered fact lines, the notes it links to, the SOURCE CHATS and "
            "CANDIDATE notes. " + " ".join(jobs) + " Quote exactly; never invent; when unsure say so. Reply with ONLY "
            'JSON: {"facts":[{"line":1,"verdict":"supported|unsupported|contradicted","quote":"","note":""}],'
            '"links":[{"to":"<candidate path>","why":"","quote_a":"","quote_b":""}],'
            '"conflicts":[{"with":"<candidate or linked path>","why":"","quote_a":"","quote_b":""}],'
            '"prune":{"decision":"keep|prune|unsure","certain":false,"reason":""}}')


def ask_model(sys_prompt, user):
    S = settings.get("cur")
    body = {"model": S["model_file"], "temperature": S["temperature"], "max_tokens": int(S["max_tokens"]["curate"]),
            "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]}
    n = S["model_file"].lower()
    if "gpt-oss" in n:
        body["chat_template_kwargs"] = {"reasoning_effort": S["reasoning_effort"]}
    elif "qwen3" in n:
        body["chat_template_kwargs"] = {"enable_thinking": bool(S.get("thinking", True))}
    msg = thinklog.chat("cur", ENDPOINT, body, "curate", slot=0)
    text = re.sub(r"\[THINK\].*?\[/THINK\]|<think>.*?</think>|<seed:think>.*?</seed:think>", "", msg.get("content") or "", flags=re.S)
    return agent.parse_json(re.sub(r"^.*?(\[/THINK\]|</think>|</seed:think>)", "", text, flags=re.S))


# ---------- acting on the answer ----------
def write_links(rel, links):
    p = VAULT / rel
    if not p.exists():
        return
    t = p.read_text(encoding="utf-8")
    new = BLOCK.sub("", t).rstrip("\n") + "\n"
    if links:
        rows = "\n".join(f"- [[{l['to'][:-3] if l['to'].endswith('.md') else l['to']}]]: {l['why']}" for l in links)
        new += f"\n{START}\n## Related (added by the curator)\n{rows}\n{END}\n"
    if new != t:
        p.write_text(new, encoding="utf-8")


def card(question, why, options, payload):
    qid = questions.ask(question, why=why, kind="curate",
                        options=[{"label": o, "value": o} for o in options], payload=payload)
    if qid is None:
        raise Full()


def auto_prune_ok(v, rel, lines):
    if str((v.meta.get(rel) or {}).get("confidence")) != "low" or len(lines) > 2:
        return False
    return all(str(b).startswith(("summaries/", "timelines/", "entities/")) for b in v.back[rel])


def apply_result(v, rel, out, cfg, data, blob, cset, cconf, lines):
    st, title, now, full = data["stats"], v.title(rel), datetime.now().isoformat(timespec="seconds"), False
    name = Path(rel).stem
    try:
        if cfg["verify"]:
            asked = 0
            for f in (out.get("facts") or [])[:__import__("tuning").get("curator.facts_checked")]:
                if not isinstance(f, dict):
                    continue
                try:
                    fact = lines[int(f.get("line")) - 1][1]
                except (TypeError, ValueError, IndexError):
                    continue
                vd, quote = f.get("verdict"), str(f.get("quote", ""))
                if vd == "supported":
                    st["verified"] += 1
                    continue
                if vd not in ("unsupported", "contradicted") or asked >= __import__("tuning").get("curator.questions_per_note"):
                    continue
                if vd == "contradicted" and not reviewer.grounded(quote, blob):
                    continue
                why = (f'The chats say: "{quote[:200]}". ' if vd == "contradicted"
                       else "The curator could not find this in the chats. ") + str(f.get("note", ""))[:150]
                card(f'Is this still right? In "{title}": "{fact[:240]}"', why,
                     ["Still true", "Not true", "Not sure"], {"act": "fact", "rel": rel, "line": fact[:80]})
                asked += 1
                st["asked"] += 1
        if cfg["connect"]:
            mine = data["links"].setdefault(rel, [])
            have = {l["to"] for l in mine}
            for l in (out.get("links") or [])[:__import__("tuning").get("curator.links_per_note")]:
                if not isinstance(l, dict):
                    continue
                to = str(l.get("to", ""))
                qa, qb = str(l.get("quote_a", "")), str(l.get("quote_b", ""))
                why = re.sub(r"\s+", " ", str(l.get("why", "")).replace("[[", "").replace("]]", ""))[:200]
                if (to not in cset or to in have or not why or not reviewer.grounded(qa, blob)
                        or not reviewer.grounded(qb, strip(v.body[to]))):
                    continue
                mine.append({"to": to, "why": why, "quote_a": qa[:200], "quote_b": qb[:200], "at": now})
                have.add(to)
                st["links"] += 1
                reviewgate.note_changed([rel], f"curator linked {rel} to {to}: {why}")
                log({"act": "link", "from": rel, "to": to, "why": why, "quote_a": qa[:200], "quote_b": qb[:200]})
            write_links(rel, mine)
            if not mine:
                data["links"].pop(rel, None)
            for c in (out.get("conflicts") or [])[:__import__("tuning").get("curator.conflicts_per_note")]:
                if not isinstance(c, dict):
                    continue
                other, qa, qb = str(c.get("with", "")), str(c.get("quote_a", "")), str(c.get("quote_b", ""))
                if other not in cconf or not reviewer.grounded(qa, blob) or not reviewer.grounded(qb, strip(v.body[other])):
                    continue
                ot = v.title(other)
                card(f'Two notes disagree. "{title}" says: "{qa[:150]}" but "{ot}" says: "{qb[:150]}". Which is right?',
                     str(c.get("why", ""))[:300], [f"{title} is right", f"{ot} is right", "Both are true"],
                     {"act": "conflict", "rel": rel, "with": other})
                st["asked"] += 1
        p = out.get("prune") if isinstance(out.get("prune"), dict) else {}
        if cfg["prune"] and p.get("decision") in ("prune", "unsure"):
            if hygiene.slug(name).lower() in hygiene._load(hygiene.KEEP):
                return full
            reason = re.sub(r"\s+", " ", str(p.get("reason", "")))[:300] or "nothing useful recorded"
            if p.get("decision") == "prune" and p.get("certain") is True and auto_prune_ok(v, rel, lines):
                hygiene.add_block(name)
                qid = hashlib.sha1(f"curate|prune|{name}|{now}".encode()).hexdigest()[:12]
                questions.QDIR.mkdir(parents=True, exist_ok=True)
                questions._save({"id": qid, "created": now, "status": "answered", "answered": now, "kind": "curate",
                                 "question": f'Pruned "{name.replace("-", " ")}"', "why": reason, "options": [],
                                 "payload": {"act": "prune", "entity": name, "at": now}, "answer": "pruned"})
                for r in list(data["links"]):
                    data["links"][r] = [l for l in data["links"][r] if l["to"] != rel]
                st["pruned"] += 1
                log({"act": "prune", "note": rel, "reason": reason})
            else:
                qid = questions.ask(f'Keep "{name.replace("-", " ")}" as its own note?', why="Curator: " + reason,
                                    kind="hygiene", payload={"entity": name},
                                    options=[{"label": "Keep it", "value": "keep"}, {"label": "Drop it", "value": "drop"}])
                if qid is None:
                    raise Full()
                st["asked"] += 1
    except Full:
        full = True
    return full


def park(rel, why):
    QUEUE.mkdir(parents=True, exist_ok=True)
    at = datetime.now().isoformat(timespec="seconds")
    (QUEUE / (Path(rel).stem + ".json")).write_text(json.dumps({"rel": rel, "why": why, "at": at}))
    questions.ask(f'Could not review note "{Path(rel).stem}"', why=f"Reason: {why}.", kind="curatecheck",
                  payload={"rel": rel, "at": at},
                  options=[{"label": "Try again", "value": "retry"}, {"label": "Skip this note", "value": "skip"}])
    agent.log(f"curator: parked {rel} ({why})")


def decide(rel, text):
    """Your answer to a parked note. Skip marks it checked at its current content; either answer unparks it."""
    if text == "skip":
        d = load_data()
        d["notes"][rel] = {"hash": chash(file_text(rel)), "at": datetime.now().isoformat(timespec="seconds"),
                           "checks": d["notes"].get(rel, {}).get("checks", 0), "skipped": True}
        save_data(d)
    (QUEUE / (Path(rel).stem + ".json")).unlink(missing_ok=True)


# ---------- one note and the loop ----------
def indexes(v):
    ents = sorted(r for r in v.body if r.startswith("entities/"))
    S = {r: {t for t, _ in mentions(v.body[r])} for r in ents}
    T = {r: toks(v.title(r) + " " + strip(v.body[r])) for r in ents}
    return ents, S, T


def unit(cfg):
    data = load_data()
    v = Vault(VAULT)
    for r in list(data["links"]):
        write_links(r, data["links"][r])
    ents, S, T = indexes(v)
    rel, cands, why = next_note(v, data, cfg, ents, S, T)
    import mapper
    if (not rel or why.startswith("looking")) and mapper.wants(cfg, v):
        return mapper.step(cfg, data, v)
    if not rel:
        status("waiting", "every note is checked at its current content; waiting for a change")
        return False
    status("working", why, rel)
    try:
        user, blob, cset, cconf, lines = build(v, rel, cands, cfg)
        out = None
        for attempt in range(2):
            u = user if attempt == 0 else user + "\n\nYour last answer was not valid JSON in the required shape. Reply with the JSON only."
            out = ask_model(system(cfg), u)
            if isinstance(out, dict):
                break
        else:
            park(rel, "the model gave no usable answer twice")
            return True
        full = apply_result(v, rel, out, cfg, data, blob, cset, cconf, lines)
    except Exception as e:
        try:
            import power
            if power.consume_kill("cur"):
                status("waiting", "stopped by you with the power button")
                return False
        except Exception:
            pass
        park(rel, f"{type(e).__name__}: {str(e)[:100]}")
        return True
    if full:
        save_data(data)
        status("waiting", "the Ask queue is full, so questions cannot be asked; answer some and it continues")
        return False
    data["notes"][rel] = {"hash": chash(file_text(rel)), "at": datetime.now().isoformat(timespec="seconds"),
                          "checks": data["notes"].get(rel, {}).get("checks", 0) + 1}
    if cfg["connect"]:
        data["pairs"] |= {pk(rel, o) for o in cands}
    save_data(data)
    agent.log(f"curator: reviewed {rel}")
    return True


def sig():
    parts = []
    for d in (VAULT / "entities", VAULT / "summaries", questions.QDIR):
        try:
            fs = list(d.glob("*")) if d.is_dir() else []
            parts.append(f"{len(fs)}:{max([f.stat().st_mtime_ns for f in fs] or [0])}")
        except OSError:
            parts.append("x")
    for f in (CFG_FILE, settings.FILE):
        parts.append(str(f.stat().st_mtime_ns if f.exists() else 0))
    parts.append("manual" if _manual() else "-")
    return "|".join(parts)


def wait_change():
    before = sig()
    while sig() == before:
        time.sleep(POLL)


def summary():
    d = load_data()
    n = len(list((VAULT / "entities").glob("*.md"))) if (VAULT / "entities").is_dir() else 0
    return {"cfg": load_cfg(), "status": load_status(), "stats": d["stats"], "checked": len(d["notes"]), "total": n}


def _manual():
    try:
        import power
        return power.manual("cur")
    except Exception:
        return False


def watch():
    agent.log("curator started")
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    if swap.holding() or unit_active():
        release()
    idle_since = None
    try:
        while True:
            cfg = load_cfg()
            if _manual() and not swap.holding() and unit_active():
                if applier.health("cur") != "ready":
                    status("loading", "the curator model you loaded is still loading")
                    time.sleep(SAMPLE)
                    continue
                try:
                    did = unit(cfg)
                except Exception as e:
                    agent.log(f"curator error: {type(e).__name__}: {str(e)[:100]}")
                    status("waiting", f"error: {type(e).__name__}")
                    did = False
                if not did:
                    wait_change()
                continue
            if not cfg["enabled"]:
                if swap.holding() or (unit_active() and not _manual()):
                    status("off", "finished; unloading the curator and loading the Sweeper and Reviewer")
                    release()
                status("off", "switched off")
                idle_since = None
                wait_change()
                continue
            try:
                import remote
                ext = remote.enabled("cur")
            except Exception:
                ext = False
            if ext:
                try:
                    did = unit(cfg)
                except Exception as e:
                    agent.log(f"curator error: {type(e).__name__}: {str(e)[:100]}")
                    status("waiting", f"error: {type(e).__name__}")
                    did = False
                if not did:
                    wait_change()
                continue
            if swap.holding():
                if swap.wanted():
                    status("waiting", "another job needs the Sweeper or Reviewer; swapping back")
                    release()
                    idle_since = None
                    continue
                try:
                    did = unit(cfg)
                except Exception as e:
                    agent.log(f"curator error: {type(e).__name__}: {str(e)[:100]}")
                    status("waiting", f"error: {type(e).__name__}")
                    did = False
                if not did:
                    release()
                    wait_change()
                continue
            if swap.others_idle():
                idle_since = idle_since or time.time()
            else:
                idle_since = None
            if idle_since is None:
                status("waiting", "the Sweeper or Reviewer is working")
                time.sleep(SAMPLE)
                continue
            left = __import__("tuning").get("curator.idle_seconds") - (time.time() - idle_since)
            if left > 0:
                status("waiting", f"both idle, swapping in {int(left)}s if they stay idle")
                time.sleep(SAMPLE)
                continue
            if not acquire():
                idle_since = None
                time.sleep(SAMPLE)
    finally:
        try:
            release()
        except Exception as e:
            agent.log(f"curator: release failed: {type(e).__name__}")


if __name__ == "__main__":
    a = sys.argv
    if "--watch" in a:
        watch()
    elif "--release" in a:
        if swap.holding() or unit_active():
            release()
    elif "--once" in a:
        if applier.health("cur") != "ready":
            sys.exit("the curator server is not running: systemctl start memory-curator-llm")
        print("reviewed" if unit(load_cfg()) else "nothing to do")
    elif "--show" in a or "--rank" in a:
        v = Vault(VAULT)
        ents, S, T = indexes(v)
        if "--rank" in a:
            pen, bad = penalties(), bad_facts()
            rows = sorted((-uncertainty(v, r, pen, bad)[0], r, uncertainty(v, r, pen, bad)[1]) for r in ents)
            for s, r, why in rows[:15]:
                print(-s, r, "-", why)
        else:
            rel = a[a.index("--show") + 1]
            user, blob, cset, cconf, lines = build(v, rel, find_cands(v, load_data(), rel, ents, S, T), load_cfg())
            print(len(user), "characters,", len(lines), "fact lines,", len(cset), "candidates,",
                  len(cconf - cset), "linked notes")
            print(user[:3000])
    else:
        sys.exit(__doc__)
