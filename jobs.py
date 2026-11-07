"""Jobs the Assistant hands to the Reviewer and the Sweeper. Each job is a JSON file in <state>/jobs/, like chat
messages. The workers pick a job up only when their own work is done, and every result waits for you as an Ask card
(Approve / Reject). Nothing here writes to the vault; only an approved change to a file in a Git server set to Read and
write is pushed:
  doc_draft    (Reviewer) a new version of a file in a Git server (read and write only), quoting vault notes. The Ask
               card shows the diff; Approve and commit commits and pushes exactly that file.
  file_edit    (Assistant) the Assistant's own change to one file, made by propose_edit. The same card. On a read only
               server nothing is kept or asked: the Assistant shows the change in its reply.
  updated_file (Assistant) a note of type updated-file under files/updated: a file as it is now, with a diff against the saved
               original and links to the chat and where the change is recorded. Made by propose_updated_file, local to
               the vault (nothing goes to Git); approving writes it, or replaces the one it was asked to edit.
  note_update  (Reviewer) one exact change to a vault note, with a quote that supports it. Approving files it to the
               inbox, where the inbox agent turns it into a proposal as for any note.
  sort_note    (Sweeper)  reads a note or text and says which notes it belongs to, with quoted claims. Approving
               files it to the inbox, linked to those notes.
A failed job is marked failed and parks an Ask card; nothing retries by itself. "Try again" on the Failed list of the Queue page does."""
import difflib
import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

import agent
import gitrepos
import settings
from scrub import scrub

DIR = agent.STATE / "jobs"
WORKER = {"doc_draft": "gpt", "note_update": "gpt", "sort_note": "qwen"}   # what the Assistant may hand to the others
GIT_CHANGES = ("doc_draft", "file_edit")   # jobs whose result is a change to a file in a Git server
NICE = {"gpt": "Reviewer", "qwen": "Sweeper", "pa": "Assistant"}
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
    target, instructions = str(target or "").strip(), scrub(str(instructions or "").strip())[0]
    if not instructions:
        raise ValueError("say what the job should do")
    if kind == "doc_draft":
        e, _ = _git_target(target)
        if e["access"] != "write":
            raise ValueError(f"{e['name']} is set to Read only, so nothing could be pushed and nothing is queued: tell the person, "
                             f"who can set it to Read and write under Settings > Git servers")
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


def requeue(jid):
    j = get(jid)
    if not j or j["status"] != "failed":
        raise ValueError("only a failed job can be queued again")
    if j["kind"] == "file_edit":
        raise ValueError("an edit goes stale when the file changes: ask the Assistant to make it again")
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
    if ok and j["kind"] == "updated_file":
        j["result"]["written"] = _write_updated(j)
    if ok and j["kind"] in GIT_CHANGES and j["result"].get("access") == "write":
        try:
            j["result"]["commit"] = _commit(j)
        except (ValueError, OSError) as e:   # nothing was committed: say why, keep the draft
            j.update(status="failed", error=str(e)[:400], decided=datetime.now().isoformat(timespec="seconds"))
            _save(j)
            import questions
            if isinstance(e, gitrepos.Changed):   # the file moved on: offer to merge the draft's changes into it
                questions.ask(f"The approved change to {j['result']['path']} in {j['result']['server']} was not committed: the file "
                              "changed in the repository since the draft was written.",
                              why="Nothing changed in the repository. Merge applies the draft's changes to the file as it is now and "
                                  "asks you again before anything is committed. Skip leaves it as it is.",
                              kind="jobmerge", options=[{"label": "Merge", "value": "merge"}], payload={"job": j["id"]})
                agent.log(f"jobs: {j['id']} could not be committed, the file changed; merge offered")
                return j
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
def _git_target(target, model="gpt"):
    """git/<server>/<path> -> (entry, path inside). The model (the Reviewer for drafts, the Assistant for its own
    edits) needs its own token for that server."""
    parts = target.split("/", 2)
    if len(parts) < 3 or parts[0] != "git" or not parts[2].strip("/"):
        raise ValueError("a Git file looks like git/<Git server name>/<file path>")
    e = gitrepos.by_name(parts[1])
    if not e:
        names = ", ".join(x["name"] for x in gitrepos.entries()) or "none added yet"
        raise ValueError(f"there is no Git server called {parts[1]}. The names are: {names}. Use one exactly as written, as git/<name>/<file path>")
    if not gitrepos.has_token(e["id"], model):
        raise ValueError(f"the {gitrepos.MODELS[model]} has no token for {e['name']}; add one under Git servers on the Settings page")
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
    if e["access"] != "write":
        raise RuntimeError(f"{e['name']} is set to Read only, so nothing could be pushed; set it to Read and write under Settings > Git servers")
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
    j["result"] = {"summary": str(out.get("summary") or "")[:500], "diff": diff, "sources": ok,
                   "dropped": len(sources) - len(ok), "server": e["name"], "server_id": e["id"], "url": e["url"],
                   "branch": e["branch"], "path": inner, "access": e["access"], "base": gitrepos.content_sha(f),
                   "new_file": not f.is_file()}
    return (f"Commit a change to {inner} in {e['name']}? {j['result']['summary']}",
            f"Repository: {e['name']} ({e['url']}), branch {e['branch']}. File: {inner}"
            + (" (a new file)" if j["result"]["new_file"] else "") + f". {len(ok)} quote(s) checked against the notes. "
            "Approve commits exactly this diff and pushes it; nothing else changes.")


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
    user = (f"TEXT ({label}):\n{text}\n\nWHAT THE PERSON WANTS: {j['instructions']}\n\nCANDIDATES:\n"
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


def _card(j, question, why):
    """The Ask card for a finished job. A change to a Git file shows exactly which repository, branch and file, and the
    diff."""
    import questions
    r, payload = j["result"], {"job": j["id"]}
    if j["kind"] in GIT_CHANGES:
        payload.update(repo=r["server"], url=r["url"], branch=r["branch"], file=r["path"], summary=r["summary"],
                       diff=r["diff"], commit=r["access"] == "write")
    yes = "Approve and commit" if payload.get("commit") else "Approve"
    questions.ask(question, why=why, kind="job", payload=payload,
                  options=[{"label": yes, "value": "approve"}, {"label": "Reject", "value": "reject"}])


def propose_edit(target, old, new, why="", chat=""):
    """The Assistant's own edit of one file in a Git server: no model call and no Reviewer. old is exact text from the
    file (it must occur once) and new replaces it; a blank old adds new at the end of the file, or creates the file.
    The change waits as an Ask card with its diff and is committed only after you approve (read and write servers)."""
    target = str(target or "").strip()
    e, inner = _git_target(target, "pa")
    root = gitrepos.copy(e["id"], "pa")
    if root is None:
        raise ValueError(f"could not get a copy of {e['name']}")
    f = (root / inner).resolve()
    if not f.is_relative_to(root.resolve()) or ".git" in f.relative_to(root.resolve()).parts:
        raise ValueError("that path leaves the repository")
    current = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
    old, new = str(old or ""), str(new if new is not None else "")
    if old:
        n = current.count(old)
        if n != 1:
            raise ValueError(f"the text to replace is {'not in' if not n else 'in more than one place in'} {inner}: open the "
                             "file and copy the exact text (a longer piece, if it repeats), or leave old empty to add at the end")
        text = current.replace(old, new, 1)
    elif new.strip():
        text = current + ("" if not current or current.endswith("\n") else "\n") + new + ("" if new.endswith("\n") else "\n")
    else:
        raise ValueError("give the new text to add, or old and new to replace")
    diff = "".join(difflib.unified_diff(current.splitlines(True), text.splitlines(True), f"a/{inner}", f"b/{inner}"))
    if not diff:
        raise ValueError("that is the same as the file now: nothing to change")
    why = " ".join(str(why or "").split())[:300] or f"Edit {inner}"
    j = {"id": f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}", "kind": "file_edit", "worker": "pa", "target": target,
         "instructions": why, "chat": chat, "status": "waiting", "created": datetime.now().isoformat(timespec="seconds"),
         "error": "", "result": {"summary": why, "diff": diff, "sources": [], "dropped": 0, "server": e["name"],
                                 "server_id": e["id"], "url": e["url"], "branch": e["branch"], "path": inner, "access": e["access"],
                                 "base": gitrepos.content_sha(f), "new_file": not f.is_file()}}
    if e["access"] != "write":   # nothing could be pushed, so nothing is kept or asked: the Assistant shows the change in its reply
        return j
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / f"{j['id']}.draft").write_text(text, encoding="utf-8")
    _save(j)
    _card(j, f"Commit a change to {inner} in {e['name']}? {why}",
          f"Repository: {e['name']} ({e['url']}), branch {e['branch']}. File: {inner}"
          + (" (a new file)" if j["result"]["new_file"] else "") + ". Approve commits exactly this diff and pushes it; nothing else changes.")
    agent.log(f"jobs: the Assistant proposed an edit of {inner} in {e['name']}; waiting for you")
    return j


def propose_updated_file(name, what, contents, chat="", reference="", path="", asked_in=""):
    """The Assistant's own updated file, or its edit of one, kept in the vault only. Nothing is written until you approve."""
    import filestore
    name = filestore.good_name(name)
    if not name:
        raise ValueError("name is the file's own name with its extension, for example install.sh")
    what, contents = " ".join(str(what or "").split())[:400], str(contents or "")
    if not what or not contents.strip():
        raise ValueError("say in one sentence what changed, and give the whole file as it is now in contents")
    vault = Path(agent.VAULT)
    chat = str(chat or "").strip().removeprefix("vault/").removesuffix(".md")
    if chat and not (vault / f"{chat}.md").is_file():
        raise ValueError(f"there is no chat note {chat}; copy its path from a search result, or leave chat empty")
    path = str(path or "").strip().removeprefix("vault/")
    if path:
        if not path.startswith("files/updated/") or not path.endswith(".md") or ".." in path or not (vault / path).is_file():
            raise ValueError("path must be an existing updated file under files/updated/ (open it from a search first)")
    else:
        path = f"files/updated/{name}-{datetime.now():%Y%m%d}-{secrets.token_hex(2)}.md"
    originals = sorted((filestore.ORIG / name).glob("*")) if (filestore.ORIG / name).is_dir() else []
    diff = ""
    if originals:
        was = originals[-1].read_text(encoding="utf-8", errors="replace")
        diff = "".join(difflib.unified_diff(was.splitlines(True), contents.splitlines(True), f"a/{name} (saved original)", f"b/{name}"))
    ext, fence = Path(name).suffix.lstrip("."), "````"
    lines = [f"---\ntype: updated-file\ntitle: {json.dumps('Updated ' + name)}\nfilename: {json.dumps(name)}\nconfidence: medium\n"
             f"made_by: Assistant\nupdated: {datetime.now():%Y-%m-%d}\n---\n# Updated {name}\n\n{what}\n"]
    if chat:
        lines.append(f"- Chat: [[{chat}]]")
    if originals:
        lines.append(f"- Saved original: [[raw/files/{name}/{originals[-1].name}]]")
    if reference:
        lines.append(f"- Change reference: {' '.join(str(reference).split())[:300]}")
    lines.append("- Written by the Assistant and approved by you.")
    body = "\n".join(lines) + f"\n\n## The file as it is now\n{fence}{ext}\n{contents.rstrip(chr(10))}\n{fence}\n"
    if diff:
        body += f"\n## The change against the saved original (diff)\n{fence}diff\n{diff}\n{fence}\n"
    body = scrub(body)[0]
    j = {"id": f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}", "kind": "updated_file", "worker": "pa", "target": path,
         "instructions": what, "chat": asked_in, "status": "waiting", "created": datetime.now().isoformat(timespec="seconds"),
         "error": "", "result": {"path": path, "name": name, "editing": (vault / path).is_file(), "diff_lines": len(diff.splitlines())}}
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / f"{j['id']}.draft").write_text(body, encoding="utf-8")
    _save(j)
    import questions
    questions.ask(f"{'Replace' if j['result']['editing'] else 'Add'} the updated file for {name}? {what}",
                  why=f"Approving {'replaces' if j['result']['editing'] else 'writes'} {path} in your vault (nothing goes to Git). "
                      f"It holds {len(contents.splitlines())} lines of the file as it is now"
                      + (f" and a {j['result']['diff_lines']}-line diff against the saved original" if diff else ", with no saved original to compare")
                      + ". Start of the note:\n\n" + body[:1200],
                  kind="job", payload={"job": j["id"]}, options=[{"label": "Approve", "value": "approve"}, {"label": "Reject", "value": "reject"}])
    agent.log(f"jobs: the Assistant proposed an updated file for {name} ({path}); waiting for you")
    return j


# ---------- the Assistant's own changes to local vault notes ----------
def _vault_note(path, must_exist):
    rel = str(path or "").strip().removeprefix("vault/").strip("/")
    vault = Path(agent.VAULT).resolve()
    p = (vault / rel).resolve()
    if not rel.endswith(".md") or not p.is_relative_to(vault) or Path(rel).parts[0] in ("raw", ".git"):
        raise ValueError(f"{path} is not a place for a note: give a vault path ending in .md, like entities/foo.md (raw/ is for originals)")
    if must_exist and not p.is_file():
        raise ValueError(f"there is no note {rel} in the vault: open it from a search first, or use write_note to create it")
    return rel, p


def _put(p, text, who="assistant"):
    """Write a vault note. who is named first so the History page labels the change correctly."""
    import actors
    actors.claim(Path(p).resolve().relative_to(Path(agent.VAULT).resolve()).as_posix(), who)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def _did(kind, what, why, changes, chat):
    """Record a change the Assistant already made to the vault and ask you to keep it or undo it.
    changes: [(rel path, text before or None if it did not exist, text after or None if now gone)]."""
    diff = ""
    for rel, before, after in changes:
        diff += "".join(difflib.unified_diff((before or "").splitlines(True), (after or "").splitlines(True),
                                             f"a/{rel}" if before is not None else "/dev/null",
                                             f"b/{rel}" if after is not None else "/dev/null"))
    j = {"id": f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}", "kind": "note_done", "worker": "pa",
         "target": changes[-1][0], "instructions": why, "chat": chat, "status": "waiting",
         "created": datetime.now().isoformat(timespec="seconds"), "error": "",
         "result": {"what": what, "diff": diff, "changes": [{"path": r, "before": b, "after": a} for r, b, a in changes]}}
    _save(j)
    import questions
    questions.ask(f"The Assistant {what}. {why}".strip(), why="Already done in your vault only (nothing goes to Git). OK keeps it; "
                  "Undo puts the note back exactly as it was.", kind="notedone",
                  payload={"job": j["id"], "local": True, "diff": diff, "file": ", ".join(c[0] for c in changes)},
                  options=[{"label": "OK", "value": "ok"}, {"label": "Undo", "value": "undo"}])
    agent.log(f"jobs: the Assistant {what} (in your vault)")
    return j


def write_note(path, text, why="", chat=""):
    """Create a vault note, or replace one whole. Done at once."""
    rel, p = _vault_note(path, False)
    text = scrub(str(text or ""))[0]
    if not text.strip():
        raise ValueError("give the note's whole text")
    before = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
    if before == text:
        raise ValueError("that is the same as the note now: nothing to change")
    _put(p, text)
    return _did("write", f"{'replaced' if before is not None else 'created'} the note {rel}", " ".join(str(why or "").split())[:300],
                [(rel, before, text)], chat)


def edit_note(path, old, new, why="", chat=""):
    """Replace one piece of text in a vault note (it must occur once). Done at once."""
    rel, p = _vault_note(path, True)
    current = p.read_text(encoding="utf-8", errors="replace")
    old, new = str(old or ""), scrub(str(new if new is not None else ""))[0]
    n = current.count(old) if old else 0
    if n != 1:
        raise ValueError(f"the text to replace is {'not in' if not n else 'in more than one place in'} {rel}: open the note and copy "
                         "the exact text (a longer piece, if it repeats)")
    text = current.replace(old, new, 1)
    if text == current:
        raise ValueError("that is the same as the note now: nothing to change")
    _put(p, text)
    return _did("edit", f"changed the note {rel}", " ".join(str(why or "").split())[:300], [(rel, current, text)], chat)


def move_note(src, dest, why="", chat=""):
    """Move (file away) a vault note to another path. Done at once."""
    a, pa = _vault_note(src, True)
    b, pb = _vault_note(dest, False)
    if pb.exists():
        raise ValueError(f"{b} already exists: pick another path")
    text = pa.read_text(encoding="utf-8", errors="replace")
    _put(pb, text)
    import actors
    actors.claim(a, "assistant")
    pa.unlink()
    return _did("move", f"moved the note {a} to {b}", " ".join(str(why or "").split())[:300], [(a, text, None), (b, None, text)], chat)


def undo(jid):
    """Put back what a note_done job changed, if nothing touched those notes since."""
    j = get(jid)
    if not j or j["kind"] != "note_done" or j["status"] != "waiting":
        raise ValueError("there is nothing to undo")
    vault = Path(agent.VAULT).resolve()
    ch = j["result"]["changes"]
    for c in ch:
        p = vault / c["path"]
        now = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
        if now != c["after"]:
            raise ValueError(f"{c['path']} was changed again after this, so undoing would lose that: change it back by hand")
    import actors
    for c in reversed(ch):
        p = vault / c["path"]
        if c["before"] is None:
            actors.claim(c["path"], "you")
            p.unlink(missing_ok=True)
        else:
            _put(p, c["before"], "you")   # you pressed Undo
    j["status"] = "undone"
    j["decided"] = datetime.now().isoformat(timespec="seconds")
    _save(j)
    agent.log(f"jobs: undone by you: the Assistant {j['result']['what']}")
    return j


def keep(jid):
    j = get(jid)
    if j and j["kind"] == "note_done" and j["status"] == "waiting":
        j["status"] = "kept"
        j["decided"] = datetime.now().isoformat(timespec="seconds")
        _save(j)


def _hunks(diff):
    """The hunks of a unified diff: [{"start": line in the old file, "lines": [" x", "-x", "+x"]}]."""
    out, cur = [], None
    for line in diff.splitlines(True):
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", line)
        if m:
            cur = {"start": int(m.group(1)), "lines": []}
            out.append(cur)
        elif cur is not None and line[:1] in (" ", "+", "-"):
            cur["lines"].append(line)
    return out


def merge_patch(current, diff):
    """Apply the changes of a diff (made against an older version) to the file as it is now. Each hunk goes where its text is
    found, nearest to where it was; with less of the unchanged lines around it if they have changed. -> (text, applied, failed)."""
    lines, applied, failed, shift = current.splitlines(True), 0, 0, 0
    for h in _hunks(diff):
        body, done = h["lines"], False
        for trim in range(0, 4):   # all the unchanged lines around the change, then fewer
            a, b = 0, len(body)
            for _ in range(trim):
                if a < b and body[a][0] == " ":
                    a += 1
                if a < b and body[b - 1][0] == " ":
                    b -= 1
            part = body[a:b]
            old = [l[1:] for l in part if l[0] in " -"]
            new = [l[1:] for l in part if l[0] in " +"]
            if not old or all(l[0] == "+" for l in part):
                break
            at = [i for i in range(len(lines) - len(old) + 1) if lines[i:i + len(old)] == old]
            if trim and len(at) > 1:   # with little around it, the place must be clear
                continue
            if at:
                want = h["start"] - 1 + a + shift
                i = min(at, key=lambda k: abs(k - want))
                lines[i:i + len(old)] = new
                shift += len(new) - len(old)
                applied += 1
                done = True
                break
        if not done:
            failed += 1
    return "".join(lines), applied, failed


def merge(jid):
    """Your answer Merge on a change that could not be committed because the file moved on: put the draft's changes into the
    file as it is now. Nothing is committed: the result waits for you on a new Ask card. Raises ValueError with the reason."""
    j = get(jid)
    if not j or j["status"] != "failed" or j["kind"] not in GIT_CHANGES:
        raise ValueError("that change is not waiting to be merged")
    r = j["result"]
    if r.get("new_file"):
        raise ValueError("this draft makes a new file, and a file by that name is there now: ask the Assistant for a new draft")
    if len(r["diff"]) >= 60000:
        raise ValueError("the draft is too long to merge here: ask the Assistant for a new draft")
    model = next((m for m in gitrepos.COMMIT_ORDER if gitrepos.has_token(r["server_id"], m)), None)
    root = gitrepos.copy(r["server_id"], model, force=True) if model else None
    if root is None:
        raise ValueError(f"could not get the newest version of {r['server']}")
    f = (root / r["path"]).resolve()
    if not f.is_relative_to(root.resolve()) or not f.is_file():
        raise ValueError(f"{r['path']} is not in {r['server']} any more")
    current = f.read_text(encoding="utf-8", errors="replace")
    merged, ok, bad = merge_patch(current, r["diff"])
    if bad:
        raise ValueError(f"{bad} of the draft's {ok + bad} change(s) no longer fit {r['path']}, because that part of the file changed. "
                         "Nothing was changed. Ask the Assistant for a new draft.")
    diff = "".join(difflib.unified_diff(current.splitlines(True), merged.splitlines(True), f"a/{r['path']}", f"b/{r['path']}"))
    if not diff:
        raise ValueError(f"{r['path']} already contains the draft's changes: nothing to commit")
    (DIR / f"{jid}.draft").write_text(merged, encoding="utf-8")
    r.update(diff=diff, base=gitrepos.content_sha(f), merged=True)
    j.update(status="waiting", error="")
    _save(j)
    _card(j, f"Commit the merged change to {r['path']} in {r['server']}? {r.get('summary') or ''}".strip(),
          f"Repository: {r['server']} ({r['url']}), branch {r['branch']}. File: {r['path']}. The draft's {ok} change(s) were put into "
          "the file as it is now; the diff below is against that file. Approve commits exactly this diff and pushes it.")
    agent.log(f"jobs: {jid} merged into the current {r['path']}; waiting for you")
    return j


def _write_updated(j):
    draft = DIR / f"{j['id']}.draft"
    out = Path(agent.VAULT) / j["result"]["path"]
    import actors
    actors.claim(j["result"]["path"], "assistant")   # the Assistant's note, written once you approved it
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(draft.read_text(encoding="utf-8"), encoding="utf-8")
    tmp.replace(out)
    draft.unlink(missing_ok=True)
    return j["result"]["path"]


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
        _card(j, question, why)
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
    """For the Assistant's jobs tool."""
    return [{k: j.get(k) for k in ("id", "kind", "target", "status", "error", "created", "worker")}
            | {"result": {k: v for k, v in (j.get("result") or {}).items() if k != "diff"}} for j in all_jobs()[-50:]]
