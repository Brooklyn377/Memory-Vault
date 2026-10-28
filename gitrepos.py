"""Git servers you add on the Settings page: a name, what it is for, the repository address, read only or read and
write, and a token per model. Nothing is listed by default.

The models never run Git. This module keeps a read-only local copy of each entry for the models that have a token for
it, and pulls it when a search or job needs it (at most every PULL_EVERY seconds). A failed pull parks an Ask card and
keeps the last copy; nothing retries by itself.

Tokens are stored root-only in /etc/memory-agent/git/, never in settings, never in a URL or a repository's config, and
never in a model call: each git command gets the token as a one-off HTTP header. Nothing here pushes: an entry marked
read and write only records that you allow it, for a later piece that will still ask you first."""
import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path

import agent

FILE = agent.STATE / "git_servers.json"
COPIES = agent.STATE / "git"
TOKENS = Path("/etc/memory-agent/git")
MODELS = {"qwen": "Sweeper", "gpt": "Reviewer", "cur": "Curator", "pa": "Assistant"}
ACCESS = ("read", "write")
PULL_EVERY = 300
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.\-]{0,39}")
BRANCH = re.compile(r"[A-Za-z0-9][\w./\-]{0,99}")


def _load():
    try:
        d = json.loads(FILE.read_text())
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _store(items):
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1))
    os.replace(tmp, FILE)


def entries():
    return _load()


def get(eid):
    return next((e for e in _load() if e["id"] == eid), None)


def by_name(name):
    return next((e for e in _load() if e["name"].lower() == str(name).strip().lower()), None)


def _token_file(eid, model):
    return TOKENS / f"{eid}-{model}.token"


def token(eid, model):
    try:
        return _token_file(eid, model).read_text().strip()
    except OSError:
        return ""


def has_token(eid, model):
    return bool(token(eid, model))


def readable(model):
    """The entries this model may read: the ones it has a token for."""
    return [e for e in _load() if has_token(e["id"], model)]


def public(e):
    """An entry as the page and the models see it: never a token, only which models have one."""
    return {k: e[k] for k in ("id", "name", "description", "url", "branch", "access")} | {
        "tokens": {m: has_token(e["id"], m) for m in MODELS}, "pulled": e.get("pulled", 0), "error": e.get("error", "")}


def norm_url(u):
    u = str(u or "").strip()
    p = urllib.parse.urlparse(u)
    if p.scheme not in ("https", "http") or not p.hostname or not p.path.strip("/"):
        raise ValueError("type the repository's HTTPS address, for example https://git.example.com/owner/repo.git")
    if p.username or p.password:
        raise ValueError("leave the token out of the address; type it in the token boxes")
    return u


def save(d):
    """Add or change an entry. d: id (blank for new), name, description, url, branch, access, and per model a typed
    token (tokens[model]) or a request to clear it (clear[model]). Returns the entry as public() shows it."""
    items = _load()
    eid = str(d.get("id") or "")
    old = next((e for e in items if e["id"] == eid), None) if eid else None
    if eid and not old:
        raise ValueError("that Git server is no longer listed")
    name = str(d.get("name") or "").strip()
    if not NAME.fullmatch(name):
        raise ValueError("name: 1 to 40 letters, digits, spaces, dots, dashes or underscores")
    if any(e["name"].lower() == name.lower() and e["id"] != eid for e in items):
        raise ValueError(f"there is already a Git server called {name}")
    branch = str(d.get("branch") or "main").strip()
    if not BRANCH.fullmatch(branch):
        raise ValueError("the branch name has odd characters")
    access = d.get("access") if d.get("access") in ACCESS else "read"
    e = {"id": eid or secrets.token_hex(4), "name": name, "description": str(d.get("description") or "").strip()[:500],
         "url": norm_url(d.get("url")), "branch": branch, "access": access}
    if old and (old["url"], old["branch"]) == (e["url"], e["branch"]):
        e.update({k: old[k] for k in ("pulled", "error") if k in old})
    TOKENS.mkdir(parents=True, exist_ok=True)
    os.chmod(TOKENS, 0o700)
    for m in MODELS:
        typed = str((d.get("tokens") or {}).get(m) or "").strip()
        if (d.get("clear") or {}).get(m):
            _token_file(e["id"], m).unlink(missing_ok=True)
        if typed:
            fd = os.open(_token_file(e["id"], m), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(typed)
    items = [x for x in items if x["id"] != e["id"]] + [e]
    _store(items)
    if old and (old["url"], old["branch"]) != (e["url"], e["branch"]):
        shutil.rmtree(COPIES / e["id"], ignore_errors=True)   # our own copy of the old address, not your repository
    agent.log(f"settings: Git server {name} saved ({access})")
    return public(e)


def remove(eid):
    """Take an entry off the list: its tokens and the agent's local copy go. The repository itself is not touched."""
    items = _load()
    e = next((x for x in items if x["id"] == eid), None)
    if not e:
        raise ValueError("that Git server is no longer listed")
    _store([x for x in items if x["id"] != eid])
    for m in MODELS:
        _token_file(eid, m).unlink(missing_ok=True)
    shutil.rmtree(COPIES / eid, ignore_errors=True)
    agent.log(f"settings: Git server {e['name']} removed from the list")


def _git(args, tok, cwd=None, timeout=300):
    """Run git with the token as a one-off header: it is not written to any file or URL."""
    auth = base64.b64encode(f"oauth2:{tok}".encode()).decode()
    cmd = ["git", "-c", f"http.extraHeader=Authorization: Basic {auth}", "-c", "credential.helper=", *args]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode:
        raise RuntimeError(_clean(r.stderr or r.stdout, tok, auth))
    return r.stdout


def _clean(text, *secret):
    for s in secret:
        if s:
            text = text.replace(s, "[token]")
    return " ".join(text.split())[:200] or "git failed"


def test(url, eid, model, typed=""):
    """Can this model's token (or one just typed) read the repository at url? Saves nothing."""
    if model not in MODELS:
        raise ValueError("unknown model")
    tok = str(typed or "").strip() or (token(eid, model) if eid else "")
    if not tok:
        raise ValueError(f"no token for the {MODELS[model]} yet")
    _git(["ls-remote", "--heads", norm_url(url)], tok, timeout=60)
    return True


def _set(eid, **kv):
    items = _load()
    for x in items:
        if x["id"] == eid:
            x.update(kv)
    _store(items)


def copy(eid, model, force=False):
    """The local copy of an entry, pulled for this model if it is older than PULL_EVERY. Returns its folder, or None
    if the model has no token for it. A failed pull parks an Ask card and keeps the last copy."""
    e = get(eid)
    tok = token(eid, model) if e else ""
    if not tok:
        return None
    dest = COPIES / eid
    if not force and (dest / ".git").is_dir() and time.time() - e.get("pulled", 0) < PULL_EVERY:
        return dest
    try:
        COPIES.mkdir(parents=True, exist_ok=True)
        if (dest / ".git").is_dir():
            _git(["fetch", "--depth", "1", "origin", e["branch"]], tok, cwd=dest)
            _git(["reset", "--hard", "-q", "FETCH_HEAD"], tok, cwd=dest)
        else:
            shutil.rmtree(dest, ignore_errors=True)
            _git(["clone", "-q", "--depth", "1", "--branch", e["branch"], e["url"], str(dest)], tok)
        _set(eid, pulled=time.time(), error="")
    except Exception as ex:
        why = str(ex)[:200]
        _set(eid, error=why)
        agent.log(f"git: pulling {e['name']} failed: {why}")
        import questions
        questions.ask(f"Could not pull the Git server {e['name']}: {why}",
                      why="The last copy is used until a pull works. Check its address and the "
                          f"{MODELS[model]}'s token on the Settings page.",
                      kind="gitpull", options=[{"label": "OK", "value": "ok"}], payload={"git": eid, "error": why})
        if not (dest / ".git").is_dir():
            return None
    return dest
