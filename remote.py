"""External model servers: send one model's calls to another machine instead of the local llama-server."""
import asyncio
import ipaddress
import json
import os
import re
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


def check(model, d, warn=None):
    """Validate the settings from the page and test the server. Saves nothing. A server outside your network, a
    rejected key or a server that does not answer are warnings (added to warn): you can apply anyway, for example to
    set up a server that is not running yet. Only text that cannot be used (address, model name) is refused."""
    warn = [] if warn is None else warn
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
    return {"enabled": True, "url": url, "api": api, "model": name, "ack": ack}


def save(model, d):
    cfg = check(model, d, [])   # warnings were shown and accepted on the page before saving
    was = enabled(model)
    d = d or {}
    typed = str(d.get("key") or "").strip()
    kf = KEYDIR / f"remote-{model}.key"
    if d.get("clear_key"):
        kf.unlink(missing_ok=True)
    if typed:
        fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(typed)
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
            "model": c.get("model", ""), "ack": bool(c.get("ack")), "key_set": (KEYDIR / f"remote-{model}.key").exists()}


def register(app, authed):
    router = APIRouter()

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
