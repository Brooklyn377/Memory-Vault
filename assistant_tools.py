"""The Assistant's tools: read-only look-ups over everything it may see. Results go to the Assistant's model, which may
run on another machine, so secrets never come out of here: files named like keys, tokens, passwords or env files are
refused, nothing under /etc/memory-agent is reachable, and every result goes through scrub.py.

Paths look like vault/entities/foo.md, state/review_log.jsonl, code/sweep.py or git/<server name>/README.md. A bare
note path such as entities/foo.md means the vault."""
import json
import re
import time
from pathlib import Path

import agent
import gitrepos
from scrub import scrub

VAULT, STATE = agent.VAULT, agent.STATE
CODE = Path(__file__).resolve().parent
SECRET_WORDS = {"key", "keys", "token", "tokens", "secret", "secrets", "password", "passwords", "passwd",
                "credential", "credentials", "env", "pem", "id_rsa", "id_ed25519"}


class _Secret:
    """A file or folder name that looks like it holds a secret: one of SECRET_WORDS as a whole word."""
    def search(self, name):
        name = str(name).lower()
        return name in SECRET_WORDS or bool(SECRET_WORDS & set(re.split(r"[^a-z0-9]+", name)))


SECRET = _Secret()
SKIP_DIRS = {".git", "venv", "__pycache__", "node_modules", "think"}   # think: model logs, searched only with where=logs
MAX_FILE = 2_000_000
SEARCH_SECONDS = 8


def _t(key):
    return __import__("tuning").get(key)


def roots():
    """Name -> folder for everything the Assistant may read. Git servers: only the ones it has a token for."""
    out = {"vault": VAULT, "state": STATE, "code": CODE}
    for e in gitrepos.readable("pa"):
        d = gitrepos.copy(e["id"], "pa")
        if d:
            out["git/" + e["name"]] = d
    return out


def _allowed(p, root):
    """True if p (resolved) is inside root, is not a secret, and is not another Git server's copy."""
    try:
        rel = p.relative_to(root.resolve())
    except ValueError:
        return False
    if any(SECRET.search(part) for part in rel.parts):
        return False
    if root == STATE and rel.parts[:1] == ("git",):
        return False   # Git copies are reached as git/<name>, and only with a token
    return True


def resolve(path):
    """-> (label, Path) for a path the Assistant typed, or a ValueError saying why not."""
    path = str(path or "").strip().lstrip("/")
    rs = roots()
    name = next((n for n in sorted(rs, key=len, reverse=True) if path == n or path.startswith(n + "/")), None)
    if name is None:
        name, path = "vault", "vault/" + path
    root = rs[name]
    p = (root / path[len(name):].lstrip("/")).resolve()
    if not _allowed(p, root):
        raise ValueError(f"{path} cannot be read")
    if not p.exists():
        raise ValueError(f"{path} does not exist")
    return f"{name}/{p.relative_to(root.resolve())}".rstrip("/."), p


def _files(root, under=None):
    base = (root / under) if under else root
    for p in sorted(base.rglob("*")):
        if p.is_file() and not (set(p.relative_to(base).parts[:-1]) & SKIP_DIRS) and _allowed(p.resolve(), root):
            yield p


def _where(where):
    """Search areas -> [(label prefix, root, subfolder)]."""
    rs, where = roots(), str(where or "all").strip()
    gits = [(n, r, None) for n, r in rs.items() if n.startswith("git/")]
    areas = {
        "notes": [("vault", VAULT, None)], "vault": [("vault", VAULT, None)],
        "raw": [("vault", VAULT, "raw")], "inbox": [("vault", VAULT, "inbox")],
        "state": [("state", STATE, None)], "code": [("code", CODE, None)],
        "logs": [("state", STATE, "think"), ("state", STATE, None)],
        "git": gits, "all": [("vault", VAULT, None)] + gits,
    }
    if where in areas:
        return areas[where]
    if "git/" + where in rs:
        return [("git/" + where, rs["git/" + where], None)]
    raise ValueError("where must be all, notes, raw, inbox, vault, state, code, logs, git, or a Git server's name")


def search(text, where="all"):
    terms = [t for t in dict.fromkeys(re.findall(r"[a-z0-9][a-z0-9._-]{1,}", str(text).lower()))][:10]
    if not terms:
        raise ValueError("give some words to search for")
    end, scored, stopped = time.time() + SEARCH_SECONDS, [], False
    for label, root, under in _where(where):
        if under and not (root / under).is_dir():
            continue
        for p in _files(root, under):
            if time.time() > end:
                stopped = True
                break
            if where == "notes" and p.relative_to(root).parts[:1] in (("raw",), ("inbox",)):
                continue
            try:
                if p.stat().st_size > MAX_FILE:
                    continue
                body = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = str(p.relative_to(root)).lower()
            low = body.lower()
            score = sum(3 for t in terms if t in rel) + sum(min(low.count(t), 5) for t in terms)
            if score:
                lines = [l.strip() for l in body.splitlines() if any(t in l.lower() for t in terms)][:2]
                scored.append((score, f"{label}/{p.relative_to(root)}", lines))
    scored.sort(key=lambda x: (-x[0], x[1]))
    if not scored:
        return "Nothing matched." + (" (stopped early: the search took too long)" if stopped else "")
    out = [f"{path}\n" + "\n".join("  " + l[:160] for l in lines) for _, path, lines in scored[:_t("assistant.search_results")]]
    return "\n".join(out) + ("\n(stopped early: the search took too long; narrow it with where)" if stopped else "")


def open_file(path, start=1):
    label, p = resolve(path)
    if p.is_dir():
        names = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir() if not SECRET.search(x.name))
        return f"{label}/ holds:\n" + "\n".join(names[:200])
    if p.stat().st_size > MAX_FILE * 5:
        raise ValueError(f"{label} is too big to open")
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, int(start or 1))
    body = "\n".join(f"{n}: {l}" for n, l in enumerate(lines[start - 1:], start))
    return f"{label}, lines {start} to {len(lines)} of {len(lines)}:\n{body}"


def links(path):
    from graph import Vault
    label, p = resolve(path)
    if not label.startswith("vault/"):
        raise ValueError("links work for vault notes")
    v, rel = Vault(VAULT), label[len("vault/"):]
    if rel not in v.body:
        raise ValueError(f"{label} is not a note")
    hood = v.neighborhood(rel, 2)
    return json.dumps({"links to": sorted(v.out[rel]), "linked from": sorted(v.back[rel]),
                       "within 2 links": list(hood["within_hops"])[:50]}, indent=1)


def tasks():
    import todo
    return "\n".join(f"- {t['title']} (from {t['source']})" for t in todo.list_tasks("open")) or "No open tasks."


def questions():
    import questions as q
    return "\n".join(f"- [{x['kind']}] {x['question']}" for x in q.list_questions("open")) or "No open Ask cards."


def queue():
    import queuestate
    return json.dumps(queuestate.snapshot(), default=str)[:20000]


def repos():
    out = [f"- {e['name']} ({'read and write' if e['access'] == 'write' else 'read only'}): {e['description'] or 'no description'}"
           for e in gitrepos.readable("pa")]
    return "\n".join(out) or "No Git servers the Assistant has a token for."


CHAT = {"id": ""}   # the chat message being answered, so a queued job records where it came from


def queue_job(kind, target, instructions):
    import jobs
    j = jobs.create(kind, target, instructions, CHAT["id"])
    return (f"Queued job {j['id']} ({kind}) for the {jobs.NICE[j['worker']]}. It runs when that model is idle, and "
            "the result waits for the person's approval as an Ask card. Tell them it is queued.")


def edit_file(path, old, new, why):
    """Propose one change to one file in a Git server. Nothing changes until the person approves it."""
    import jobs
    j = jobs.propose_edit(path, old, new, why, CHAT["id"])
    r = j["result"]
    return (f"Proposed the change to {r['path']} in {r['server']} ({r['branch']}). It is waiting for the person's approval on "
            "their Ask page, which shows the exact diff"
            + ("; approving commits and pushes it." if r["access"] == "write" else f"; {r['server']} is read only, so approving only marks the draft approved.")
            + " Tell them it is waiting there.")


def job_list():
    import jobs
    rows = jobs.summary()
    return "\n".join(f"- {j['id']} {j['kind']} {j['target'] or ''}: {j['status']}{' (' + j['error'] + ')' if j['error'] else ''}"
                     for j in rows) or "No jobs yet."


TOOLS = {"search": lambda a: search(a.get("text", ""), a.get("where", "all")),
         "open": lambda a: open_file(a.get("path", ""), a.get("start", 1)),
         "links": lambda a: links(a.get("path", "")),
         "tasks": lambda a: tasks(), "questions": lambda a: questions(), "queue": lambda a: queue(),
         "repos": lambda a: repos(), "jobs": lambda a: job_list(),
         "queue_job": lambda a: queue_job(a.get("kind"), a.get("target"), a.get("instructions")),
         "edit_file": lambda a: edit_file(a.get("path") or a.get("target"), a.get("old"), a.get("new"), a.get("why"))}


def run(name, args):
    """Run one tool. Always returns text for the model: a result, or what went wrong. Never a secret."""
    fn = TOOLS.get(str(name))
    if fn is None:
        return f"There is no tool called {name}. Tools: " + ", ".join(TOOLS)
    try:
        text = fn(args if isinstance(args, dict) else {})
    except (ValueError, OSError) as e:
        text = f"{name} failed: {e}"
    text = scrub(str(text))[0]
    lim = _t("assistant.tool_chars")
    return text if len(text) <= lim else text[:lim] + f"\n(cut at {lim:,} characters; open a later line to read on)"
