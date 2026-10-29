"""Applies server changes: writes the env file the llama-server unit reads, then restarts that server.
A change is applied now, or queued until the server has no call running."""
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import agent
import serverflags
import settings

ENV = {"qwen": Path("/etc/memory-agent/llama-qwen.env"), "gpt": Path("/etc/memory-agent/llama-gpt.env"),
       "cur": Path("/etc/memory-agent/llama-cur.env"), "pa": Path("/etc/memory-agent/llama-pa.env")}
UNIT = {"qwen": "llama-server", "gpt": "memory-reviewer", "cur": "memory-curator-llm", "pa": "memory-assistant-llm"}
PORT = {"qwen": 8080, "gpt": 8081, "cur": 8082, "pa": 8083}
STATE = Path("/var/lib/memory-agent")
PENDING, LAST = STATE / "pending_apply.json", STATE / "last_apply.json"
KEYS = ("model_file", "context", "threads", "parallel")
WAKE = threading.Event()
_started = False


def _atomic(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_env(model, s):
    _atomic(ENV[model], f"MODEL={s['model_file']}\nCTX={s['context']}\nTHREADS={s['threads']}\nPARALLEL={s['parallel']}\nEXTRA={serverflags.build(s)}\n")


def pending():
    try:
        return json.loads(PENDING.read_text())
    except (OSError, ValueError):
        return {}


def queue(model, vals):
    p = pending()
    p[model] = {"values": vals, "at": datetime.now().isoformat(timespec="seconds")}
    _atomic(PENDING, json.dumps(p))
    WAKE.set()


def cancel(model):
    p = pending()
    if p.pop(model, None) is not None:
        _atomic(PENDING, json.dumps(p))


def last():
    try:
        return json.loads(LAST.read_text())
    except (OSError, ValueError):
        return None


def health(model):
    try:
        import remote
        if remote.enabled(model):
            return remote.health(model)
    except Exception:
        pass
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT[model]}/health", timeout=2) as r:
            return "ready" if r.status == 200 else "loading"
    except urllib.error.HTTPError:
        return "loading"
    except Exception:
        return "down"


def usable(model):
    """True if calls to this model can go ahead: it answers now, or it is local and takes turns (a call then waits for
    its turn and the model is loaded for it)."""
    if health(model) == "ready":
        return True
    try:
        import turns
        return turns.managed(model)
    except Exception:
        return False


def idle(model):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT[model]}/slots", timeout=3) as r:
            return not any(x.get("is_processing") for x in json.load(r))
    except Exception:
        return True  # server not answering: nothing running to cut off


def _active(model):
    return subprocess.run(["systemctl", "is-active", "--quiet", UNIT[model]], timeout=10).returncode == 0


def _pid(model):
    r = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", UNIT[model]],
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip()


def _wait_ready(model, old_pid, limit=600):
    """Wait for a new server process that answers /health with ready."""
    end = time.time() + limit
    while time.time() < end:
        time.sleep(2)
        if _pid(model) not in ("", "0", old_pid) and health(model) == "ready":
            return
    raise TimeoutError("server did not come back ready")


def apply_now(model, vals):
    new = settings.load()
    before = dict(new[model])
    new[model].update(vals)
    now = datetime.now().isoformat(timespec="seconds")
    try:
        settings.save(new)
        write_env(model, new[model])
        if _active(model):
            old = _pid(model)
            subprocess.run(["systemctl", "restart", "--no-block", UNIT[model]], check=True, timeout=30)
            _wait_ready(model, old)
    except Exception as e:
        new[model] = before
        try:
            settings.save(new)
            write_env(model, before)
            if _active(model):
                subprocess.run(["systemctl", "restart", "--no-block", UNIT[model]], timeout=30)
        except Exception:
            pass
        _atomic(LAST, json.dumps({"model": model, "at": now, "error": f"{type(e).__name__}: {str(e)[:100]}"}))
        try:   # say so where you will see it, not only on the Settings page
            import questions
            nice = {"qwen": "Sweeper", "gpt": "Reviewer", "cur": "Curator", "pa": "Assistant"}.get(model, model)
            questions.ask(f"The {nice} server did not start with context {vals.get('context', '?'):,} "
                          f"({vals.get('model_file', '')}), so its previous settings were put back.",
                          why=f"{type(e).__name__}: {str(e)[:150]}. Most often the model and context do not fit in memory. "
                              "Lower the context or slots, or try again; the journal of the server unit has the details.",
                          kind="serverfail", options=[{"label": "OK", "value": "ok"}],
                          payload={"model": model, "at": now, "values": {k: vals.get(k) for k in KEYS}})
        except Exception:
            pass
        raise
    _atomic(LAST, json.dumps({"model": model, "at": now, "before": {k: before[k] for k in KEYS},
                              "after": {k: new[model][k] for k in KEYS}}))
    agent.log(f"settings: restarted {UNIT[model]} with {new[model]['model_file']}, context {new[model]['context']}")


def _loop():
    while True:
        p = pending()
        if not p:
            WAKE.wait()   # sleeps until a change is queued
            WAKE.clear()
            continue
        for model, job in list(p.items()):
            if idle(model):
                try:
                    apply_now(model, job["values"])
                except Exception:
                    pass
                if pending().get(model) == job:
                    cancel(model)
        if pending():
            time.sleep(5)


def start():
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, daemon=True).start()
