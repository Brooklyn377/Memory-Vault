"""External model servers: send one model's calls to another machine instead of the local llama-server."""
import asyncio
import ipaddress
import json
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

import agent
import settings

KEYDIR = Path("/etc/memory-agent")
PORT = {"qwen": 8080, "gpt": 8081, "cur": 8082, "pa": 8083}
UNIT = {"qwen": "llama-server", "gpt": "memory-reviewer", "cur": "memory-curator-llm", "pa": "memory-assistant-llm"}
NICE = {"qwen": "Sweeper", "gpt": "Reviewer", "cur": "Curator", "pa": "Assistant"}
ON_DEMAND = ("cur", "pa")   # no local server is kept running for these: nothing to wait for or start when going back
NAME = re.compile(r"[\w.:/@\-]{1,200}")
_cache = {}


def _cached(name, secs, fn):
    hit = _cache.get(name)
    if hit and time.time() - hit[0] < secs:
        return hit[1]
    val = fn()
    _cache[name] = (time.time(), val)
    return val


def raw(model):
    try:
        return dict(settings.get(model).get("remote") or {})
    except Exception:
        return {}


def saved_key(model):
    try:
        return (KEYDIR / f"remote-{model}.key").read_text().strip()
    except OSError:
        return ""


def local_ready(model):
    def probe_local():
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT[model]}/health", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False
    return _cached(("local", model), 5, probe_local)


def enabled(model):
    """True while this model's calls go to the external server. Switching back is immediate: a "back" left by an older
    version (calls stayed external until the local model loaded) counts as local."""
    c = raw(model)
    return bool(c.get("enabled") and c.get("url") and not c.get("back"))


def base(model):
    return str(raw(model).get("url", "")).rstrip("/")


def endpoint(model):
    return base(model) + "/v1"


def _get(url, keyv):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + keyv} if keyv else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(200000)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return 0, b""


def probe(url, api, keyv=""):
    """-> (state, model names). state is ready, loading, key or down."""
    names = []
    code, body = _get(url + "/v1/models", keyv)
    if code == 200:
        try:
            names = [str(x.get("id")) for x in (json.loads(body).get("data") or []) if isinstance(x, dict)][:20]
        except ValueError:
            pass
    if api == "llamacpp":
        hc, _ = _get(url + "/health", keyv)
        state = "ready" if hc == 200 else "loading" if hc == 503 else "down"
        if state == "down" and code == 200:
            state = "ready"
    else:
        state = "ready" if code == 200 else "key" if code in (401, 403) else "down"
    return state, names


def runner_url(model):
    """The model runner on the external machine (model_runner.py), if you added one: it lets this page manage it."""
    return str(raw(model).get("runner", "")).rstrip("/")


def runner_key(model):
    try:
        return (KEYDIR / f"runner-{model}.key").read_text().strip()
    except OSError:
        return ""


def managed(model):
    """True while this model runs on an external server whose settings this page controls through its runner."""
    return enabled(model) and bool(runner_url(model))


def runner(model, method, path, body=None, timeout=30, url=None, key=None):
    """Call the runner. -> its JSON reply. Raises ValueError with a plain reason when it cannot be used."""
    url, key = (url or runner_url(model)).rstrip("/"), key if key is not None else runner_key(model)
    req = urllib.request.Request(url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            why = json.loads(e.read() or b"{}").get("error") or e.reason
        except ValueError:
            why = e.reason
        raise ValueError("the runner rejected the token" if e.code == 401 else f"the runner says: {why}")
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot reach the runner at {url} ({type(e).__name__})")


def health(model):
    c = raw(model)
    if not c.get("url"):
        return "down"
    api = c.get("api", "llamacpp")
    return _cached(("health", model), 5, lambda: probe(base(model), api, saved_key(model))[0])


def is_private(host):
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        h = host.lower()
        return "." not in h or h.endswith((".lan", ".local", ".home", ".internal", ".home.arpa", ".ts.net"))


def norm_url(u):
    u = str(u or "").strip()
    if not u:
        raise ValueError("type the server address, for example http://192.168.1.50:8080")
    if "://" not in u:
        u = "http://" + u
    p = urllib.parse.urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("use an address like http://192.168.1.50:8080")
    path = p.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urllib.parse.urlunparse((p.scheme, p.netloc, path, "", "", "")).rstrip("/")


# ---------- presets: saved external servers any model's card can pick ----------
PRESETS = agent.STATE / "server_presets.json"
PRESET_ID = re.compile(r"[a-f0-9]{8}")


def _secret(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def _presets():
    try:
        d = json.loads(PRESETS.read_text())
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _preset_key(pid, kind):
    try:
        return (KEYDIR / f"preset-{pid}-{kind}.key").read_text().strip()
    except OSError:
        return ""


def presets():
    """For the page: the presets without their keys, only whether each has one."""
    return [dict(p, key_set=bool(_preset_key(p["id"], "api")), runner_key_set=bool(_preset_key(p["id"], "runner")))
            for p in _presets()]


def save_preset(model, d):
    """Save the external server typed on this model's card as a preset. Keys: the typed ones, else this model's saved
    ones, stored root-only like every key. -> the preset (without keys)."""
    name = " ".join(str(d.get("name") or "").split())[:60]
    if not name:
        raise ValueError("give the preset a name")
    items = _presets()
    cur = str(d.get("preset") or "")   # the preset chosen on the card: saving updates it (a new name renames it)
    same = next((p for p in items if p["name"].lower() == name.lower()), None)
    if same and same["id"] != cur and any(p["id"] == cur for p in items):
        raise ValueError(f"there is already a preset called {same['name']}")
    pid = cur if any(p["id"] == cur for p in items) else same["id"] if same else secrets.token_hex(4)
    api = d.get("api") if d.get("api") in ("llamacpp", "openai") else "llamacpp"
    mname = str(d.get("model") or "").strip()
    if mname and not NAME.fullmatch(mname):
        raise ValueError("the model name has odd characters")
    p = {"id": pid, "name": name, "url": norm_url(d.get("url")), "api": api, "model": mname, "ack": bool(d.get("ack")),
         "runner": norm_url(d["runner"]) if str(d.get("runner") or "").strip() else ""}
    key = str(d.get("key") or "").strip() or (_preset_key(d.get("preset"), "api") if d.get("preset") else "") or saved_key(model)
    rkey = (str(d.get("runner_key") or "").strip() or (_preset_key(d.get("preset"), "runner") if d.get("preset") else "")
            or runner_key(model))
    KEYDIR.mkdir(parents=True, exist_ok=True)
    for kind, val in (("api", key), ("runner", rkey)):
        f = KEYDIR / f"preset-{pid}-{kind}.key"
        if val:
            _secret(f, val)
        else:
            f.unlink(missing_ok=True)
    _store_presets([x for x in items if x["id"] != pid] + [p])
    agent.log(f"settings: server preset {name} saved")
    return dict(p, key_set=bool(key), runner_key_set=bool(rkey))


def _store_presets(items):
    tmp = PRESETS.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1))
    os.replace(tmp, PRESETS)


def delete_preset(pid):
    """Remove a preset and its keys. Models that use it keep their settings and keys."""
    if not PRESET_ID.fullmatch(str(pid)):
        raise ValueError("unknown preset")
    _store_presets([p for p in _presets() if p["id"] != pid])
    for kind in ("api", "runner"):
        (KEYDIR / f"preset-{pid}-{kind}.key").unlink(missing_ok=True)


def with_preset(d):
    """The card's remote settings with a chosen preset's keys filled in where none was typed, so check() and save()
    use them as if typed: save() then stores them for that model, like every key."""
    d = dict(d or {})
    pid = str(d.get("preset") or "")
    if not PRESET_ID.fullmatch(pid) or not any(p["id"] == pid for p in _presets()):
        d.pop("preset", None)
        return d
    if not str(d.get("key") or "").strip() and not d.get("clear_key"):
        d["key"] = _preset_key(pid, "api")
    if not str(d.get("runner_key") or "").strip() and not d.get("runner_clear"):
        d["runner_key"] = _preset_key(pid, "runner")
    return d


def check(model, d, warn=None):
    """Validate the settings from the page and test the server. Saves nothing. A server outside your network, a
    rejected key or a server that does not answer are warnings (added to warn): you can apply anyway, for example to
    set up a server that is not running yet. Only text that cannot be used (address, model name) is refused."""
    warn = [] if warn is None else warn
    d = with_preset(d)
    if model not in PORT:
        raise ValueError("unknown server")
    d = d or {}
    if not d.get("enabled"):
        return {"enabled": False}
    url = norm_url(d.get("url"))
    api = d.get("api") if d.get("api") in ("llamacpp", "openai") else "llamacpp"
    name = str(d.get("model") or "").strip()
    if name and not NAME.fullmatch(name):
        raise ValueError("the model name has odd characters")
    if api == "openai" and not name:
        raise ValueError(f"{NICE[model]}: this kind of server needs the model name")
    host = urllib.parse.urlparse(url).hostname
    ack = bool(d.get("ack"))
    if not is_private(host) and not ack:
        warn.append(f"{NICE[model]}: {host} looks like a server outside your network. Your notes and chats would be sent to it.")
    typed = str(d.get("key") or "").strip()
    keyv = typed or ("" if d.get("clear_key") else saved_key(model))
    state, _ = probe(url, api, keyv)
    if state == "key":
        warn.append(f"{NICE[model]}: the server rejected the API key; calls to it will fail until the key is right.")
    elif state == "down":
        warn.append(f"{NICE[model]}: cannot reach {url} right now; calls to it will fail until it answers.")
    run_url = str(d.get("runner") or "").strip()
    if run_url:
        run_url = norm_url(run_url)
        run_key = str(d.get("runner_key") or "").strip() or ("" if d.get("runner_clear") else runner_key(model))
        try:
            runner(model, "GET", "/status", url=run_url, key=run_key, timeout=10)
        except ValueError as e:
            warn.append(f"{NICE[model]}: {e}; its settings cannot be changed from here until it answers.")
    return {"enabled": True, "url": url, "api": api, "model": name, "ack": ack, "runner": run_url, "preset": d.get("preset", "")}


def save(model, d):
    d = with_preset(d)          # a chosen preset's keys count as typed: stored for this model below
    cfg = check(model, d, [])   # warnings were shown and accepted on the page before saving
    was = enabled(model)
    typed = str(d.get("key") or "").strip()
    kf = KEYDIR / f"remote-{model}.key"
    if d.get("clear_key"):
        kf.unlink(missing_ok=True)
    if typed:
        fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(typed)
    rk, rtyped = KEYDIR / f"runner-{model}.key", str(d.get("runner_key") or "").strip()
    if d.get("runner_clear"):
        rk.unlink(missing_ok=True)
    if rtyped:
        fd = os.open(rk, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(rtyped)
    s = settings.load()
    old = dict(s[model].get("remote") or {})
    new = {**old, **cfg} if cfg["enabled"] else dict(old, enabled=False)
    new.pop("back", None)
    s[model]["remote"] = new
    settings.save(s)
    _cache.clear()
    if cfg["enabled"] != was or (cfg["enabled"] and (old.get("url"), old.get("model")) != (new.get("url"), new.get("model"))):
        import thinklog
        thinklog.abort(model)   # a call running on the old server is cut off now; its work is queued again
        (agent.STATE / f"model_off_{model}").unlink(missing_ok=True)   # switching means: use the new server now
    if cfg["enabled"] and not was:
        subprocess.run(["systemctl", "stop", "--no-block", UNIT[model]], timeout=30)
        agent.log(f"settings: {NICE[model]} now uses the external server {cfg['url']}")
    elif not cfg["enabled"] and was:
        if model not in ON_DEMAND:
            subprocess.run(["systemctl", "start", "--no-block", UNIT[model]], timeout=30)
        agent.log(f"settings: {NICE[model]} switched back to its local model")


def public(model):
    c = raw(model)
    return {"enabled": enabled(model), "url": c.get("url", ""), "api": c.get("api", "llamacpp"),
            "model": c.get("model", ""), "ack": bool(c.get("ack")), "key_set": (KEYDIR / f"remote-{model}.key").exists(),
            "runner": c.get("runner", ""), "runner_key_set": (KEYDIR / f"runner-{model}.key").exists(),
            "preset": c.get("preset", "")}


def register(app, authed):
    router = APIRouter()

    @router.get("/api/remote/presets")
    def preset_list(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        return {"presets": presets()}

    @router.post("/api/remote/presets/save")
    async def preset_save(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        d = await request.json()
        try:
            if str(d.get("model_key", "")) not in PORT:
                raise ValueError("unknown server")
            return {"ok": True, "preset": save_preset(d["model_key"], d)}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/api/remote/presets/delete")
    async def preset_delete(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        try:
            delete_preset((await request.json()).get("id"))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @router.post("/api/remote/test")
    async def test(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        d = await request.json()
        try:
            m = str(d.get("model_key", ""))
            if m not in PORT:
                raise ValueError("unknown server")
            url = norm_url(d.get("url"))
            api = d.get("api") if d.get("api") in ("llamacpp", "openai") else "llamacpp"
            keyv = str(d.get("key") or "").strip() or saved_key(m)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        state, names = await asyncio.to_thread(probe, url, api, keyv)
        return {"state": state, "models": names}

    app.include_router(router)
