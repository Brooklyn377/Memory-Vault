"""Which models may work at the same time, and whose turn it is.

A group is a list of models that are allowed to work at the same time, wherever they run; a model may be in several.
The models working at one time must all be in one group, so models that no group lists together take turns, even on
different machines. With no groups, one model works at a time. Each piece of hardware (the host's CPU, the host's GPU,
or one external server) also has a number: the most models that may work on it at once (default 1), which groups never
raise. When several want to work, the queue goes by the priority you set (the Assistant, Reviewer, Sweeper and Curator
at first). A model that is working is never taken over: the next in the queue waits until it is idle or its turn
minutes are over, and never in the middle of an Assistant task.

How it works. A call that needs a model asks for it (a "want") and holds a "lease" while it runs (thinklog.chat does
both). The manager, a thread of the web service, looks at the wants and leases every few seconds: when a wanted model may not
work together with one that is working, it lets that one finish its running call, stops its server if it is on this
machine, and starts the wanted model. A model that still has work keeps its turn for at least `turn_minutes` before it
must give way. Callers only ever write small files in <state>/turns, so any service can use this; only the manager
starts and stops model servers.

If the manager is not running (its heartbeat is old), callers do not wait for turns: they behave as before."""
import fcntl
import json
import os
import re
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
LIMITS = agent.STATE / "hardware_limits.json"   # {hardware: how many may work at once}, set on the servers card
ACTIVE, BEAT, LOCK = DIR / "active.json", DIR / "beat", DIR / "lock"
MODELS = ("qwen", "gpt", "cur", "pa")
PRIORITY = ("pa", "gpt", "qwen", "cur")   # the default order of the queue; you can drag it into another order
NICE = {"qwen": "Sweeper", "gpt": "Reviewer", "cur": "Curator", "pa": "Assistant"}
FRESH = 30         # seconds a want counts after it was last made (a waiting call repeats it every few seconds)
QUIET = 30         # seconds after its last call that a model still counts as busy: its group is not given away between two calls
BEAT_FRESH = 20    # seconds the manager's heartbeat counts
STEP = 2           # seconds between the manager's looks
_started = False
_WAIT = {}   # the last reason logged for each model that is waiting
_UP = {}     # whether each external server answered at the last look


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
    order = [m for m in dict.fromkeys((d or {}).get("priority") or []) if m in MODELS] if isinstance(d, dict) else []
    return {"groups": groups, "turn_minutes": minutes, "priority": order + [m for m in PRIORITY if m not in order]}


def save_config(groups, turn_minutes, priority=None):
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
    order = [m for m in dict.fromkeys(priority if priority is not None else config()["priority"]) if m in MODELS]
    tmp.write_text(json.dumps({"groups": out, "turn_minutes": minutes, "priority": order + [m for m in PRIORITY if m not in order]}, indent=1))
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
    """Where the model runs: this machine's CPU or GPU, or one external server. Only models on the same hardware wait
    for each other."""
    try:
        import remote
        import serverflags
        import settings
        if not remote.enabled(model):
            return serverflags.device(settings.get(model))
        return "server " + (remote.raw(model).get("server") or server_of(model) or model)
    except Exception:
        return "cpu"


def limit(model):
    """How many models may work at the same time on this model's hardware (default 1: they take turns). The host's
    CPU and GPU numbers are in hardware_limits.json; an external server's is on its entry in the server list."""
    hw = hardware(model)
    try:
        if hw.startswith("server "):
            import remote
            return remote.server_limit(hw[7:]) if remote.get_server(hw[7:]) else max(1, int(json.loads(LIMITS.read_text()).get(hw, 1)))
        return max(1, int(json.loads(LIMITS.read_text()).get(hw, 1)))
    except (OSError, ValueError, TypeError, AttributeError):
        return 1


def set_host_limit(key, n):
    """The number of models that may work at once on this machine's "cpu" or "gpu"."""
    if key not in ("cpu", "gpu"):
        raise ValueError("cpu or gpu")
    try:
        n = max(1, int(float(n)))
    except (TypeError, ValueError):
        raise ValueError("how many models may work at the same time must be a number")
    try:
        d = json.loads(LIMITS.read_text())
    except (OSError, ValueError):
        d = {}
    d[key] = n
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = LIMITS.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    os.replace(tmp, LIMITS)
    agent.log(f"settings: up to {n} model(s) at the same time on this server's {key.upper()}")


def host_limits():
    try:
        d = json.loads(LIMITS.read_text())
    except (OSError, ValueError):
        d = {}
    return {k: max(1, int(d.get(k, 1))) if str(d.get(k, 1)).lstrip("-").isdigit() else 1 for k in ("cpu", "gpu")}


def together(a, b):
    return any(a in g["models"] and b in g["models"] for g in config()["groups"])


def blockers(model, loaded):
    """The loaded models that must finish before this one can work. The models working at one time must all be in one
    group (wherever they run), and no hardware may have more working than its number says. [] means it fits."""
    others = [x for x in loaded if x != model]
    if not others:
        return []
    best = max((g["models"] for g in config()["groups"] if model in g["models"]), key=lambda g: sum(x in g for x in others), default=[])
    keep, used = [], {hardware(model): 1}
    for x in others:
        hw = hardware(x)
        if x in best and used.get(hw, 0) < limit(x):
            keep.append(x)
            used[hw] = used.get(hw, 0) + 1
    return [x for x in others if x not in keep]


def conflict(a, b):
    """True if the two models may not work at the same time: no group lists them together, or they share hardware that
    takes one model at a time."""
    return a != b and (not together(a, b) or (hardware(a) == hardware(b) and limit(a) <= 1))


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
def want(model):
    """This model is needed now. Repeat while waiting: a want older than FRESH seconds is forgotten."""
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / f".want-{model}.{os.getpid()}"
    tmp.write_text(json.dumps({"at": time.time()}))
    os.replace(tmp, DIR / f"want-{model}")


def wants(now=None):
    """-> {model: {"at"}} for models wanted lately, plus the ones held on purpose: the Curator while it holds a
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
            out.setdefault("cur", {"at": now})
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
    why = d.get("why") if isinstance(d.get("why"), dict) else {}
    return {"group": models, "since": {m: float(since.get(m, 0)) for m in models},
            "draining": [m for m in draining if m in models], "why": {m: str(t) for m, t in why.items() if m in MODELS}}


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
def lease(model, stop=None):
    """Hold this model's turn while a call runs. Waits (asking for the turn) until the model is loaded in the group that
    has it. stop(): called while waiting; raise from it to give up (a server switch or stop). Does nothing without the manager."""
    import applier
    if not managed(model):
        yield
        return
    _dirs()
    path = None
    while path is None:
        want(model)
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
    away, and the turn minutes do not end it. Does nothing without the manager."""
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
        loaded, since, drain = list(st["group"]), dict(st["since"]), set()   # who is finishing up is worked out afresh each time
        w = {m: d for m, d in wants(now).items() if _has_model(m)}
        counts = lease_counts()
        held = lease_counts(HOLDS)
        running = [m for m in MODELS if not _external(m) and _running(m)]
        if not loaded and running:   # servers already up (after an update or a restart): they are the ones working now
            loaded, since = list(running), {m: now for m in running}
            did.append("adopt " + "+".join(loaded))
        # the queue: by the priority you set (Assistant, Reviewer, Sweeper, Curator at first). A higher one waits for a working one
        order = sorted(w, key=lambda m: (cfg["priority"].index(m), w[m]["at"]))
        in_the_way = set()
        why = {}       # model -> why it has to wait, for the log
        detail = {}    # the same with what each model in the way is doing now, for the pages
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
                    why[m] = "waits in the queue behind " + ", ".join(NICE[b] for b in waiting if conflict(m, b) and b != m)
                    detail[m] = why[m]
                continue
            waiting.append(m)
            in_the_way |= set(block)
            why[m] = "waits for " + ", ".join(NICE[b] for b in block) + " to finish (no group lists them together, or the hardware takes one at a time)"
            detail[m] = "waits for " + "; ".join(f"{NICE[b]} ({_doing(b, w, counts, held, since, now, cfg)})" for b in block)

            def can_go(x):
                inside = x in w or counts.get(x) or held.get(x) or used_lately(x, now)   # it still has work
                expired = now - since.get(x, now) >= cfg["turn_minutes"] * 60
                # it answered at least one call (or waited long for a server that never came up) before giving way
                served = used_after(x, since.get(x, now)) or now - since.get(x, now) > 900
                return not inside or (expired and served and not held.get(x))
            if all(can_go(x) for x in block):
                drain |= set(block)   # no new calls for them: their running calls finish, then this model comes
                busy = [NICE[y] for y in block if counts.get(y)]
                for x in block:   # what a model that is being drained is told, since it is not the one waiting
                    detail.setdefault(x, f"stops taking new calls so the {NICE[m]} can have its turn"
                                         + (f" (waiting for the {' and '.join(busy)} to finish a call)" if busy else ""))
                if not any(counts.get(x) for x in block):
                    for x in block:
                        loaded.remove(x)
                        since.pop(x, None)
                        drain.discard(x)
                        stop.append(x)
                    loaded.append(m)
                    since[m] = now
                    waiting.remove(m)
                    why.pop(m, None)
                    did.append(f"switch to {m} (after {', '.join(block)} finished)")
        for m in [m for m in _WAIT if m not in why]:
            _WAIT.pop(m)
        for m, r in why.items():   # a wait is logged once, and again only when its reason changes
            if _WAIT.get(m) != r:
                _WAIT[m] = r
                agent.log(f"turns: the {NICE[m]} {r}")
        drain = {x for x in drain if x in loaded and x in in_the_way}
        _set_active({"group": loaded, "since": since, "draining": sorted(drain), "why": detail})
        stop = [m for m in stop if m in running]   # only a server on this machine is stopped
        start = [m for m in order if m in loaded and m not in running and not _external(m)]   # wanted ones that are not up
    for m in stop:   # outside the lock: stopping a server takes a while, and callers only wait meanwhile
        _stop_unit(m)
        did.append("stop " + m)
    for m in start:
        _start_unit(m)
        did.append("start " + m)
    if did:
        agent.log("turns: " + re.sub(r"\b(qwen|gpt|cur|pa)\b", lambda k: NICE[k.group(1)], "; ".join(did)))
    return did


def _doing(x, w, counts, held, since, now, cfg):
    """What a model that is in the way is doing, in words, for the pages."""
    left = max(0, cfg["turn_minutes"] * 60 - (now - since.get(x, now)))
    if counts.get(x):
        what = "running a call"
    elif held.get(x):
        what = "in the middle of an Assistant task"
    elif x in w:
        what = "asking for work"
    elif used_lately(x, now):
        what = "just finished a call"
    else:
        what = "idle, so it gives way"
    return what + (f", its turn lasts {int(left // 60)} more min" if left and (counts.get(x) or x in w) else "")


def _watch_servers():
    """Log when an external server stops answering or answers again, so the History tab has the times (a machine that
    went to sleep or restarted, a llama-server that died)."""
    import applier
    import remote
    for m in MODELS:
        if not _external(m):
            _UP.pop(m, None)
            continue
        h = applier.health(m)
        up = h == "ready"
        if m in _UP and _UP[m] != up:
            sid = remote.raw(m).get("server")
            name = (remote.get_server(sid) or {}).get("name") if sid else None
            agent.log(f"health: the {NICE[m]}'s server" + (f" {name}" if name else "") + (" is answering again" if up else f" is not answering ({h})"))
        _UP[m] = up


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

    def health_loop():
        while True:
            try:
                _watch_servers()
            except Exception as e:
                agent.log(f"turns: {type(e).__name__}: {str(e)[:100]}")
            time.sleep(15)
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=health_loop, daemon=True).start()
