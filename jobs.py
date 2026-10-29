"""Jobs the Assistant hands to the Reviewer and the Sweeper. Each job is a JSON file in <state>/jobs/, like chat
messages. The workers pick a job up only when their own work is done, and every result waits for you as an Ask card
(Approve / Reject). Nothing here writes to the vault or pushes to Git:
  doc_draft    (Reviewer) a new version of a file in a Git server, quoting vault notes. Approving marks it approved;
               you apply it yourself (shown on the Queue page). Nothing is pushed.
  note_update  (Reviewer) one exact change to a vault note, with a quote that supports it. Approving files it to the
               inbox, where the inbox agent turns it into a proposal as for any note.
  sort_note    (Sweeper)  reads a note or text and says which notes it belongs to, with quoted claims. Approving
               files it to the inbox, linked to those notes.
A failed job is marked failed and parks an Ask card; nothing retries by itself. "Queue again" on the Queue page does."""
import difflib
import json
import os
import secrets
import time
from datetime import datetime
from pathlib import Path

import agent
import gitrepos
import settings
from scrub import scrub

DIR = agent.STATE / "jobs"
WORKER = {"doc_draft": "gpt", "note_update": "gpt", "sort_note": "qwen"}
NICE = {"gpt": "Reviewer", "qwen": "Sweeper"}
ENDPOINT = {"gpt": "http://127.0.0.1:8081/v1", "qwen": "http://127.0.0.1:8080/v1"}

DRAFT_SYS = (
    "You update one documentation file from a person's notes vault. You get the CURRENT FILE (it may be empty for a "
    "new file), what the person wants, and NOTES from the vault. Write the whole new file. Change only what the notes "
    "support; keep everything else as it is. Reply with ONLY JSON: "
    '{"text":"<the whole new file>","summary":"<one or two sentences on what changed>",'
    '"sources":[{"note":"<note path as given>","quote":"<exact words copied from that note>"}]}. '
    "Every changed fact needs a source quote copied character for character. Never invent.")
NOTE_SYS = (
    "You propose ONE change to a note in a person's notes vault. You get the NOTE, what the person wants, and other "
    "NOTES. Reply with ONLY JSON: "
    '{"old":"<exact text copied from the NOTE>","new":"<replacement text>","why":"<one sentence>",'
    '"source":{"note":"<note path as given>","quote":"<exact words copied from that note>"}}. '
    "'old' must be copied character for character from the NOTE, and the quote from the note you name. Never invent. "
    'If the notes do not support a change, reply {"old":"","new":"","why":"<why not>"}.')
SORT_SYS = (
    "You sort one piece of text into a person's notes vault. You get the TEXT, what the person wants, and CANDIDATE "
    "notes it may belong to. Reply with ONLY JSON: "
    '{"title":"<short title>","belongs_to":["<candidate path>"],'
    '"claims":[{"note":"<candidate path>","statement":"<one sentence>","quote":"<exact words copied from the TEXT>"}]}. '
    "Use only candidates listed. Every claim needs a quote copied character for character from the TEXT. Never invent.")


# ---------- storage ----------
def _path(jid):
    return DIR / f"{jid}.json"


def _save(j):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(j["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(j, indent=1, ensure_ascii=False))
    os.replace(tmp, _path(j["id"]))


def get(jid):
    p = _path(str(jid))
    return json.loads(p.read_text()) if p.exists() else None


def all_jobs():
    if not DIR.exists():
        return []
    return sorted((json.loads(p.read_text()) for p in DIR.glob("*.json")), key=lambda j: j["id"])


def queued(model):
    return [j for j in all_jobs() if j["status"] == "queued" and j["worker"] == model]


def create(kind, target, instructions, chat=""):
    """Queue a job (the Assistant's queue_job tool). Checks the target now, so a bad one is refused in the chat."""
    if kind not in WORKER:
        raise ValueError("kind must be doc_draft, note_update or sort_note")
    target, instructions = str(target or "").strip(), scrub(str(instructions or "").strip())[0][:4000]
    if not instructions:
        raise ValueError("say what the job should do")
    if kind == "doc_draft":
        _git_target(target)
    elif kind == "note_update":
        _note(target)
    elif target:
        _read_any(target)
    j = {"id": f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}", "kind": kind, "worker": WORKER[kind],
         "target": target, "instructions": instructions, "chat": chat, "status": "queued",
         "created": datetime.now().isoformat(timespec="seconds"), "result": {}, "error": ""}
    _save(j)
    agent.log(f"jobs: queued {kind} for the {NICE[j['worker']]}: {target or instructions[:60]}")
    return j


def cancel(jid):
    j = get(jid)
    if not j or j["status"] != "queued":
        raise ValueError("only a queued job can be cancelled")
    j["status"] = "cancelled"
    _save(j)


def requeue(jid):
    j = get(jid)
    if not j or j["status"] != "failed":
        raise ValueError("only a failed job can be queued again")
    j.update(status="queued", error="", result={})
    _save(j)


def drop(jid):
    """Skip a failed job: it stays listed as skipped and is not run."""
    j = get(jid)
    if not j or j["status"] != "failed":
        raise ValueError("only a failed job can be skipped")
    j["status"] = "skipped"
    _save(j)


def decide(jid, ok):
    """Your answer on a finished job's Ask card. Approve files note jobs to the inbox; a draft is only marked."""
    j = get(jid)
    if not j or j["status"] != "waiting":
        raise ValueError("that job is not waiting for you")
    if ok and j["kind"] in ("note_update", "sort_note"):
        j["result"]["filed"] = _file_to_inbox(j)
    if ok and j["kind"] == "doc_draft" and j["result"].get("access") == "write":
        try:
            j["result"]["commit"] = _commit(j)
        except (ValueError, OSError) as e:   # nothing was committed: say why, keep the draft
            j.update(status="failed", error=str(e)[:400], decided=datetime.now().isoformat(timespec="seconds"))
            _save(j)
            import questions
            questions.ask(f"The approved change to {j['result']['path']} in {j['result']['server']} was not committed: "
                          f"{str(e)[:250]}", why="Nothing changed in the repository. Queue it again from the Queue page "
                          "for a new draft, or ask the Assistant again.", kind="assistant",
                          options=[{"label": "OK", "value": "ok"}], payload={"job": j["id"]})
            agent.log(f"jobs: commit of {j['id']} failed: {e}")
            return j
    j["status"] = ("committed" if j["result"].get("commit") else "approved") if ok else "rejected"
    j["decided"] = datetime.now().isoformat(timespec="seconds")
    _save(j)
    agent.log(f"jobs: {j['kind']} {j['id']} {j['status']} by you")
    return j


# ---------- reading ----------
def _git_target(target):
    """git/<server>/<path> -> (entry, path inside). The Reviewer needs its own token for that server."""
    import gitrepos
    parts = target.split("/", 2)
    if len(parts) < 3 or parts[0] != "git" or not parts[2].strip("/"):
        raise ValueError("a doc_draft target looks like git/<Git server name>/<file path>")
    e = gitrepos.by_name(parts[1])
    if not e:
        raise ValueError(f"there is no Git server called {parts[1]}")
    if not gitrepos.has_token(e["id"], "gpt"):
        raise ValueError(f"the Reviewer has no token for {e['name']}; add one under Git servers on the Settings page")
    if ".." in Path(parts[2]).parts:
        raise ValueError("that path leaves the repository")
    return e, parts[2].strip("/")


def _note(target):
    rel = str(target).removeprefix("vault/").strip("/")
    p = (agent.VAULT / rel).resolve()
    if not p.is_relative_to(Path(agent.VAULT).resolve()) or p.suffix != ".md" or not p.is_file():
        raise ValueError(f"{target} is not a note in the vault")
    return rel, p


def _read_any(target):
    import assistant_tools
    label, p = assistant_tools.resolve(target)
    if not p.is_file():
        raise ValueError(f"{target} is not a file")
    return label, p.read_text(encoding="utf-8", errors="replace")


def _notes_for(text, n=None):
    """Vault notes that match the words of a job, as (path, body). Same search as the chat look-up."""
    import ui_chat
    from graph import Vault
    v = Vault(agent.VAULT)
    top, extra = ui_chat.retrieve(text)
    return [(r, v.body[r]) for r in (top + extra)[:n or 8]]


def _call(model, system, user):
    s = settings.get(model)
    body = {"model": s["model_file"], "temperature": s["temperature"],
            "max_tokens": s["max_tokens"]["review" if model == "gpt" else "ask"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if model == "gpt":
        body["chat_template_kwargs"] = {"reasoning_effort": s["reasoning_effort"]}
    else:
        body["chat_template_kwargs"] = {"enable_thinking": s["thinking"]}
    import thinklog
    out = agent.parse_json(thinklog.chat(model, ENDPOINT[model], body, "job", slot=0).get("content") or "")
    if not isinstance(out, dict):
        raise RuntimeError(f"the {NICE[model]} gave no usable answer")
    return out


def _grounded(quote, text):
    import reviewer
    return reviewer.grounded(quote, text)


# ---------- the work ----------
def _doc_draft(j):
    import gitrepos
    e, inner = _git_target(j["target"])
    root = gitrepos.copy(e["id"], "gpt")
    if root is None:
        raise RuntimeError(f"could not get a copy of {e['name']}")
    f = (root / inner).resolve()
    if not f.is_relative_to(root.resolve()):
        raise RuntimeError("that path leaves the repository")
    current = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
    notes = _notes_for(j["instructions"] + " " + inner)
    lim = __import__("tuning").get("chat.note_chars")
    user = (f"CURRENT FILE ({j['target']}):\n{current or '(new file)'}\n\nWHAT THE PERSON WANTS: {j['instructions']}\n\n"
            + "\n\n".join(f"NOTE {r}:\n{b[:lim]}" for r, b in notes))
    out = _call("gpt", DRAFT_SYS, user)
    text = str(out.get("text") or "")
    if not text.strip():
        raise RuntimeError("the draft came back empty")
    by = dict(notes)
    sources = [s for s in out.get("sources") or [] if isinstance(s, dict)]
    ok = [s for s in sources if s.get("note") in by and _grounded(s.get("quote"), by[s["note"]])]
    if text.strip() != current.strip() and not ok:
        raise RuntimeError("no quote from the notes supports the draft")
    diff = "".join(difflib.unified_diff(current.splitlines(True), text.splitlines(True), f"a/{inner}", f"b/{inner}"))
    if not diff:
        raise RuntimeError("the draft is the same as the file: nothing to change")
    (DIR / f"{j['id']}.draft").write_text(text, encoding="utf-8")
    j["result"] = {"summary": str(out.get("summary") or "")[:500], "diff": diff[:60000], "sources": ok,
                   "dropped": len(sources) - len(ok), "server": e["name"], "server_id": e["id"], "url": e["url"],
                   "branch": e["branch"], "path": inner, "access": e["access"], "base": gitrepos.content_sha(f),
                   "new_file": not f.is_file()}
    commit = e["access"] == "write"
    return (f"{'Commit' if commit else 'Draft'} a change to {inner} in {e['name']}? {j['result']['summary']}",
            f"Repository: {e['name']} ({e['url']}), branch {e['branch']}. File: {inner}"
            + (" (a new file)" if j["result"]["new_file"] else "") + f". {len(ok)} quote(s) checked against the notes. "
            + ("Approve commits exactly this diff and pushes it; nothing else changes." if commit else
               f"{e['name']} is read only, so Approve only marks the draft approved; it is on the Queue page to apply."))


def _commit(j):
    """An approved draft to a read-and-write Git server: commit and push exactly that file. -> the commit id."""
    r = j["result"]
    text = (DIR / f"{j['id']}.draft").read_text(encoding="utf-8")
    msg = (f"{r.get('summary') or 'Update ' + r['path']}\n\nRequested through the memory agent's Assistant and "
           f"approved by you on its Ask page (job {j['id']}).")
    return gitrepos.commit_file(r["server_id"], r["path"], text, r["base"], msg)


def _note_update(j):
    rel, p = _note(j["target"])
    import reviewer
    note = reviewer.body_of(p)
    notes = [(r, b) for r, b in _notes_for(j["instructions"] + " " + rel) if r != rel]
    lim = __import__("tuning").get("chat.note_chars")
    user = (f"NOTE ({rel}):\n{note}\n\nWHAT THE PERSON WANTS: {j['instructions']}\n\n"
            + "\n\n".join(f"NOTES {r}:\n{b[:lim]}" for r, b in notes))
    out = _call("gpt", NOTE_SYS, user)
    if not str(out.get("old") or "").strip():
        raise RuntimeError("no change proposed: " + str(out.get("why") or "the notes do not support one")[:200])
    if not agent.verify(out, note):
        raise RuntimeError("the text to replace is not in the note exactly once")
    src = out.get("source") or {}
    by = dict(notes + [(rel, note)])
    if not (isinstance(src, dict) and src.get("note") in by and _grounded(src.get("quote"), by[src["note"]])):
        raise RuntimeError("its quote is not in the note it names")
    j["result"] = {"note": rel, "old": str(out["old"]), "new": str(out.get("new") or ""), "why": str(out.get("why") or "")[:300],
                   "source": {"note": src["note"], "quote": str(src["quote"])[:300]}}
    return (f'Change "{j["result"]["old"][:80]}" to "{j["result"]["new"][:80]}" in {rel}?',
            f'{j["result"]["why"]} Quote from {src["note"]}: "{j["result"]["source"]["quote"][:150]}". '
            "Approving files it to the inbox; the inbox agent then makes a proposal as for any note.")


def _sort_note(j):
    label, text = _read_any(j["target"]) if j["target"] else ("the Assistant's text", j["instructions"])
    import agent as a
    from graph import Vault
    v = Vault(a.VAULT)
    cands = [r for r, _ in _notes_for(text[:2000] + " " + j["instructions"], 12) if r.startswith("entities/")]
    user = (f"TEXT ({label}):\n{text[:12000]}\n\nWHAT THE PERSON WANTS: {j['instructions']}\n\nCANDIDATES:\n"
            + "\n".join(f"- {r}: {v.title(r)}" for r in cands))
    out = _call("qwen", SORT_SYS, user)
    claims = [c for c in out.get("claims") or [] if isinstance(c, dict) and c.get("note") in cands
              and _grounded(c.get("quote"), text)]
    if not claims:
        raise RuntimeError("no claim with a quote from the text")
    j["result"] = {"title": str(out.get("title") or label)[:120], "source": label, "claims": claims[:20],
                   "belongs_to": sorted({c["note"] for c in claims})}
    return (f"Sort {label} into {', '.join(j['result']['belongs_to'])}?",
            f"{len(claims)} claim(s), each with a quote from the text. Approving files it to the inbox, linked to "
            "those notes; the inbox agent then makes proposals as for any note.")


def _file_to_inbox(j):
    """An approved note job becomes an inbox note: the same path your /fix corrections take."""
    import yaml
    r, now = j["result"], datetime.now()
    if j["kind"] == "note_update":
        body = (f"Change requested through the Assistant and approved by you, for [[{r['note'][:-3]}]].\n\n"
                f"Replace: {r['old']}\nWith: {r['new']}\nWhy: {r['why']}\n"
                f"Source: [[{r['source']['note'][:-3]}]], \"{r['source']['quote']}\"\n")
    else:
        body = (f"{r['title']}: sorted by the Sweeper from {r['source']} and approved by you.\n\n"
                + "\n".join(f"- [[{c['note'][:-3]}]]: {c['statement']} (\"{c['quote']}\")" for c in r["claims"]) + "\n")
    fm = {"type": "assistant-job", "confidence": "low", "created": f"{now:%Y-%m-%d}", "job_id": j["id"]}
    inbox = agent.VAULT / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    name = f"{now:%Y-%m-%d-%H%M%S}-job-{j['id'][-4:]}.md"
    (inbox / name).write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + scrub(body)[0], encoding="utf-8")
    return f"inbox/{name}"


RUN = {"doc_draft": _doc_draft, "note_update": _note_update, "sort_note": _sort_note}


def run_one(model):
    """Do the oldest queued job for this model. Returns True if there was one. Called by the workers when idle."""
    todo = queued(model)
    if not todo:
        return False
    import questions
    j = todo[0]
    j.update(status="running", started=datetime.now().isoformat(timespec="seconds"))
    _save(j)
    try:
        question, why = RUN[j["kind"]](j)
        j["status"] = "waiting"
        _save(j)
        r, payload = j["result"], {"job": j["id"]}
        if j["kind"] == "doc_draft":   # the card shows exactly what would change, where
            payload.update(repo=r["server"], url=r["url"], branch=r["branch"], file=r["path"], summary=r["summary"],
                           diff=r["diff"], commit=r["access"] == "write")
        yes = "Approve and commit" if payload.get("commit") else "Approve"
        questions.ask(question, why=why, kind="job", payload=payload,
                      options=[{"label": yes, "value": "approve"}, {"label": "Reject", "value": "reject"}])
        agent.log(f"jobs: {j['kind']} {j['id']} done by the {NICE[model]}, waiting for you")
    except Exception as e:
        import thinklog
        if isinstance(e, thinklog.Aborted):   # its server was switched or stopped: back in the queue, not a failure
            j.update(status="queued")
            _save(j)
            agent.log(f"jobs: {j['kind']} {j['id']} cut off by a server switch or stop; queued again")
            return True
        j.update(status="failed", error=f"{type(e).__name__}: {str(e)[:300]}")
        _save(j)
        questions.ask(f"A job from the Assistant failed: {j['kind']} {j['target'] or ''}: {str(e)[:200]}",
                      why="Nothing was changed. Queue it again from the Queue page, or ask the Assistant differently.",
                      kind="assistant", options=[{"label": "OK", "value": "ok"}], payload={"job": j["id"], "error": j["error"]})
        agent.log(f"jobs: {j['kind']} {j['id']} failed: {j['error']}")
    return True


def interrupted(model):
    """At worker start: this worker's job left running was cut off. It is marked failed, never restarted by itself."""
    for j in all_jobs():
        if j["status"] == "running" and j["worker"] == model:
            j.update(status="failed", error="interrupted by a restart; queue it again from the Queue page")
            _save(j)


def summary():
    """For the Assistant's jobs tool and the Queue page."""
    return [{k: j.get(k) for k in ("id", "kind", "target", "status", "error", "created", "worker")}
            | {"result": {k: v for k, v in (j.get("result") or {}).items() if k != "diff"}} for j in all_jobs()[-50:]]
