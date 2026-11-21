"""Keeps the model runner of each server on the same model_runner.py as this agent.

Once a minute, each server with a runner is asked for its /status. If its file (sha256) differs from this agent's copy,
the runner is asked to update to this agent's commit: it downloads the file from GitHub itself and checks it against the
sha256 sent here, so no code goes over the link. It is sent at once, also while models on that server have calls running:
those calls are stopped once the runner has accepted it, the same way Apply stops them. While the runner cannot be reached, it is sent as soon as it
can be. A refused update (the file did not match, or was not valid
Python) is not asked again until this agent's version or the runner changes; a failed download is tried again each minute,
which covers a GitHub outage. Each server's state is kept in STATE for the Servers card."""
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import agent
import remote
import settings
import updates

MINE = Path(__file__).resolve().parent / "model_runner.py"
STATE = agent.STATE / "runner_update.json"
EVERY = 60
_started = False


def load():
    try:
        d = json.loads(STATE.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _store(d):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    os.replace(tmp, STATE)


def _stop_calls(sid):
    """The calls of the models on this server are stopped before its runner restarts, as Apply does: the restart would
    cut them off anyway."""
    import thinklog
    s = settings.load()
    for m in remote.PORT:
        r = s[m].get("remote") or {}
        if r.get("enabled") and r.get("server") == sid:
            try:
                thinklog.abort(m)
            except Exception:
                pass


def check_one(p, mine, commit, prev):
    """One server -> its new state record."""
    sid, name = p["id"], p["name"]
    rec = {"commit": commit, "want": mine, "at": time.time()}
    try:
        v = remote.server_runner(sid, "GET", "/status", timeout=10).get("version")
    except ValueError:
        if prev.get("state") == "current" and prev.get("want") == mine:
            return prev   # nothing to send it; it was up to date when last seen
        return dict(rec, state="waiting")
    if not isinstance(v, dict) or not v.get("sha256"):
        return dict(rec, state="old")
    if v["sha256"] == mine:
        if prev.get("state") == "updating":
            agent.log(f"runner on {name}: updated to commit {commit[:7]}")
        return dict(rec, state="current")
    if (prev.get("state") == "failed" and prev.get("kind") != "download" and prev.get("want") == mine
            and prev.get("started") == v.get("started")):
        return prev   # the same request would be refused the same way
    if not commit:
        return dict(rec, state="unknown")
    try:
        r = remote.server_runner(sid, "POST", "/update", {"commit": commit, "sha256": mine}, timeout=120)
    except ValueError:
        return dict(rec, state="waiting")
    if r.get("ok"):
        if not r.get("same"):
            _stop_calls(sid)   # it restarts a second after saying yes; a refused or failed update cuts nothing off
        return dict(rec, state="current" if r.get("same") else "updating")
    why = str(r.get("error") or "the runner gave no reason")
    if prev.get("state") != "failed" or prev.get("error") != why:   # a download retried each minute is logged once
        agent.log(f"runner on {name}: update failed: {why}")
    return dict(rec, state="failed", error=why, kind=r.get("kind", ""), started=v.get("started"))


def check_all():
    try:
        mine = hashlib.sha256(MINE.read_bytes()).hexdigest()
    except OSError:
        return
    commit = updates.installed_now()
    old, new = load(), {}
    for p in remote.servers():
        if p.get("runner"):
            try:
                new[p["id"]] = check_one(p, mine, commit, old.get(p["id"]) or {})
            except Exception as e:
                agent.log(f"runner on {p.get('name')}: the update check failed ({type(e).__name__}: {str(e)[:150]})")
                new[p["id"]] = old.get(p["id"]) or {}
    if new != old:
        _store(new)


def _loop():
    while True:
        try:
            check_all()
        except Exception as e:
            agent.log(f"runner updates: {type(e).__name__}: {str(e)[:150]}")
        time.sleep(EVERY)


def start():
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, daemon=True).start()
