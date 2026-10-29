"""Which local models are loaded together, and whose turn it is.

A model group is a set of local models that may be loaded at the same time. A model in no group is alone. The models
of one group are loaded together; any other local model waits its turn. With no groups at all, one local model is loaded
at a time. A model on an external server needs no turn: it never waits and never makes another model wait.

How it works. A call that needs a model asks for it (a "want") and holds a "lease" while it runs (thinklog.chat does
both). The manager, a thread of the web service, looks at the wants and leases every few seconds: when a model outside
the loaded group is wanted, it lets the loaded group finish what it is doing, stops the units of that group, and starts
the wanted group. A group that still has work keeps its turn for at least `turn_minutes` before it must give way; a
message you send in the chat, or a model you load by hand, does not wait for that. Callers only ever write small files
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
CONFIG = agent.STATE / "model_groups.json"
ACTIVE, BEAT, LOCK = DIR / "active.json", DIR / "beat", DIR / "lock"
MODELS = ("qwen", "gpt", "cur", "pa")
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
    groups, seen = [], set()
    for g in (d.get("groups") or []) if isinstance(d, dict) else []:
        models = [m for m in (g.get("models") or []) if m in MODELS and not (m in seen or seen.add(m))]
        if models:
            groups.append({"id": str(g.get("id") or secrets.token_hex(3)), "models": models})
    try:
        minutes = max(0, int(float((d or {}).get("turn_minutes", 10))))
    except (TypeError, ValueError):
        minutes = 10
    return {"groups": groups, "turn_minutes": minutes}


def save_config(groups, turn_minutes):
    """Store the groups. A model in two groups stays in the first. -> (config, notes about what was moved or dropped)."""
    notes, seen, out = [], {}, []
    for i, g in enumerate(groups or []):
        models = []
        for m in (g.get("models") if isinstance(g, dict) else g) or []:
            if m not in MODELS:
                continue
            if m in seen:
                notes.append(f"{NICE[m]} is in group {seen[m]} already, so it was left out of group {i + 1}: a model is in one group.")
            elif m not in models:
                models.append(m)
                seen[m] = i + 1
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
    agent.log("settings: model groups " + (", ".join("+".join(NICE[m] for m in g["models"]) for g in out) or "none (one local model at a time)"))
    return config(), notes


def group_of(model):
    for g in config()["groups"]:
        if model in g["models"]:
            return list(g["models"])
    return [model]


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
    """True if this model's calls take turns: it is local, has a model file, and the manager is running."""
    return model in MODELS and not _external(model) and _has_model(model) and alive()


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


def lease_counts():
    """-> {model: running calls}. A lease whose process is gone is removed."""
    out = {}
    for p in LEASES.glob("*") if LEASES.exists() else []:
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
    try:
        d = json.loads(ACTIVE.read_text())
    except (OSError, ValueError):
        d = {}
    return {"group": [m for m in d.get("group") or [] if m in MODELS], "since": d.get("since", 0), "draining": bool(d.get("draining"))}


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
    has it. stop(): called while waiting; raise from it to give up (a server switch or stop). Does nothing for a model
    on an external server, or without the manager."""
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
            if model in st["group"] and not st["draining"] and applier.health(model) == "ready":
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


def holds(model):
    return model in active()["group"]


def others_waiting(model):
    """True if a local model outside this model's group is wanted: it should give way after its current work."""
    mine = set(group_of(model))
    return any(m not in mine and m != model and not _external(m) for m in wants())


def others_idle(model):
    """True if no other local model has a call running or wanted, and their servers have no call in flight."""
    import applier
    counts, w = lease_counts(), wants()
    for m in MODELS:
        if m == model or _external(m):
            continue
        if counts.get(m) or m in w or not applier.idle(m):
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
        grp = list(st["group"])
        w = {m: d for m, d in wants(now).items() if not _external(m) and _has_model(m)}
        counts = lease_counts()
        running = [m for m in MODELS if not _external(m) and _running(m)]
        if not grp and running:   # servers already up (after an update or a restart): they are the loaded group
            grp = list(running)
            st = {"group": grp, "since": now, "draining": False}
            did.append("adopt " + "+".join(grp))
        order = sorted(w, key=lambda m: (not w[m]["urgent"], w[m]["at"]))
        if not grp and order:
            grp = group_of(order[0])
            st = {"group": grp, "since": now, "draining": False}
            did.append("load " + "+".join(grp))
        outsiders = [m for m in order if m not in grp]
        inside = [m for m in grp if m in w or counts.get(m) or used_lately(m, now)]   # the loaded group still has work
        if outsiders:
            expired = now - st["since"] >= cfg["turn_minutes"] * 60
            # a group answers at least one call before it gives way (or has waited a long time for a server that never came up)
            served = any(used_after(m, st["since"]) for m in grp) or now - st["since"] > 900
            if w[outsiders[0]]["urgent"] or not inside or (expired and served):
                st["draining"] = True   # no new calls for the loaded group: they finish, then the next group comes
                if not any(counts.get(m) for m in grp):
                    target = group_of(outsiders[0])
                    stop = [m for m in running if m not in target]
                    grp = list(target)
                    st = {"group": grp, "since": now, "draining": False}
                    did.append("switch to " + "+".join(grp))
        elif st["draining"]:
            st["draining"] = False
        _set_active(st)
        start = [m for m in order if m in grp and m not in running]   # wanted members of the loaded group that are not up
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
