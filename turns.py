"""Which models may work at the same time, and whose turn it is.

Models that run on the same hardware (this machine, or one external server) work one at a time, unless a group lists
them together: a group is a list of models that may work at the same time, and a model may be in several groups. The
models working at one time on the same hardware must all be in one group; the rest take turns. Models on different hardware never wait for each other. When several
want the same hardware, the Assistant goes first, then the Reviewer, the Sweeper and the Curator.

How it works. A call that needs a model asks for it (a "want") and holds a "lease" while it runs (thinklog.chat does
both). The manager, a thread of the web service, looks at the wants and leases every few seconds: when a wanted model may not
work together with one that is working, it lets that one finish its running call, stops its server if it is on this
machine, and starts the wanted model. A model that still has work keeps its turn for at least `turn_minutes` before it
must give way; the Assistant, a message you send in the chat and a model you load by hand do not wait for that. Callers only ever write small files
in <state>/turns, so any service can use this; only the manager starts and stops model servers.

If the manager is not running (its heartbeat is old), callers do not wait for turns: they behave as before."""
import fcntl
import json
import os
import secrets
import subprocess
import threading
import time
from contextlib import contextmanager

import agent

DIR = agent.STATE / "turns"
LEASES = DIR / "leases"
HOLDS = DIR / "holds"   # a model working through a whole task (several calls with other work between them)
CONFIG = agent.STATE / "model_groups.json"
ACTIVE, BEAT, LOCK = DIR / "active.json", DIR / "beat", DIR / "lock"
MODELS = ("qwen", "gpt", "cur", "pa")
PRIORITY = ("pa", "gpt", "qwen", "cur")   # who goes first when models that may not work together both want to
NICE = {"qwen": "Sweeper", "gpt": "Reviewer", "cur": "Curator", "pa": "Assistant"}
FRESH = 30         # seconds a want counts after it was last made (a waiting call repeats it every few seconds)
QUIET = 30         # seconds after its last call that a model still counts as busy: its group is not given away between two calls
BEAT_FRESH = 20    # seconds the manager's heartbeat counts
STEP = 2           # seconds between the manager's looks
_started = False


def _dirs():
    LEASES.mkdir(parents=True, exist_ok=True)


# ---------- the groups you set ----------
def config():
    try:
        d = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        d = {}
    groups = []
    for g in (d.get("groups") or []) if isinstance(d, dict) else []:
        models = [m for m in dict.fromkeys(g.get("models") or []) if m in MODELS]
        if models:
            groups.append({"id": str(g.get("id") or secrets.token_hex(3)), "models": models})
    try:
        minutes = max(0, int(float((d or {}).get("turn_minutes", 10))))
    except (TypeError, ValueError):
        minutes = 10
    return {"groups": groups, "turn_minutes": minutes}


def save_config(groups, turn_minutes):
    """Store the groups exactly as given: a model may be in several. -> (config, notes; none now, kept for the page)."""
    out = []
    for g in groups or []:
        models = [m for m in dict.fromkeys((g.get("models") if isinstance(g, dict) else g) or []) if m in MODELS]
        if models:
            out.append({"id": str((g.get("id") if isinstance(g, dict) else "") or secrets.token_hex(3)), "models": models})
    try:
        minutes = max(0, int(float(turn_minutes)))
    except (TypeError, ValueError):
        raise ValueError("minutes must be a number")
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps({"groups": out, "turn_minutes": minutes}, indent=1))
    os.replace(tmp, CONFIG)
    agent.log("settings: model groups " + (", ".join("+".join(NICE[m] for m in g["models"]) for g in out) or "none (one model at a time on each machine)"))
    return config(), []


def server_of(model):
    """The external server this model's calls go to (None for a local model)."""
    try:
        import remote
        return remote.base(model).rstrip("/").lower() or None if remote.enabled(model) else None
    except Exception:
        return None


def hardware(model):
    """Where the model runs: this machine, or one external server. Only models on the same hardware wait for each other."""
    if not _external(model):
        return "local"
    return "server " + (server_of(model) or model)


def together(a, b):
    return any(a in g["models"] and b in g["models"] for g in config()["groups"])


def blockers(model, loaded):
    """The loaded models on the same hardware that must finish before this one can work: the models that working
    together would need a group for. Models working at one time must all be in one group, so the group that this model
    is in and that holds most of the loaded ones is the one it joins; the rest are in its way. [] means it fits."""
    same = [x for x in loaded if x != model and hardware(x) == hardware(model)]
    if not same:
        return []
    best = max((g["models"] for g in config()["groups"] if model in g["models"]), key=lambda g: sum(x in g for x in same), default=[])
    return [x for x in same if x not in best]


def conflict(a, b):
    """True if the two models may not work at the same time: same hardware and no group has both."""
    return a != b and hardware(a) == hardware(b) and not together(a, b)


# ---------- what is local, and whether the manager is there ----------
def _external(model):
    try:
        import remote
        return remote.enabled(model)
    except Exception:
        return False


def _has_model(model):
    try:
        import settings
        return bool(settings.get(model).get("model_file"))
    except Exception:
        return False


def alive():
    try:
        return time.time() - BEAT.stat().st_mtime < BEAT_FRESH
    except OSError:
        return False


def managed(model):
    """True if this model's calls take turns: it has a model file and the manager is running."""
    return model in MODELS and _has_model(model) and alive()


def _manual_hold(model):
    try:
        import power
        return power.manual(model)
    except Exception:
        return False


def _off(model):
    try:
        import power
        return power.off(model)
    except Exception:
        return False


# ---------- wants, leases and the loaded group ----------
def want(model, urgent=False):
    """This model is needed now. Repeat while waiting: a want older than FRESH seconds is forgotten."""
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / f".want-{model}.{os.getpid()}"
    tmp.write_text(json.dumps({"at": time.time(), "urgent": bool(urgent)}))
    os.replace(tmp, DIR / f"want-{model}")


def wants(now=None):
    """-> {model: {"at", "urgent"}} for models wanted lately, plus the ones held on purpose: the Curator while it holds a
    turn, and models you loaded by hand."""
    now = now or time.time()
    out = {}
    for m in MODELS:
        try:
            d = json.loads((DIR / f"want-{m}").read_text())
            if now - d["at"] < FRESH:
                out[m] = d
        except (OSError, ValueError, KeyError):
            continue
    try:
        import power
        import swap
        if swap.holding():
            out.setdefault("cur", {"at": now, "urgent": False})
        for m in MODELS:
            if power.manual(m):
                out[m] = {"at": now, "urgent": True}
    except Exception:
        pass
    return {m: d for m, d in out.items() if not _off(m)}


def clear_want(model):
    (DIR / f"want-{model}").unlink(missing_ok=True)


def _used(model):
    """Note that this model had a call starting or ending just now."""
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / f"used-{model}").write_text(str(time.time()))


def used_after(model, since):
    try:
        return float((DIR / f"used-{model}").read_text()) >= since
    except (OSError, ValueError):
        return False


def used_lately(model, now=None):
    try:
        return (now or time.time()) - float((DIR / f"used-{model}").read_text()) < QUIET
    except (OSError, ValueError):
        return False


def lease_counts(folder=None):
    """-> {model: running calls}. A lease whose process is gone is removed."""
    out = {}
    folder = folder or LEASES
    for p in folder.glob("*") if folder.exists() else []:
        try:
            model, pid, _ = p.name.split(".", 2)
            os.kill(int(pid), 0)
        except ProcessLookupError:
            p.unlink(missing_ok=True)
            continue
        except (ValueError, PermissionError):
            pass
        out[p.name.split(".")[0]] = out.get(p.name.split(".")[0], 0) + 1
    return out


def active():
    """-> {"group": models allowed to work now, "since": {model: when its turn began}, "draining": models finishing up."""
    try:
        d = json.loads(ACTIVE.read_text())
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    since = d.get("since") if isinstance(d.get("since"), dict) else {}   # an older version stored one time here
    groupd = d.get("group") if isinstance(d.get("group"), list) else []
    draining = d.get("draining") if isinstance(d.get("draining"), list) else []   # and true or false here
    models = [m for m in groupd if m in MODELS]
    return {"group": models, "since": {m: float(since.get(m, 0)) for m in models},
            "draining": [m for m in draining if m in models]}


def _set_active(st):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = ACTIVE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, ACTIVE)


@contextmanager
def _lock():
    DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def lease(model, urgent=False, stop=None):
    """Hold this model's turn while a call runs. Waits (asking for the turn) until the model is loaded in the group that
    has it. stop(): called while waiting; raise from it to give up (a server switch or stop). Does nothing without the manager."""
    import applier
    if not managed(model):
        yield
        return
    _dirs()
    path = None
    while path is None:
        want(model, urgent)
        if stop:
            stop()
        with _lock():
            st = active()
            if model in st["group"] and model not in st["draining"] and applier.health(model) == "ready":
                path = LEASES / f"{model}.{os.getpid()}.{secrets.token_hex(3)}"
                path.write_text(str(time.time()))
                clear_want(model)   # it has its turn: it is not asking any more (a call still waiting asks again)
                _used(model)
        if path is None:
            time.sleep(STEP)
            if not alive():   # the manager went away: do not wait for it
                yield
                return
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
        _used(model)


@contextmanager
def hold(model):
    """Keep this model's turn for a whole task: between its calls (a tool running, a page loading) nothing takes the turn
    away, and the minutes of the group do not end it. Only a message you send in the chat or a model you load by hand
    still goes first. Does nothing without the manager."""
    if not managed(model):
        yield
        return
    HOLDS.mkdir(parents=True, exist_ok=True)
    path = HOLDS / f"{model}.{os.getpid()}.{secrets.token_hex(3)}"
    path.write_text(str(time.time()))
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
        _used(model)


def holds(model):
    return model in active()["group"]


def others_waiting(model):
    """True if a model that may not work together with this one is wanted: it should give way after its current work."""
    return any(conflict(model, m) for m in wants())


def others_idle(model):
    """True if no model that may not work together with this one has a call running or wanted, and its server has no
    call in flight."""
    import applier
    counts, w, held = lease_counts(), wants(), lease_counts(HOLDS)
    for m in MODELS:
        if not conflict(model, m):
            continue
        if counts.get(m) or held.get(m) or m in w or not applier.idle(m):
            return False
    return True


# ---------- the manager ----------
def _systemctl(*args):
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=600)


def _running(model):
    """The model's server is up or on its way up: a server that is still starting counts, or a second group could be
    started on top of it."""
    import applier
    return _systemctl("is-active", applier.UNIT[model]).stdout.strip() in ("active", "activating", "reloading")


def _stop_unit(model):
    import applier
    _systemctl("stop", applier.UNIT[model])


def _start_unit(model):
    import applier
    import settings
    s = settings.get(model)
    if s.get("model_file"):
        applier.write_env(model, s)
        _systemctl("start", "--no-block", applier.UNIT[model])


def step(now=None):
    """One look at the wants, leases and loaded models; switches groups and starts or stops model servers as needed.
    -> what it did, for the log and the tests."""
    now = now or time.time()
    did = []
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        BEAT.write_text(str(now))
    except OSError:
        return did
    stop, start = [], []
    for m in MODELS:   # a local server of a model that runs on another machine only uses memory: stop it
        if _external(m) and _running(m) and not _manual_hold(m):
            _stop_unit(m)
            did.append("stop " + m + " (it runs on another machine)")
    with _lock():
        cfg = config()
        st = active()
        loaded, since, drain = list(st["group"]), dict(st["since"]), set(st["draining"])
        w = {m: d for m, d in wants(now).items() if _has_model(m)}
        counts = lease_counts()
        held = lease_counts(HOLDS)
        running = [m for m in MODELS if not _external(m) and _running(m)]
        if not loaded and running:   # servers already up (after an update or a restart): they are the ones working now
            loaded, since = list(running), {m: now for m in running}
            did.append("adopt " + "+".join(loaded))
        # who goes first: you (a chat message, a model loaded by hand), then the Assistant, Reviewer, Sweeper, Curator
        order = sorted(w, key=lambda m: (not (w[m]["urgent"] or m == "pa"), PRIORITY.index(m), w[m]["at"]))
        in_the_way = set()
        waiting = []   # wanted models that had to wait: a lower one may not slip in front of them
        for m in order:
            if m in loaded:
                continue
            block = blockers(m, loaded)
            if not block:
                if not any(conflict(m, b) for b in waiting):
                    loaded.append(m)
                    since[m] = now
                    did.append("load " + m)
                else:
                    waiting.append(m)
                continue
            waiting.append(m)
            in_the_way |= set(block)
            first = w[m]["urgent"] or m == "pa"

            def can_go(x):
                inside = x in w or counts.get(x) or held.get(x) or used_lately(x, now)   # it still has work
                expired = now - since.get(x, now) >= cfg["turn_minutes"] * 60
                # it answered at least one call (or waited long for a server that never came up) before giving way
                served = used_after(x, since.get(x, now)) or now - since.get(x, now) > 900
                return first or not inside or (expired and served and not held.get(x))
            if all(can_go(x) for x in block):
                drain |= set(block)   # no new calls for them: their running calls finish, then this model comes
                if not any(counts.get(x) for x in block):
                    for x in block:
                        loaded.remove(x)
                        since.pop(x, None)
                        drain.discard(x)
                        stop.append(x)
                    loaded.append(m)
                    since[m] = now
                    waiting.remove(m)
                    did.append("switch to " + m)
        drain = {x for x in drain if x in loaded and x in in_the_way}
        _set_active({"group": loaded, "since": since, "draining": sorted(drain)})
        stop = [m for m in stop if m in running]   # only a server on this machine is stopped
        start = [m for m in order if m in loaded and m not in running and not _external(m)]   # wanted ones that are not up
    for m in stop:   # outside the lock: stopping a server takes a while, and callers only wait meanwhile
        _stop_unit(m)
        did.append("stop " + m)
    for m in start:
        _start_unit(m)
        did.append("start " + m)
    return did


def start():
    """Run the manager in a thread of this process (the web service). It beats every few seconds."""
    global _started
    if _started:
        return
    _started = True

    def loop():
        while True:
            try:
                step()
            except Exception as e:
                agent.log(f"turns: {type(e).__name__}: {str(e)[:100]}")
            time.sleep(STEP)
    threading.Thread(target=loop, daemon=True).start()
