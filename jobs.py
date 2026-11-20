"""Changes the Assistant made or proposed. Each is a JSON file in <state>/jobs/, like chat messages, and waits for you
as an Ask card:
  file_edit    one file from the Assistant's sandbox (sandbox.py), new, changed or deleted, made by propose_file. The
               Ask card shows the diff; Approve and commit commits and pushes exactly that file, only on a Git server
               set to Read and write. On a read only server nothing is kept or asked: the Assistant shows the change
               in its reply.
  note_done    a change the Assistant already made to a note in your vault (write_note, edit_note, move_note). The
               card shows the diff, with OK (keep) and Undo.
A change that could not be committed is marked failed and parks an Ask card; nothing retries by itself.

The Assistant used to hand work to the Reviewer and the Sweeper as jobs (doc_draft, note_update, sort_note) and to
propose updated-file notes (updated_file). That was removed; retire() marks what is left of those as skipped."""
import difflib
import json
import os
import secrets
from datetime import datetime
from pathlib import Path

import agent
import gitrepos
from scrub import scrub

DIR = agent.STATE / "jobs"
GIT_CHANGES = ("file_edit",)   # jobs whose result is a change to a file in a Git server
RETIRED = ("doc_draft", "note_update", "sort_note", "updated_file")   # kinds that are no longer made or run
RETIRED_WHY = "Not run: handing work to the Reviewer and Sweeper was removed. Ask the Assistant to do it itself."
NICE = {"gpt": "Reviewer", "qwen": "Sweeper", "pa": "Assistant"}
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


def requeue(jid):
    """Try again on the Failed list: a change that could not be committed went stale, so it is never repeated as it is."""
    j = get(jid)
    if not j or j["status"] != "failed":
        raise ValueError("only a failed job can be tried again")
    raise ValueError("A failed change cannot be tried again as it is: ask the Assistant to make the change again.")


def retire():
    """At chat worker start: whatever is left of the removed kinds of job (queued, cut off, failed, or waiting for an
    answer) is marked skipped, with the reason. Nothing is deleted."""
    for j in all_jobs():
        if j.get("kind") in RETIRED and j.get("status") in ("queued", "running", "failed", "waiting"):
            was = j["status"]
            j.update(status="skipped", error=RETIRED_WHY)
            _save(j)
            agent.log(f"jobs: {j['kind']} {j['id']} (was {was}) marked skipped: handing work off was removed")


def drop(jid):
    """Skip a failed job: it stays listed as skipped and is not run."""
    j = get(jid)
    if not j or j["status"] != "failed":
        raise ValueError("only a failed job can be skipped")
    j["status"] = "skipped"
    _save(j)


def decide(jid, ok):
    """Your answer on a proposed change's Ask card. Approve commits it, on a Git server set to Read and write."""
    j = get(jid)
    if not j or j["status"] != "waiting":
        raise ValueError("that job is not waiting for you")
    if ok and j["kind"] in GIT_CHANGES and j["result"].get("access") == "write":
        try:
            j["result"]["commit"] = _commit(j)
        except (ValueError, OSError) as e:   # nothing was committed: say why, keep the draft
            j.update(status="failed", error=str(e)[:400], decided=datetime.now().isoformat(timespec="seconds"))
            _save(j)
            import questions
            if isinstance(e, gitrepos.Changed):   # the file moved on: nothing is merged, a new draft is needed
                questions.ask(f"The approved change to {j['result']['path']} in {j['result']['server']} was not committed: the file "
                              "changed in the repository since the draft was written.",
                              why="Nothing changed in the repository. Ask for a new draft.", kind="assistant",
                              options=[{"label": "OK", "value": "ok"}], payload={"job": j["id"]})
                agent.log(f"jobs: {j['id']} could not be committed, the file changed since the draft")
                return j
            questions.ask(f"The approved change to {j['result']['path']} in {j['result']['server']} was not committed: "
                          f"{str(e)[:250]}", why="Nothing changed in the repository. Ask the Assistant to make the change "
                          "again.", kind="assistant",
                          options=[{"label": "OK", "value": "ok"}], payload={"job": j["id"]})
            agent.log(f"jobs: commit of {j['id']} failed: {e}")
            return j
    j["status"] = ("committed" if j["result"].get("commit") else "approved") if ok else "rejected"
    j["decided"] = datetime.now().isoformat(timespec="seconds")
    _save(j)
    agent.log(f"jobs: {j['kind']} {j['id']} {j['status']} by you")
    return j


def _commit(j):
    """An approved change to a read-and-write Git server: commit and push exactly that file. -> the commit id."""
    r = j["result"]
    text = None if r.get("delete") else (DIR / f"{j['id']}.draft").read_bytes().decode("utf-8")
    msg = (f"{r.get('summary') or 'Update ' + r['path']}\n\nRequested through the memory agent's Assistant and "
           f"approved by you on its Ask page (job {j['id']}).")
    return gitrepos.commit_file(r["server_id"], r["path"], text, r["base"], msg, r.get("executable"))


def _card(j, question, why):
    """The Ask card for a proposed change to a Git file: exactly which repository, branch and file, and the diff."""
    import questions
    r, payload = j["result"], {"job": j["id"]}
    if j["kind"] in GIT_CHANGES:
        payload.update(repo=r["server"], url=r["url"], branch=r["branch"], file=r["path"], summary=r["summary"],
                       diff=r["diff"], commit=r["access"] == "write")
    yes = "Approve and commit" if payload.get("commit") else "Approve"
    questions.ask(question, why=why, kind="job", payload=payload,
                  options=[{"label": yes, "value": "approve"}, {"label": "Reject", "value": "reject"}])


class Pending(ValueError):
    """A change to that file is already waiting for you: the Assistant is told so in these words, and nothing is proposed."""
    plain = True


def pending_change(server_id, path):
    """The job, if any, whose change to this file in this Git server is waiting on your Ask page (from any chat). A file has
    one waiting change at a time: two changes to one file cannot both be committed."""
    for j in all_jobs():
        r = j.get("result") or {}
        if j.get("status") == "waiting" and j.get("kind") in GIT_CHANGES and r.get("server_id") == server_id and r.get("path") == path:
            return j
    return None


def propose_file(e, inner, text, why="", chat="", executable=None):
    """One file from the Assistant's sandbox, as it should be in the repository of the Git server e: its whole new text,
    or None to delete it. No model call and no Reviewer. The change waits as an Ask card with its diff and is committed
    only after you approve (read and write servers). While a change to that file is waiting, another one is refused
    (Pending). On a read only server nothing is kept or asked: the job is returned so the Assistant can show the diff."""
    root = gitrepos.copy(e["id"], "pa")
    if root is None:
        raise ValueError(f"could not get a copy of {e['name']}")
    f = (root / inner).resolve()
    if not f.is_relative_to(root.resolve()) or ".git" in f.relative_to(root.resolve()).parts:
        raise ValueError("that path leaves the repository")
    if e["access"] == "write" and pending_change(e["id"], inner):
        raise Pending(f"There is already a pending change to {inner} in {e['name']}. You must wait for the pending edit to be "
                      "approved or denied before you can edit that file again. Nothing was proposed.")
    current = f.read_bytes().decode("utf-8", errors="replace") if f.is_file() else ""
    if text is None and not f.is_file():
        raise ValueError(f"{inner} is not in the repository, so there is nothing to delete")
    diff = "".join(difflib.unified_diff(current.splitlines(True), (text or "").splitlines(True),
                                        f"a/{inner}" if f.is_file() else "/dev/null", f"b/{inner}" if text is not None else "/dev/null"))
    if text is not None and f.is_file() and text == current:
        raise ValueError("that is the same as the file now: nothing to change")
    why = " ".join(str(why or "").split())[:300] or f"Edit {inner}"
    target = f"git/{e['name']}/{inner}"
    j = {"id": f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}", "kind": "file_edit", "worker": "pa", "target": target,
         "instructions": why, "chat": chat, "status": "waiting", "created": datetime.now().isoformat(timespec="seconds"),
         "error": "", "result": {"summary": why, "diff": diff, "sources": [], "dropped": 0, "server": e["name"],
                                 "server_id": e["id"], "url": e["url"], "branch": e["branch"], "path": inner, "access": e["access"],
                                 "base": gitrepos.content_sha(f), "new_file": not f.is_file(), "delete": text is None,
                                 "executable": executable}}
    if e["access"] != "write":   # nothing could be pushed, so nothing is kept or asked: the Assistant shows the change in its reply
        return j
    DIR.mkdir(parents=True, exist_ok=True)
    if text is not None:
        (DIR / f"{j['id']}.draft").write_bytes(text.encode("utf-8"))
    _save(j)
    place = f"Repository: {e['name']} ({e['url']}), branch {e['branch']}."
    if text is None:
        _card(j, f"Delete {inner} from {e['name']}? {why}", f"{place} Approve deletes exactly this file and pushes that; nothing else changes.")
    else:
        _card(j, f"Commit a change to {inner} in {e['name']}? {why}",
              f"{place} File: {inner}" + (" (a new file)" if j["result"]["new_file"] else "")
              + ". Approve commits exactly this diff and pushes it; nothing else changes.")
    agent.log(f"jobs: the Assistant proposed {'deleting' if text is None else 'a change to'} {inner} in {e['name']}; waiting for you")
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
    """Create a new vault note. Done at once. It never replaces a note that exists: those change only with edit_note."""
    rel, p = _vault_note(path, False)
    if p.exists():
        import tuning
        raise ValueError(f"{rel} already exists, and write_note only creates new notes. Change it with edit_note, a piece of "
                         f"at most {tuning.get('chat.call_chars')} characters at a time")
    text = scrub(str(text or ""))[0]
    if not text.strip():
        raise ValueError("give the start of the note as text, in text")
    _put(p, text)
    return _did("write", f"created the note {rel}", " ".join(str(why or "").split())[:300], [(rel, None, text)], chat)


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


def summary():
    """For the Assistant's jobs tool."""
    return [{k: j.get(k) for k in ("id", "kind", "target", "status", "error", "created", "worker")}
            | {"result": {k: v for k, v in (j.get("result") or {}).items() if k != "diff"}} for j in all_jobs()[-50:]]
