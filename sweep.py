"""Chunked sweep: read each full conversation in order, a chunk at a time, keeping a running recap.

Per chunk the model returns a recap plus grounded facts. A fact is kept only if the exact words it
quotes really appear in the chunk. Notes written (never raw/, never git):
  summaries/<chat>.md      recap + entity links, rewritten after every chunk
  timelines/<entity>.md    dated facts about one entity, across all chats, oldest first
  entities/<entity>.md     created as a small stub only if it does not exist yet
Resumable and incremental: chunk fingerprints are stored, so after a re-import only changed or
new chunks are processed again. Optional <state>/merges.yaml maps alias -> canonical entity name.
"""
import hashlib
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

import yaml

import agent
import apply_merges
import hygiene
import questions
import reviewgate
import settings
from bootstrap import clean, slug

VAULT, STATE, M = agent.VAULT, agent.STATE, agent.M
SWEEP = STATE / "sweep.json"
MERGES = STATE / "merges.yaml"
CONV_DIR = VAULT / "raw" / "conversations"
CHUNK_CHARS = 8000
MAX_SINGLE = 12000  # one message up to this size is never cut
MAX_FACTS = 6
MAX_TRIES = 3
HEAD_RE = re.compile(r"^### (Human|Assistant)(?: \(([^)]*)\))?[ \t]*$", re.M)

SYS = (
    "You read one chat between a person and an AI assistant, one chunk "
    "at a time, to keep documentation notes. Reply with ONLY JSON: "
    '{"recap":"<updated recap of the WHOLE conversation so far, facts and decisions>",'
    '"facts":[{"entity":"<specific named thing>","kind":"added|removed|changed|decided|problem|fix|info",'
    '"fact":"<one sentence on what happened or is true>",'
    '"quote":"<exact words copied from the chunk, up to 120 characters, supporting the fact>"}],'
    '"question":"<ONE short question for the person, only if something important is unclear or contradicts '
    'the recap; otherwise an empty string>"}. '
    "Keep to the LIMITS given with the chunk. Only what the chunk states or the person actually did. Do not invent. "
    "If nothing notable, facts is an empty list.")


def limits_line():
    """The Sweeper's limits, from the settings at call time: the settings are the only place these numbers live."""
    t = __import__("tuning")
    parts = ["any number of facts" if t.off("sweep.max_facts") else f"at most {t.get('sweep.max_facts')} facts"]
    if not t.off("sweep.min_quote_chars"):
        parts.append(f"each quote at least {t.get('sweep.min_quote_chars')} characters")
    if not t.off("sweep.recap_chars"):
        parts.append(f"recap at most about {max(1, t.get('sweep.recap_chars') * 2 // 15)} words")
    return "LIMITS: " + "; ".join(parts) + ".\n\n"


# ---------- reading and chunking ----------
def split_messages(body):
    """-> list of (stamp, text_with_heading)"""
    marks = list(HEAD_RE.finditer(body))
    msgs = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        msgs.append((m.group(2) or "", body[m.start():end].strip()))
    return msgs


def split_big(text, limit):
    """Split one oversized message into pieces of at most `limit` characters.
    The heading always stays with the first slice of the text."""
    pieces, cur = [], ""
    for para in re.split(r"(?<=\n)\n?", text):
        if len(cur) + len(para) <= limit:
            cur += para
        elif len(para) <= limit:
            pieces.append(cur)
            cur = para
        else:
            while len(cur) + len(para) > limit:
                take = limit - len(cur)
                pieces.append(cur + para[:take])
                cur, para = "", para[take:]
            cur += para
    if cur:
        pieces.append(cur)
    return pieces


def make_chunks(body, d=None):
    import chunking
    return chunking.make_chunks(body, d, __import__("tuning").get("sweep.chunk_chars"), split_messages, split_big, MAX_SINGLE)


def _make_chunks_old(body):
    chunks, cur, first, last = [], [], None, 0
    for n, (stamp, text) in enumerate(split_messages(body), start=1):
        parts = split_big(text, MAX_SINGLE) if len(text) > MAX_SINGLE else [text]
        for k, part in enumerate(parts):
            if k:
                part = f"(continued)\n{part}"
            if cur and sum(len(c) for c in cur) + len(part) > CHUNK_CHARS:
                chunks.append({"text": "\n\n".join(cur), "first": first, "last": last})
                cur, first = [], None
            if first is None:
                first = (n, stamp[:10])
            cur.append(part)
            last = n
    if cur:
        chunks.append({"text": "\n\n".join(cur), "first": first, "last": last})
    for c in chunks:
        c["start_msg"], c["date"] = c["first"]
        c["hash"] = hashlib.sha256(c["text"].encode()).hexdigest()[:16]
    return chunks


def read_chat(rel):
    raw = (VAULT / rel).read_text(encoding="utf-8", errors="replace")
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
    meta = (yaml.safe_load(m.group(1)) or {}) if m else {}
    body = raw[m.end():] if m else raw
    body = re.sub(r"\A# .*\n", "", body.lstrip(), count=1)
    return meta, body


# ---------- model ----------
def call_model(user):
    body = {"model": M["name"], "temperature": settings.get("qwen")["temperature"], "max_tokens": settings.get("qwen")["max_tokens"]["sweep"],
            "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": user}],
            "chat_template_kwargs": {"enable_thinking": settings.get("qwen")["thinking"]}}
    import thinklog
    text = thinklog.chat("qwen", M["endpoint"], body, "sweep", slot=0).get("content") or ""
    return agent.parse_json(text)


def norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()


def grounded(quote, chunk_text):
    q = norm(str(quote or ""))   # an empty quote is never evidence, whatever the minimum length is set to
    return bool(q) and len(q) >= __import__("tuning").get("sweep.min_quote_chars") and q in norm(chunk_text)


# ---------- state ----------
def load_state():
    if SWEEP.exists():
        return json.loads(SWEEP.read_text())
    return {"docs": {}, "files": {}}


def save_state(st):
    tmp = SWEEP.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, ensure_ascii=False))
    tmp.replace(SWEEP)


def load_merges():
    if MERGES.exists():
        d = yaml.safe_load(MERGES.read_text()) or {}
        return {slug(k).lower(): str(v) for k, v in d.items()}
    return {}


def entity_key(name, merges):
    k = slug(name).lower()
    return slug(merges[k]).lower() if k in merges else k


# ---------- notes ----------
def entity_file(st, key, name):
    if key in st["files"]:
        return st["files"][key]
    bp = STATE / "bootstrap.json"
    if bp.exists():
        e = json.loads(bp.read_text()).get("entities", {}).get(key)
        if e:
            st["files"][key] = e["file"]
            return e["file"]
    st["files"][key] = slug(name)
    return st["files"][key]


def write_timeline(st, key, merges):
    rows, name = [], None
    for rel, d in st["docs"].items():
        for f in d["facts"]:
            if f["ok"] and entity_key(f["entity"], merges) == key:
                name = name or f["entity"]
                rows.append((f["date"], rel, f["chunk"], f))
    if not rows or key in hygiene.blocked():
        f = st["files"].get(key)
        if f:
            (VAULT / "timelines" / f"{f}.md").unlink(missing_ok=True)
            ent = VAULT / "entities" / f"{f}.md"
            if ent.exists() and "See [[timelines/" in ent.read_text(encoding="utf-8"):
                ent.unlink()
        return
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    file = entity_file(st, key, name)
    lines = [f"- {d or 'undated'} · {f['kind']} — {clean(f['fact'])[:300]} "
             f"([[{rel[:-3]}]], msgs {f['msgs']})" for d, rel, _, f in rows]
    tl = VAULT / "timelines" / f"{file}.md"
    tl.parent.mkdir(parents=True, exist_ok=True)
    fm = {"type": "timeline", "entity": file, "confidence": "low"}
    tl.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n"
                  f"# {clean(name)}: timeline\n\nEntity: [[entities/{file}|{clean(name)}]]\n\n"
                  "Facts quoted from chats (each checked against the chat text), oldest first.\n\n"
                  + "\n".join(lines) + "\n", encoding="utf-8")
    ent = VAULT / "entities" / f"{file}.md"
    if not ent.exists():
        ent.parent.mkdir(parents=True, exist_ok=True)
        ent.write_text("---\n" + yaml.safe_dump(
            {"type": "entity", "aliases": [clean(name)], "confidence": "low"}, sort_keys=False)
            + f"---\n\n# {clean(name)}\n\nSee [[timelines/{file}|timeline]].\n", encoding="utf-8")


def write_summary(st, rel, meta, total, merges):
    d = st["docs"][rel]
    keys = []
    for f in d["facts"]:
        if f["ok"]:
            k = entity_key(f["entity"], merges)
            if k not in keys:
                keys.append(k)
    links = ", ".join(f"[[entities/{st['files'][k]}]]" for k in keys[:25] if k in st["files"] and k not in hygiene.blocked()) or "none"
    done = len(d["hashes"])
    title = clean(str(meta.get("title") or Path(rel).stem))[:100]
    fm = {"type": "summary", "source": rel, "confidence": "low", "title": title,
          "status": "complete" if done >= total else f"in progress ({done}/{total} chunks)"}
    recap = clean(d["recaps"][-1]) if d["recaps"] else "(not read yet)"
    clar = "".join(f"- {clean(x['q'])} → {clean(x['a'])}\n" for x in d.get("clarifications", []))
    clar = f"\n**Clarified by you:**\n\n{clar}" if clar else ""
    if d.get("flags"):
        clar += f"\n**Waiting on your answer:** {len(d['flags'])} unclear part(s) will be re-read.\n"
    out = VAULT / "summaries" / f"{slug(Path(rel).stem, 80)}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n"
                   f"# {title}\n\n{recap}\n\n**Source:** [[{rel[:-3]}]]\n\n"
                   f"**Entities:** {links}\n{clar}", encoding="utf-8")


def sync_merges(st, merges):
    """If merges.yaml changed (you answered a 'same thing?' question), merge the entity notes and
    rewrite the timelines. `merges` is updated in place so the running sweep uses the new names."""
    raw = MERGES.read_text() if MERGES.exists() else ""
    raw += hygiene.blocklist_text()
    stamp = hashlib.sha1(raw.encode()).hexdigest()
    if st.get("merges_applied") == stamp:
        return False
    if st.get("merges_applied") is None and not raw.strip():
        st["merges_applied"] = stamp
        return False
    try:
        apply_merges.main()
    except Exception as e:
        agent.log(f"sweep: merge of entity notes failed ({type(e).__name__}: {str(e)[:80]}), will retry")
        return False
    hygiene.apply_blocklist()
    try:
        import filestore
        filestore.auto()
    except Exception:
        pass
    try:
        import qhistory
        qhistory.purge()
    except Exception:
        pass
    merges.clear()
    merges.update(load_merges())
    rebuild(st, merges)
    st["merges_applied"] = stamp
    save_state(st)
    agent.log("sweep: applied merges.yaml changes")
    return True


# ---------- main loop ----------
def background(d, chunks, idx):
    """Context from before this chunk. Background only: facts still have to be quoted from the new chunk."""
    import tuning
    out = ""
    n = tuning.get("sweep.overlap_chars")
    if n and idx:
        out += ("END OF THE PREVIOUS CHUNK (background only; never quote from it):\n"
                + chunks[idx - 1]["text"][-n:] + "\n\n")
    m = tuning.get("sweep.facts_context")
    if m and idx:
        prior = [f for f in d["facts"] if f["chunk"] < idx and f["ok"]][-m:]
        if prior:
            out += ("FACTS ALREADY RECORDED EARLIER IN THIS CHAT (background; skip repeats, but report anything the new chunk changes or contradicts):\n"
                    + "\n".join(f"- {f['entity']} ({f['kind']}): {f['fact']}" for f in prior) + "\n\n")
    return out


def run_chunk(st, rel, d, meta, chunks, idx, merges, clarification=None):
    """Read chunk idx with the model and record its grounded facts. False if the model failed."""
    sync_merges(st, merges)
    c = chunks[idx]
    while not agent.model_ready():
        time.sleep(30)
    recap = d["recaps"][idx - 1] if idx else ""
    extra = ""
    if clarification:
        extra = ("THE PERSON CLARIFIED THIS (trust it over the chat):\n"
                 f"Question: {clarification['q']}\nAnswer: {clarification['a']}\n\n")
    user = f"{limits_line()}RECAP SO FAR:\n{recap or '(start of conversation)'}\n\n{background(d, chunks, idx)}{extra}NEW CHUNK:\n{c['text']}"
    try:
        import json as _j
        (agent.STATE / "sweep_now.json").write_text(_j.dumps({"rel": rel, "chunk": idx + 1, "of": len(chunks), "at": time.time()}))
    except Exception:
        pass
    t0, data = time.time(), reviewgate.queued(rel, c["hash"], clarification)
    for attempt in range(0 if data else __import__("tuning").get("sweep.attempts")):
        wait_server()   # a server that is loading or restarting costs no attempt
        try:
            data = call_model(user)
        except Exception as e:
            agent.log(f"sweep FAILED {rel} chunk {idx}: {type(e).__name__}: {str(e)[:80]}")
            if not isinstance(e, __import__("thinklog").Aborted):   # a server switch or stop: try again on the new one at once
                time.sleep(60)
            continue
        if data and "recap" in data:
            break
        agent.log(f"sweep: unusable reply for {rel} chunk {idx} (try {attempt + 1})")
        data = None
    if data is None:
        return False

    def redo(feedback):
        return call_model(user + "\n\nREVIEWER FEEDBACK:\n" + feedback)

    data, wait = reviewgate.settle(rel, idx, c["hash"], data, redo, clarification)
    if wait:
        return False
    try:
        import taskcheck
        taskcheck.check(rel, data)
    except Exception:
        pass
    touched = {entity_key(f["entity"], merges) for f in d["facts"] if f["chunk"] == idx and f["ok"]}
    kept = bad = 0
    d["facts"] = [f for f in d["facts"] if f["chunk"] != idx]
    for f in (data.get("facts") or [])[:__import__("tuning").get("sweep.max_facts")]:
        if not isinstance(f, dict) or not str(f.get("entity", "")).strip() or not f.get("fact"):
            continue
        ok = grounded(f.get("quote"), c["text"])
        d["facts"].append({"chunk": idx, "date": c["date"], "entity": clean(f["entity"])[:60],
                           "kind": clean(f.get("kind", "info"))[:12], "fact": clean(f["fact"])[:300],
                           "quote": str(f.get("quote", ""))[:150], "ok": ok,
                           "msgs": f"{c['start_msg']}-{c['last']}"})
        kept += ok
        bad += not ok
        if ok:
            touched.add(entity_key(f["entity"], merges))
    recap_new = clean(str(data["recap"]))[:__import__("tuning").get("sweep.recap_chars")]
    if idx < len(d["hashes"]):
        d["recaps"][idx] = recap_new
    else:
        d["hashes"].append(c["hash"])
        d["recaps"].append(recap_new)
    q = str(data.get("question") or "").strip()
    if len(q) > 15 and not clarification:
        qid = questions.ask(clean(q), payload={"rel": rel, "chunk": idx},
                            why=f"From {Path(rel).name[:60]}, messages {c['start_msg']}-{c['last']}. "
                                "That part of the chat is re-read with your answer.")
        if qid:
            d.setdefault("flags", {})[str(idx)] = qid
    for k in touched:
        write_timeline(st, k, merges)
    write_summary(st, rel, meta, len(chunks), merges)
    save_state(st)
    agent.log(f"sweep: {Path(rel).name[:50]} chunk {idx + 1}/{len(chunks)}"
              + (" (re-read with your answer)" if clarification else "")
              + f": {kept} facts kept, {bad} ungrounded, {time.time() - t0:.0f}s")
    return True


def revisit(st, merges):
    """Re-read every flagged chunk whose question has been answered. Returns chunks re-read."""
    work = 0
    for rel, d in st["docs"].items():
        flags = d.get("flags") or {}
        for key in sorted(flags, key=int):
            q = questions.get(flags[key])
            if q is None or q["status"] == "open":
                continue
            idx = int(key)
            if q["status"] == "answered":
                meta, body = read_chat(rel)
                chunks = make_chunks(body, d)
                if idx < len(chunks) and idx < len(d["hashes"]) and d["hashes"][idx] == chunks[idx]["hash"]:
                    cl = {"q": q["question"], "a": q["answer"], "chunk": idx}
                    if not run_chunk(st, rel, d, meta, chunks, idx, merges, clarification=cl):
                        continue
                    d.setdefault("clarifications", []).append(cl)
                    work += 1
            del flags[key]
        d["flags"] = flags
        save_state(st)
    return work


def process_doc(rel, st, merges):
    meta, body = read_chat(rel)
    d = st["docs"].setdefault(rel, {"hashes": [], "recaps": [], "facts": []})
    chunks = make_chunks(body, d)
    i = 0
    while i < len(d["hashes"]) and i < len(chunks) and d["hashes"][i] == chunks[i]["hash"]:
        i += 1
    if i < len(d["hashes"]):
        gone = {entity_key(f["entity"], merges) for f in d["facts"] if f["chunk"] >= i and f["ok"]}
        d["hashes"], d["recaps"] = d["hashes"][:i], d["recaps"][:i]
        d["facts"] = [f for f in d["facts"] if f["chunk"] < i]
        d["flags"] = {k: v for k, v in (d.get("flags") or {}).items() if int(k) < i}
        d["clarifications"] = [x for x in d.get("clarifications", []) if x["chunk"] < i]
        for k in gone:
            write_timeline(st, k, merges)
    work = 0
    for idx in range(i, len(chunks)):
        if not run_chunk(st, rel, d, meta, chunks, idx, merges):
            return work, False
        work += 1
        work += revisit(st, merges)
    write_summary(st, rel, meta, len(chunks), merges)
    return work, True


def one_pass():
    st, merges = load_state(), load_merges()
    sync_merges(st, merges)
    hygiene.queue()
    work, complete = revisit(st, merges), True
    for p in sorted(CONV_DIR.glob("*.md")):  # filenames start with the date: oldest first
        rel = p.relative_to(VAULT).as_posix()
        w, ok = process_doc(rel, st, merges)
        work += w
        complete &= ok
    save_state(st)
    return work, complete


def rebuild(st=None, merges=None):
    """Rewrite all sweep notes from saved state (no model calls). Applies merges.yaml."""
    st = st if st is not None else load_state()
    merges = merges if merges is not None else load_merges()
    live = set()
    for d in st["docs"].values():
        live |= {entity_key(f["entity"], merges) for f in d["facts"] if f["ok"]}
    live -= hygiene.blocked()
    for key in sorted(live):
        write_timeline(st, key, merges)
    for key in [k for k in st["files"] if k not in live]:
        file = st["files"].pop(key)
        (VAULT / "timelines" / f"{file}.md").unlink(missing_ok=True)
        ent = VAULT / "entities" / f"{file}.md"
        if ent.exists() and "See [[timelines/" in ent.read_text(encoding="utf-8"):
            ent.unlink()  # a stub this sweep created; real entity notes are left alone
    for rel in st["docs"]:
        meta, body = read_chat(rel)
        write_summary(st, rel, meta, len(make_chunks(body, st["docs"].get(rel))), merges)
    save_state(st)
    agent.log(f"sweep: rebuilt notes for {len(live)} entities")
    return st


def wait_server(longest=1800):
    """Wait while the Sweeper's server (local or external) is loading or restarting, for example after Apply all.
    Waiting is not a failed attempt; after `longest` seconds the attempt goes ahead and reports what went wrong."""
    import applier
    end, said = time.time() + longest, False
    while applier.health("qwen") != "ready" and time.time() < end:
        if not said:
            agent.log("sweep: waiting for the Sweeper's server to answer")
            said = True
        time.sleep(5)


def fingerprint():
    """Cheap snapshot of what can give the sweep new work: imported chats, your answers, the Assistant's jobs, and a
    switch, stop or restart of the Sweeper's server (its abort signal), so a chunk set back by a restart goes again."""
    import jobs
    import thinklog
    parts = []
    for d in (CONV_DIR, questions.QDIR, jobs.DIR):
        for f in sorted(d.glob("*")) if d.exists() else []:
            st = f.stat()
            parts.append(f"{f.name}:{st.st_size}:{st.st_mtime_ns}")
    try:
        parts.append((thinklog.ABORT / "qwen").read_text())
    except OSError:
        pass
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def wait_for_news(before, poll=20, longest=3600):
    """Sleep until a chat is imported or changed, an answer arrives, or parked updates can be judged."""
    waited = 0
    while waited < longest:
        time.sleep(poll)
        waited += poll
        if fingerprint() != before:
            return
        if reviewgate.waiting() and reviewgate.reviewer.ready():
            return


def main():
    if "--rebuild" in sys.argv:
        return rebuild()
    import jobs
    agent.log("sweep started")
    jobs.interrupted("qwen")
    while True:
        before = fingerprint()
        work, complete = one_pass()
        if "--once" in sys.argv:
            break
        if work == 0 and jobs.run_one("qwen"):   # the Assistant's jobs only when chats are read
            continue
        if work == 0:
            wait_for_news(before)  # idle: wakes within ~20s of an import, an answer, a job, or the reviewer coming back
        elif complete:
            agent.log("sweep: pass complete")


if __name__ == "__main__":
    main()
