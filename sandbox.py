"""The Assistant's code sandbox: a private working copy of each Git server it has a token for, the editor tools that
change it, and commands run inside it under bubblewrap.

Layout, under <state>/sandbox/<Git server id>/:
  work/       the working copy: a plain file tree, with no .git (code run in the sandbox could otherwise plant Git hooks
              or config that the app's own git, which runs as root, would execute)
  base.json   for every file, the repository version this copy was last brought in line with
  venv/ home/ tmp/   the command's virtualenv, home folder and /tmp (tmp is emptied before every command)

Nothing here reaches a repository. propose() hands each named file to jobs.propose_file, which makes one Ask card per
file; approving commits and pushes that file (gitrepos.commit_file).

The repository moving on: a file the Assistant has not touched is brought up to date. A file changed on both sides is
never merged: status says so and propose refuses it, until the Assistant resets that file and makes its change again.

A command sees the working copy, its virtualenv and /usr (read only), as uid 65534. It does not see the vault, the
state folder, /etc/memory-agent or the app's code. It has no network: the only way out is the proxy below, which passes
only hosts you allowed (always, on the Settings page, or for one command, in the chat). An address on your own network
is treated like any other host. No command is stopped by a timer; Stop answer in the chat stops it."""
import hashlib
import ipaddress
import json
import os
import re
import select
import shutil
import signal
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import agent
import gitrepos

DIR = agent.STATE / "sandbox"
HOSTS = agent.STATE / "sandbox_hosts.json"
PENDING = DIR / "pending"   # a command waiting for your answer on its hosts: <chat message id>.json, and .answer
OUT = ".sandbox-output"     # in the working copy: the whole output of the last command, when it was too long to return
CODE = Path(__file__).resolve().parent
PORT = 3128
HOST = re.compile(r"(?=.{1,253}$)[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)*")
ETC = ("/etc/ssl", "/etc/pki", "/etc/ca-certificates", "/etc/crypto-policies", "/etc/alternatives", "/etc/ld.so.cache",
       "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/localtime", "/etc/mime.types")
ENV = {"PATH": "/venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "VIRTUAL_ENV": "/venv", "HOME": "/home/sandbox",
       "TMPDIR": "/tmp", "LANG": "C.UTF-8", "TERM": "dumb", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
       "HTTP_PROXY": f"http://127.0.0.1:{PORT}", "HTTPS_PROXY": f"http://127.0.0.1:{PORT}",
       "http_proxy": f"http://127.0.0.1:{PORT}", "https_proxy": f"http://127.0.0.1:{PORT}"}


class Refused(ValueError):
    """A tool call that was not carried out: the Assistant is told why in these words."""
    plain = True


# ---------- hosts you always allow ----------
def clean_host(h):
    """A host as the proxy compares it: a lower-case name or an IP address. ValueError if it is anything else."""
    s = str(h or "").strip().lower().rstrip(".")
    try:
        return str(ipaddress.ip_address(s.strip("[]")))
    except ValueError:
        pass
    if not HOST.fullmatch(s):
        raise ValueError(f'a host is a plain name like pypi.org or an IP address, with no https://, port or path: "{h}" is not one')
    return s


def hosts():
    try:
        d = json.loads(HOSTS.read_text())
        return sorted({str(x) for x in d}) if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _store_hosts(items):
    tmp = HOSTS.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(set(items)), indent=1))
    os.replace(tmp, HOSTS)


def add_host(h):
    h = clean_host(h)
    if h not in hosts():
        _store_hosts(hosts() + [h])
        agent.log(f"settings: sandbox commands may always reach {h}")
    return h


def remove_host(h):
    h = str(h or "").strip().lower()
    if h not in hosts():
        raise ValueError(f"{h} is not on the list")
    _store_hosts([x for x in hosts() if x != h])
    agent.log(f"settings: sandbox commands may no longer always reach {h}")


def where(host):
    """"" for a host on the internet, or what it is when it is not: said next to the host when you are asked."""
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            ips = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in socket.getaddrinfo(host, None)]
        except (OSError, ValueError):
            return ""
    if any(i.is_loopback for i in ips):
        return "this server itself"
    if any(i.is_private or i.is_link_local for i in ips):
        return "on your own network"
    return ""


# ---------- asking you, in the chat ----------
def pending():
    """{chat message id: {"server", "command", "hosts": [{"host", "where"}]}} for every command waiting for your answer."""
    out = {}
    for p in PENDING.glob("*.json") if PENDING.is_dir() else []:
        try:
            out[p.stem] = json.loads(p.read_text())
        except (OSError, ValueError):
            pass
    return out


def answer(mid, what):
    """Your answer from the chat page: once, always or deny."""
    mid = str(mid)
    if what not in ("once", "always", "deny"):
        raise ValueError("the answer is once, always or deny")
    if mid not in pending():
        raise ValueError("nothing is waiting for an answer there")
    tmp = PENDING / f"{mid}.tmp"
    tmp.write_text(what)
    os.replace(tmp, PENDING / f"{mid}.answer")


def clear_pending():
    """At chat worker start: a question from before the restart has nobody waiting for it (its message is answered again)."""
    shutil.rmtree(PENDING, ignore_errors=True)


def _ask(mid, e, command, need, cancel):
    """Ask you about these hosts under the chat message, and wait for as long as it takes. -> once, always, deny or stopped."""
    PENDING.mkdir(parents=True, exist_ok=True)
    q, a = PENDING / f"{mid}.json", PENDING / f"{mid}.answer"
    a.unlink(missing_ok=True)
    q.write_text(json.dumps({"server": e["name"], "command": command, "hosts": [{"host": h, "where": where(h)} for h in need]}))
    agent.log(f"sandbox: asked you in the chat whether a command in {e['name']} may reach {', '.join(need)}")
    try:
        while True:
            if cancel and cancel():
                return "stopped"
            if a.exists():
                what = a.read_text().strip()
                if what in ("once", "always", "deny"):
                    return what
            time.sleep(1)
    finally:
        q.unlink(missing_ok=True)
        a.unlink(missing_ok=True)


# ---------- the proxy: the sandbox's only way out ----------
class _Proxy(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path, allowed):
        self.allowed, self.blocked = set(allowed), []
        super().__init__(path, _Handler)


class _Handler(socketserver.BaseRequestHandler):
    def _say(self, code, text):
        body = (text + "\n").encode()
        try:
            self.request.sendall(f"HTTP/1.1 {code}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                                 "Connection: close\r\n\r\n".encode() + body)
        except OSError:
            pass

    def handle(self):
        c, head = self.request, b""
        try:
            while b"\r\n\r\n" not in head:
                d = c.recv(65536)
                if not d or len(head) > 1 << 20:
                    return
                head += d
            top, _, after = head.partition(b"\r\n\r\n")
            lines = top.decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
        except (OSError, ValueError):
            return
        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            first = after
        else:
            u = urllib.parse.urlsplit(target)
            if u.scheme != "http" or not u.hostname:
                return self._say("400 Bad Request", "sandbox proxy: only http:// and https:// addresses can be reached")
            host, port = u.hostname, u.port or 80
            keep = [x for x in lines[1:] if x.split(":")[0].strip().lower() not in ("connection", "proxy-connection", "proxy-authorization")]
            path = urllib.parse.urlunsplit(("", "", u.path or "/", u.query, ""))
            first = ("\r\n".join([f"{method} {path} {version}"] + keep + ["Connection: close"]) + "\r\n\r\n").encode("latin-1") + after
        try:
            host, port = clean_host(host), int(port)
        except ValueError:
            return self._say("400 Bad Request", "sandbox proxy: that is not a host and port")
        if host not in self.server.allowed:
            if host not in self.server.blocked:
                self.server.blocked.append(host)
            return self._say("403 Forbidden", f"sandbox proxy: {host} was not approved for this command, so the connection was refused")
        try:
            up = socket.create_connection((host, port), timeout=30)
        except OSError as e:
            return self._say("502 Bad Gateway", f"sandbox proxy: could not connect to {host}:{port} ({e})")
        try:
            up.settimeout(None)
            if method.upper() == "CONNECT":
                c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if first:
                up.sendall(first)
            _relay(c, up)
        except OSError:
            pass
        finally:
            up.close()


def _relay(a, b):
    """Pass bytes both ways until both sides have finished."""
    other, open_ = {a: b, b: a}, {a, b}
    while open_:
        for s in select.select(list(open_), [], [])[0]:
            try:
                d = s.recv(65536)
            except OSError:
                d = b""
            if d:
                other[s].sendall(d)
            else:
                open_.discard(s)
                try:
                    other[s].shutdown(socket.SHUT_WR)
                except OSError:
                    pass


# ---------- the working copy ----------
def _box(e):
    return DIR / e["id"]


def _work(e):
    return _box(e) / "work"


def _manifest(e):
    try:
        d = json.loads((_box(e) / "base.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _store(e, d):
    tmp = _box(e) / "base.tmp"
    tmp.write_text(json.dumps(d))
    os.replace(tmp, _box(e) / "base.json")


def _sha(p):
    """A fingerprint of a file's exact contents (the same one gitrepos.content_sha gives), or of where a link points."""
    if p.is_symlink():
        return "link:" + os.readlink(p)
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree(root):
    """{path: fingerprint} of every file and link under root. Links are never followed. The saved-output folder is not part of it."""
    out = {}
    for d, dirs, files in os.walk(root):
        if Path(d) == Path(root):
            dirs[:] = [x for x in dirs if x != OUT]
        for n in list(dirs):
            if os.path.islink(os.path.join(d, n)):
                dirs.remove(n)
                files.append(n)
        for n in files:
            p = Path(d) / n
            if p.is_symlink() or p.is_file():
                out[p.relative_to(root).as_posix()] = _sha(p)
    return out


def _repo(root):
    """{path: fingerprint} of every file in the app's own copy of the repository."""
    names = [n for n in gitrepos._git(["ls-files", "-z"], "", cwd=root).split("\0") if n]
    return {n: _sha(root / n) for n in names if (root / n).is_symlink() or (root / n).is_file()}


def _safe(work, rel):
    """work/rel, only if it really is inside the working copy: a link made by a command must never lead a write or a read
    outside it (these tools run as the app, not in the sandbox)."""
    rel = str(rel).strip("/")
    p = work / rel
    if not rel or ".." in Path(rel).parts or not p.parent.resolve().is_relative_to(work.resolve()):
        raise Refused(f"{rel or 'that path'} leaves the working copy, so nothing was done.")
    if p.exists() and not p.resolve().is_relative_to(work.resolve()):
        raise Refused(f"{rel} is a link to somewhere outside the working copy, so nothing was done.")
    return p


def _copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)
    if src.is_symlink():
        os.symlink(os.readlink(src), dst)
    else:
        shutil.copy2(src, dst)


def sync(e):
    """Bring the working copy in line with the repository where the Assistant changed nothing. -> the app's copy of the
    repository. A file changed on both sides is left exactly as it is here (never merged)."""
    root = gitrepos.copy(e["id"], "pa")
    if root is None:
        raise Refused(f"Could not get a copy of {e['name']}, so nothing was done. The person has an Ask card with the reason; "
                      "tell them, and do not try again until they have looked.")
    head = gitrepos._git(["rev-parse", "HEAD"], "", cwd=root).strip()
    m, work = _manifest(e), _work(e)
    if m.get("head") == head and work.is_dir():
        return root
    work.mkdir(parents=True, exist_ok=True)
    repo, base, mine = _repo(root), dict(m.get("files") or {}), _tree(work)
    for rel in sorted(set(repo) | set(base)):
        r, b, w = repo.get(rel), base.get(rel), mine.get(rel)
        if b == r:
            continue
        if w == b:   # not touched here: take the repository's version
            try:
                p = _safe(work, rel)
            except Refused:
                continue
            if r is None:
                p.unlink(missing_ok=True)
            else:
                _copy(root / rel, p)
        elif w != r:   # changed here and in the repository: left alone, and status says so
            continue
        if r is None:
            base.pop(rel, None)
        else:
            base[rel] = r
    _store(e, {"head": head, "files": base})
    return root


def _changes(e, root):
    """[(path, new|changed|deleted, whether the repository also changed it since)] for every file that differs."""
    repo, base, mine = _repo(root), _manifest(e).get("files") or {}, _tree(_work(e))
    return [(rel, "new" if rel not in repo else "deleted" if rel not in mine else "changed", base.get(rel) != repo.get(rel))
            for rel in sorted(set(repo) | set(mine)) if repo.get(rel) != mine.get(rel)]


# ---------- paths the Assistant types ----------
def _entry(name):
    name = str(name or "").strip().strip("/")
    name = name[4:] if name.startswith("git/") else name
    e = gitrepos.by_name(name)
    if not e or not gitrepos.has_token(e["id"], "pa"):
        names = ", ".join(x["name"] for x in gitrepos.readable("pa")) or "none yet (the person adds them under Settings, Git servers)"
        raise Refused(f"There is no Git server called {name or '(empty)'} that you can read. The names are: {names}. Use one exactly as written.")
    return e


def _split(path):
    """git/<Git server name>/<path inside> -> (entry, path inside, "" for the top folder)."""
    parts = str(path or "").strip().strip("/").split("/", 2)
    if len(parts) < 2 or parts[0] != "git":
        raise Refused("A sandbox path looks like git/<Git server name>/<file path>. Nothing was done.")
    return _entry(parts[1]), (parts[2].strip("/") if len(parts) > 2 else "")


def _label(e, inner):
    return f"git/{e['name']}/{inner}".rstrip("/")


def _file(path, exists=None, reading=False):
    """-> (entry, path inside, its place in the working copy), brought up to date first. exists: True if it must be a
    file, False if nothing may be there. reading: the saved-output folder may be read, never changed."""
    e, inner = _split(path)
    sync(e)
    if not inner:
        raise Refused(f"{_label(e, inner)} is the top folder: name a file in it. Nothing was done.")
    if Path(inner).parts[0] == OUT and not reading:
        raise Refused(f"{OUT} holds saved command output only; it is not part of the repository. Nothing was done.")
    p = _safe(_work(e), inner)
    if exists is True and not (p.is_file() or p.is_symlink()):
        raise Refused(f"Nothing was changed: {_label(e, inner)} is {'a folder' if p.is_dir() else 'not in the working copy'}. "
                      "Use list to see what is there.")
    if exists is False and (p.exists() or p.is_symlink()):
        raise Refused(f"Nothing was changed: {_label(e, inner)} already exists. Use replace or insert to change it, or delete it first.")
    return e, inner, p


def _text(p, label):
    data = p.read_bytes()
    try:
        if b"\0" in data[:8192]:
            raise ValueError
        return data.decode("utf-8")
    except ValueError:
        raise Refused(f"{label} is not a UTF-8 text file, so it cannot be shown or edited with these tools.") from None


def _int(v, name, default):
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        raise Refused(f"{name} must be a whole number. Nothing was done.") from None


def _limit():
    import tuning
    return tuning.get("sandbox.output_chars")


# ---------- the editor tools ----------
def list_dir(path):
    e, inner = _split(path)
    sync(e)
    p = _safe(_work(e), inner) if inner else _work(e)
    if not p.is_dir():
        raise Refused(f"{_label(e, inner)} is {'a file: use view to read it' if p.exists() else 'not in the working copy'}.")
    names = sorted(x.name + ("/" if x.is_dir() and not x.is_symlink() else "") for x in p.iterdir() if inner or x.name != OUT)
    return f"{_label(e, inner)}/ holds:\n" + ("\n".join(names) or "(nothing)")


def grep(path, pattern):
    e, inner = _split(path)
    sync(e)
    work = _work(e)
    p = _safe(work, inner) if inner else work
    try:
        rx = re.compile(str(pattern or ""))
    except re.error as ex:
        raise Refused(f"That pattern is not a valid regular expression ({ex}). Nothing was searched.") from None
    if not str(pattern or ""):
        raise Refused("Give a pattern to look for. Nothing was searched.")
    if not p.exists():
        raise Refused(f"{_label(e, inner)} is not in the working copy.")
    files = [inner] if p.is_file() else [r for r in sorted(_tree(work)) if not inner or r.startswith(inner + "/")]
    hits = []
    for rel in files:
        f = work / rel
        if f.is_symlink() or not f.is_file():
            continue
        try:
            text = _text(f, rel)
        except Refused:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{n}: {line[:300]}" + (f" ...(the line goes on: {len(line)} characters)" if len(line) > 300 else ""))
    if not hits:
        return "Nothing matched."
    shown, size = [], 0
    for h in hits:
        size += len(h) + 1
        if shown and size > _limit():
            break
        shown.append(h)
    return "\n".join(shown) + (f"\n({len(hits)} lines matched; the first {len(shown)} are above. Narrow the pattern or the path to see the rest.)"
                               if len(shown) < len(hits) else "")


def view(path, start=None, end=None):
    e, inner, p = _file(path, True, reading=True)
    label = _label(e, inner)
    lines = _text(p, label).splitlines()
    start = max(1, _int(start, "start", 1))
    end = min(len(lines), _int(end, "end", len(lines)))
    if lines and start > len(lines):
        raise Refused(f"{label} has {len(lines)} lines, so there is no line {start}.")
    body = "\n".join(f"{n}: {l}" for n, l in enumerate(lines[start - 1:end], start))
    return f"{label}, lines {start} to {max(end, start - 1)} of {len(lines)}:\n{body}"


def create(path, text):
    e, inner, p = _file(path, False)
    if not isinstance(text, str):
        raise Refused("Nothing was changed: give the whole file as text.")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return (f"Created {_label(e, inner)} ({len(text.splitlines())} lines) in the working copy. It is not in the repository "
            "until you propose it and the person approves.")


def replace(path, old, new):
    e, inner, p = _file(path, True)
    label = _label(e, inner)
    current, old, new = _text(p, label), str(old or ""), str(new if new is not None else "")
    if not old:
        raise Refused("Nothing was changed: old is empty. Give the exact text to replace, or use insert to add text.")
    n = current.count(old)
    if n != 1:
        raise Refused(f"Nothing was changed: the text to replace is {'not in' if not n else f'in {n} places in'} {label}. "
                      "View the file and copy the exact text (a longer piece, if it repeats).")
    if old == new:
        raise Refused("Nothing was changed: new is the same as old.")
    p.write_bytes(current.replace(old, new, 1).encode("utf-8"))
    at = current[:current.index(old)].count("\n") + 1
    return f"Replaced it in {label}, starting at line {at}. The change is in the working copy only."


def insert(path, line, text):
    e, inner, p = _file(path, True)
    label = _label(e, inner)
    lines, text = _text(p, label).splitlines(keepends=True), str(text if text is not None else "")
    at = _int(line, "line", None)
    if at is None or not 0 <= at <= len(lines):
        raise Refused(f"Nothing was changed: {label} has {len(lines)} lines, so line must be a number from 0 (the top) to {len(lines)}.")
    if not text:
        raise Refused("Nothing was changed: give the text to add.")
    if at and not lines[at - 1].endswith("\n"):
        lines[at - 1] += "\n"
    lines.insert(at, text if text.endswith("\n") else text + "\n")
    p.write_bytes("".join(lines).encode("utf-8"))
    return f"Added {len(text.splitlines())} line(s) to {label} after line {at}. The change is in the working copy only."


def delete(path):
    e, inner, p = _file(path, True)
    p.unlink()
    return (f"Deleted {_label(e, inner)} from the working copy. It is still in the repository until you propose the delete and "
            "the person approves.")


def rename(src, dest):
    e, a, pa = _file(src, True)
    e2, b, pb = _file(dest, False)
    if e2["id"] != e["id"]:
        raise Refused("Nothing was changed: a file can only be moved inside one Git server.")
    pb.parent.mkdir(parents=True, exist_ok=True)
    os.replace(pa, pb)
    return (f"Moved {_label(e, a)} to {_label(e, b)} in the working copy. To put that in the repository, propose both paths: "
            "the old one as a delete and the new one as a new file.")


def status(repo):
    import jobs
    e = _entry(repo)
    ch = _changes(e, sync(e))
    if not ch:
        return f"{e['name']}: the working copy is the same as the repository ({e['branch']})."
    repo_dirs = {str(d) for rel in _repo(gitrepos.COPIES / e["id"]) for d in Path(rel).parents}
    lines, folders = [], {}
    for rel, kind, both in ch:
        top = next((str(d) for d in reversed(Path(rel).parents) if str(d) != "." and str(d) not in repo_dirs), None)
        if kind == "new" and top and not both:   # a whole new folder (build output, caches) is one line, not one per file
            folders[top] = folders.get(top, 0) + 1
            continue
        lines.append(f"{kind}: {rel}"
                     + (" - ALSO CHANGED IN THE REPOSITORY since you changed it: reset it and make your change again before you propose it" if both else "")
                     + (" - a change to it is waiting for the person's approval" if jobs.pending_change(e["id"], rel) else ""))
    lines += [f"new folder: {d}/ ({n} file{'s' if n != 1 else ''}; list it to see them)" for d, n in sorted(folders.items())]
    return f"{e['name']}: {len(ch)} file{'s' if len(ch) != 1 else ''} differ from the repository ({e['branch']}).\n" + "\n".join(lines)


def reset(path):
    e, inner = _split(path)
    root, work = sync(e), _work(e)
    if not inner:
        shutil.rmtree(work, ignore_errors=True)
        (_box(e) / "base.json").unlink(missing_ok=True)
        sync(e)
        return (f"Reset the whole working copy of {e['name']} to the repository ({e['branch']}). Your changes there are gone; "
                "its virtualenv is kept.")
    if Path(inner).parts[0] == OUT:
        raise Refused(f"{OUT} holds saved command output only. Nothing was done.")
    _safe(work, inner)
    ch = [(rel, kind) for rel, kind, _ in _changes(e, root) if rel == inner or rel.startswith(inner + "/")]
    if not ch:
        return f"{_label(e, inner)} is the same as the repository: nothing to reset."
    m, repo = _manifest(e), _repo(root)
    base = dict(m.get("files") or {})
    for rel, kind in ch:
        p = _safe(work, rel)
        if kind == "new":
            p.unlink()
            base.pop(rel, None)
        else:
            _copy(root / rel, p)
            base[rel] = repo[rel]
    _store(e, {"head": m.get("head"), "files": base})
    return f"Reset {len(ch)} file{'s' if len(ch) != 1 else ''} in {e['name']} to the repository's version: {', '.join(r for r, _ in ch)}."


def propose(paths, why, chat=""):
    """Ask the person to approve the named files as they are in the working copy: one Ask card per file. Every file is
    checked first; if one cannot be proposed, none is."""
    import jobs
    paths = [paths] if isinstance(paths, str) else list(paths or [])
    if not paths:
        raise Refused('Nothing was proposed: name the files, like {"paths":["git/<Git server name>/<file>"],"why":"<one sentence>"}.')
    why = " ".join(str(why or "").split())[:300]
    if not why:
        raise Refused("Nothing was proposed: say why in one sentence, in why. The person reads it on the card.")
    todo, bad, changes = [], [], {}
    for path in dict.fromkeys(str(x) for x in paths):
        try:
            e, inner = _split(path)
            if e["id"] not in changes:
                changes[e["id"]] = {rel: (kind, both) for rel, kind, both in _changes(e, sync(e))}
            label, p = _label(e, inner), _safe(_work(e), inner) if inner else None
            if not inner or p.is_dir() and not p.is_symlink():
                raise Refused(f"{label} is a folder: name each file.")
            if Path(inner).parts[0] in (OUT, ".git"):
                raise Refused(f"{label} is not a file that can go to the repository.")
            if inner not in changes[e["id"]]:
                raise Refused(f"{label} is the same as the repository: there is nothing to propose for it.")
            kind, both = changes[e["id"]][inner]
            if both:
                raise Refused(f"Nothing was proposed for {label}: it changed in the repository after you changed it here, and changes "
                              "are never merged. reset that file to get the repository's version, make your change again, then propose.")
            if e["access"] == "write" and jobs.pending_change(e["id"], inner):
                raise Refused(f"There is already a pending change to {inner} in {e['name']}. You must wait for the pending edit to be "
                              "approved or denied before you can propose that file again.")
            if kind != "deleted" and p.is_symlink():
                raise Refused(f"{label} is a link, and only text files can be proposed.")
            text = None if kind == "deleted" else _text(p, label)
            todo.append((e, inner, text, kind != "deleted" and os.access(p, os.X_OK)))
        except Refused as ex:
            bad.append(str(ex))
    if bad:
        raise Refused("Nothing was proposed, because of the file(s) below. Put them right and propose again.\n" + "\n".join("- " + b for b in bad))
    done = [jobs.propose_file(e, inner, text, why, chat, executable) for e, inner, text, executable in todo]
    out = []
    for name in dict.fromkeys(j["result"]["server"] for j in done):
        mine = [j["result"] for j in done if j["result"]["server"] == name]
        if mine[0]["access"] != "write":
            out.append(f"NOT pushed, and nothing is waiting for the person: {name} is set to Read only. Tell them exactly that, and show "
                       "them these changes in your answer, in a code block, so they can apply them themselves:\n\n"
                       + "\n".join(r["diff"] for r in mine)
                       + f"\nIf they want you to push instead, they set {name} to Read and write under Settings > Git servers (a read "
                       "and write key alone does not change that setting) and ask you again.")
        else:
            out.append(f"Proposed {len(mine)} file{'s' if len(mine) != 1 else ''} in {name} ({mine[0]['branch']}): {', '.join(r['path'] for r in mine)}. "
                       f"{'Each waits' if len(mine) != 1 else 'It waits'} for the person's approval on their Ask page. Tell them "
                       f"{'they are' if len(mine) != 1 else 'it is'} waiting there.")
    return "\n\n".join(out)


# ---------- running a command ----------
def _bwrap(e, sock):
    box = _box(e)
    for d in ("work", "venv", "home", "tmp"):
        (box / d).mkdir(parents=True, exist_ok=True)
    etc = DIR / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    for name, text in (("passwd", "sandbox:x:65534:65534:sandbox:/home/sandbox:/bin/bash\n"), ("group", "sandbox:x:65534:\n"),
                       ("hosts", "127.0.0.1 localhost\n")):
        if not (etc / name).is_file():
            (etc / name).write_text(text)
    a = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--uid", "65534", "--gid", "65534", "--ro-bind", "/usr", "/usr"]
    for n in ("bin", "sbin", "lib", "lib32", "lib64", "libx32"):
        if os.path.islink("/" + n):
            a += ["--symlink", os.readlink("/" + n), "/" + n]
        elif os.path.isdir("/" + n):
            a += ["--ro-bind", "/" + n, "/" + n]
    for p in ETC:
        if os.path.exists(p):
            a += ["--ro-bind", p, p]
    for name in ("passwd", "group", "hosts"):
        a += ["--ro-bind", str(etc / name), "/etc/" + name]
    a += ["--proc", "/proc", "--dev", "/dev", "--bind", str(box / "tmp"), "/tmp", "--bind", str(box / "work"), "/work",
          "--bind", str(box / "venv"), "/venv", "--bind", str(box / "home"), "/home/sandbox",
          "--ro-bind", str(CODE / "sandbox_fwd.py"), "/run/sandbox/fwd.py", "--ro-bind", sock, "/run/sandbox/proxy.sock",
          "--chdir", "/work", "--clearenv"]
    for k, v in ENV.items():
        a += ["--setenv", k, v]
    return a + ["--", "/usr/bin/python3", "-I", "/run/sandbox/fwd.py", "/run/sandbox/proxy.sock", str(PORT)]


def _exec(e, command, allowed, cancel=None):
    """Run one shell command in the sandbox. -> (exit code, seconds, hosts it tried that were not allowed, whether it was
    stopped). Its output, stdout and stderr together, is in <box>/out.txt. No timer stops it."""
    if not shutil.which("bwrap"):
        raise Refused("bubblewrap (bwrap) is not installed on this server, so nothing can run in the sandbox. Tell the person: "
                      "updating the memory agent installs it.")
    box = _box(e)
    shutil.rmtree(box / "tmp", ignore_errors=True)
    tmp = tempfile.mkdtemp(prefix="sbx")   # a short path: a unix socket's is limited to about 100 characters
    sock = os.path.join(tmp, "p.sock")
    proxy = _Proxy(sock, allowed)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    began, stopped = time.time(), False
    try:
        with open(box / "out.txt", "wb") as out:
            p = subprocess.Popen(_bwrap(e, sock) + ["/bin/bash", "-c", command], stdin=subprocess.DEVNULL, stdout=out,
                                 stderr=subprocess.STDOUT, start_new_session=True)
            while p.poll() is None:
                if cancel and cancel():
                    stopped = True
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    p.wait()
                    break
                time.sleep(0.2)
        return p.returncode, time.time() - began, list(proxy.blocked), stopped
    finally:
        proxy.shutdown()
        proxy.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def _output(e):
    """The command's output for the Assistant. Too long to return: the end of it, with the whole of it saved in the
    working copy and said so."""
    out, limit = _box(e) / "out.txt", _limit()
    size = out.stat().st_size
    if size <= limit:
        return out.read_text(encoding="utf-8", errors="replace") or "(no output)"
    with open(out, "rb") as f:
        f.seek(size - limit)
        tail = f.read().decode("utf-8", errors="replace")
    try:
        _copy(out, _safe(_work(e), f"{OUT}/last.txt"))
        saved = f"The whole output is in git/{e['name']}/{OUT}/last.txt: read it with view."
    except (Refused, OSError) as ex:
        saved = f"The whole output could not be saved to read ({ex})."
    return f"The output is {size:,} characters; the last {limit:,} are below. {saved}\n{tail}"


def _venv(e, cancel):
    """The working copy's virtualenv, made the first time a command runs. -> a line for the Assistant if it could not be made."""
    if os.path.lexists(_box(e) / "venv" / "bin" / "python"):
        return ""
    code, _, _, stopped = _exec(e, "python3 -m venv /venv", (), cancel)
    if code and not stopped:
        return ("No virtualenv could be made for this working copy (python3 -m venv failed), so python and pip are the system's: "
                + " ".join(_output(e).split())[:300] + "\n")
    return ""


def run(repo, command, want=None, chat=None):
    """The run tool. chat: {"id": the chat message, "cancel": says the answer was stopped, "show": tells the chat page
    what is running}."""
    chat = chat or {}
    cancel, show = chat.get("cancel"), chat.get("show") or (lambda x: None)
    e, command = _entry(repo), str(command or "").strip()
    if not command:
        raise Refused("Give the command to run. Nothing ran.")
    sync(e)
    want = [want] if isinstance(want, str) else list(want or [])
    try:
        want = list(dict.fromkeys(clean_host(h) for h in want))
    except ValueError as ex:
        raise Refused(f"The command did not run: in hosts, {ex}.") from None
    always = hosts()
    need = [h for h in want if h not in always]
    if need:
        what = _ask(chat.get("id") or "none", e, command, need, cancel)
        if what == "stopped":
            return "STOPPED by the person while it waited for their answer. The command did not run."
        if what == "deny":
            agent.log(f"sandbox: you denied {', '.join(need)} for a command in {e['name']}")
            return ("The person DENIED internet access for this command, so it did not run. Do not ask for the same thing again: "
                    "go on without the internet, or tell them what you could not do.")
        if what == "always":
            for h in need:
                add_host(h)
        agent.log(f"sandbox: you allowed {', '.join(need)} {'always' if what == 'always' else 'once'} for a command in {e['name']}")
    allowed = sorted(set(always) | set(want))
    note = _venv(e, cancel)
    show({"server": e["name"], "command": command, "since": time.time()})
    try:
        code, secs, blocked, stopped = _exec(e, command, allowed, cancel)
    finally:
        show(None)
    net = "internet: " + ", ".join(allowed) if allowed else "no internet"
    head = (f"STOPPED by the person after {secs:.1f} seconds. It did not finish. Output so far:" if stopped
            else f"EXIT CODE {code} after {secs:.1f} seconds, {net}.")
    extra = [f"BLOCKED: the command tried to reach {h}, which was not in hosts, so that connection was refused. Run it again with "
             f"{h} in hosts if it is needed." for h in blocked]
    if code and not stopped and not allowed and not blocked:
        extra.append("This ran with no internet. If it needs it, run it again with the hosts it needs in hosts.")
    agent.log(f"sandbox: ran in {e['name']}: {' '.join(command.split())[:120]} -> {'stopped' if stopped else 'exit ' + str(code)}")
    return note + "\n".join([head] + extra + (["OUTPUT:"] if not stopped else [])) + "\n" + _output(e)
