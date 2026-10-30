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


# ---------- servers: the one list of external servers; any model's card can pick one ----------
SERVERS = agent.STATE / "servers.json"
OLD_PRESETS = agent.STATE / "server_presets.json"   # before the server list: merged into it once, then kept as .bak
NOTICE = agent.STATE / "servers_merge_notice.json"
SERVER_ID = re.compile(r"[a-f0-9]{8}")


def _secret(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def _servers():
    try:
        d = json.loads(SERVERS.read_text())
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _server_key(sid, kind):
    try:
        return (KEYDIR / f"server-{sid}-{kind}.key").read_text().strip()
    except OSError:
        return ""


def servers():
    """For the page: the servers without their keys, only whether each has one."""
    return [dict(p, limit=server_limit(p["id"]), key_set=bool(_server_key(p["id"], "api")),
                 runner_key_set=bool(_server_key(p["id"], "runner"))) for p in _servers()]


def get_server(sid):
    return next((p for p in _servers() if p["id"] == sid), None)


def server_limit(sid):
    """How many models may work at the same time on this server (default 1)."""
    try:
        return max(1, int(get_server(sid).get("limit", 1)))
    except (AttributeError, TypeError, ValueError):
        return 1


def save_server(d):
    """Add a server, or update the one with d["id"]. Keys: the typed ones, else the ones saved. Models that use it
    are updated to match. -> the server (without keys)."""
    name = " ".join(str(d.get("name") or "").split())[:60]
    if not name:
        raise ValueError("give the server a name")
    items = _servers()
    cur = str(d.get("id") or "")
    same = next((p for p in items if p["name"].lower() == name.lower()), None)
    if same and same["id"] != cur:
        raise ValueError(f"there is already a server called {same['name']}")
    sid = cur if any(p["id"] == cur for p in items) else secrets.token_hex(4)
    api = d.get("api") if d.get("api") in ("llamacpp", "openai") else "llamacpp"
    try:
        limit = max(1, int(float(d.get("limit", 1))))
    except (TypeError, ValueError):
        raise ValueError("how many models may work at the same time must be a number")
    p = {"id": sid, "name": name, "url": norm_url(d.get("url")), "api": api, "ack": bool(d.get("ack")), "limit": limit,
         "runner": norm_url(d["runner"]) if str(d.get("runner") or "").strip() else ""}
    KEYDIR.mkdir(parents=True, exist_ok=True)
    for kind, typed, clear in (("api", d.get("key"), d.get("clear_key")), ("runner", d.get("runner_key"), d.get("runner_clear"))):
        f = KEYDIR / f"server-{sid}-{kind}.key"
        if str(typed or "").strip():
            _secret(f, str(typed).strip())
        elif clear:
            f.unlink(missing_ok=True)
    _store_servers([x for x in items if x["id"] != sid] + [p])
    agent.log(f"settings: server {name} saved")
    sync_models(sid)
    return dict(p, key_set=bool(_server_key(sid, "api")), runner_key_set=bool(_server_key(sid, "runner")))


def _store_servers(items):
    tmp = SERVERS.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1))
    os.replace(tmp, SERVERS)


def delete_server(sid):
    """Remove a server and its keys. Models that use it keep their settings and keys but no longer point at the list."""
    if not SERVER_ID.fullmatch(str(sid)):
        raise ValueError("unknown server")
    _store_servers([p for p in _servers() if p["id"] != sid])
    for kind in ("api", "runner"):
        (KEYDIR / f"server-{sid}-{kind}.key").unlink(missing_ok=True)
    s = settings.load()
    for m in PORT:
        if (s[m].get("remote") or {}).get("server") == sid:
            s[m]["remote"].pop("server", None)
    settings.save(s)
    _cache.clear()


def with_server(d):
    """The card's remote settings with the chosen server's address, type, runner and keys filled in: check() and save()
    then use them as if typed, and save() stores them for that model like every key. The model name stays the card's."""
    d = dict(d or {})
    sid = str(d.get("server") or "")
    p = get_server(sid) if SERVER_ID.fullmatch(sid) else None
    if not p:
        d.pop("server", None)
        return d
    d.update(url=p["url"], api=p["api"], ack=p["ack"], runner=p["runner"], clear_key=False, runner_clear=False,
             key=_server_key(sid, "api"), runner_key=_server_key(sid, "runner"))
    return d


def sync_models(sid):
    """After a server changed: every model that uses it gets the new address, type, runner and keys."""
    s = settings.load()
    for m in PORT:
        r = s[m].get("remote") or {}
        if r.get("server") == sid and r.get("enabled"):
            try:
                save(m, dict(r, **{"enabled": True, "server": sid}))
            except ValueError as e:
                agent.log(f"settings: {NICE[m]} could not follow the changed server {sid}: {e}")


def server_runner(sid, method, path, body=None, timeout=30):
    """Call the runner of a server in the list. Raises ValueError with a plain reason."""
    p = get_server(sid)
    if not p or not p.get("runner"):
        raise ValueError("this server has no runner: add its runner address on the Servers card")
    return runner("qwen", method, path, body, timeout, url=p["runner"], key=_server_key(sid, "runner"))


def models_using(sid, name):
    """The models that run this model file on this server (their saved model name on it)."""
    s = settings.load()
    out = []
    for m in PORT:
        r = s[m].get("remote") or {}
        if r.get("enabled") and r.get("server") == sid and str(r.get("model") or "").replace("\\", "/").rsplit("/", 1)[-1] == name:
            out.append(NICE[m])
    return out


def merge_presets():
    """Once, after the update that brings the server list: the old presets and every external server typed on a model's
    card become servers in the list, and each model points at its server. The old preset file is kept as a backup.
    -> a short account of what was moved, or None when there was nothing to do."""
    if SERVERS.exists():
        return None
    old = []
    try:
        d = json.loads(OLD_PRESETS.read_text())
        old = d if isinstance(d, list) else []
    except (OSError, ValueError):
        pass
    out, notes = [], []
    for p in old:
        out.append({"id": p["id"], "name": p["name"], "url": p["url"], "api": p.get("api", "llamacpp"), "ack": bool(p.get("ack")),
                    "runner": p.get("runner", ""), "limit": 1})
        for kind in ("api", "runner"):
            src = KEYDIR / f"preset-{p['id']}-{kind}.key"
            if src.exists():
                _secret(KEYDIR / f"server-{p['id']}-{kind}.key", src.read_text())
        notes.append(f"The preset {p['name']} is now a server.")
    s = settings.load()
    for m in PORT:
        r = s[m].get("remote") or {}
        if not (r.get("enabled") and r.get("url")):
            continue
        pid = str(r.pop("preset", "") or "")
        hit = next((x for x in out if x["id"] == pid), None) or next((x for x in out if x["url"] == r["url"] and x["runner"] == r.get("runner", "")), None)
        if not hit:
            host = urllib.parse.urlparse(r["url"]).netloc
            name, n = host, 2
            while any(x["name"].lower() == name.lower() for x in out):
                name, n = f"{host} ({n})", n + 1
            hit = {"id": secrets.token_hex(4), "name": name, "url": r["url"], "api": r.get("api", "llamacpp"), "ack": bool(r.get("ack")),
                   "runner": r.get("runner", ""), "limit": 1}
            for kind, src in (("api", KEYDIR / f"remote-{m}.key"), ("runner", KEYDIR / f"runner-{m}.key")):
                if src.exists():
                    _secret(KEYDIR / f"server-{hit['id']}-{kind}.key", src.read_text())
            out.append(hit)
            notes.append(f"The {NICE[m]}'s external server {r['url']} was saved as the server {name}.")
        r["server"] = hit["id"]
        s[m]["remote"] = r
        notes.append(f"The {NICE[m]} now uses the server {hit['name']}.")
    for m in PORT:   # a model that is back on its local model: drop the old preset pointer
        (s[m].get("remote") or {}).pop("preset", None)
    settings.save(s)
    _store_servers(out)
    if old and OLD_PRESETS.exists():
        OLD_PRESETS.rename(OLD_PRESETS.with_name(OLD_PRESETS.name + ".bak"))
    if notes:
        NOTICE.write_text(json.dumps(notes))
        agent.log("settings: external server presets merged into the server list")
    return notes


def check(model, d, warn=None):
    """Validate the settings from the page and test the server. Saves nothing. A server outside your network, a
    rejected key or a server that does not answer are warnings (added to warn): you can apply anyway, for example to
    set up a server that is not running yet. Only text that cannot be used (address, model name) is refused."""
    warn = [] if warn is None else warn
    d = with_server(d)
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
    return {"enabled": True, "url": url, "api": api, "model": name, "ack": ack, "runner": run_url, "server": d.get("server", "")}


def save(model, d):
    d = with_server(d)          # a chosen server's address and keys count as typed: stored for this model below
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
            "server": c.get("server", "")}


def register(app, authed):
    router = APIRouter()
    try:
        merge_presets()   # once, after the update that brings the server list
    except Exception as e:
        agent.log(f"settings: merging the server presets failed ({type(e).__name__}: {str(e)[:100]})")

    @router.get("/api/servers")
    def server_list(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        try:
            notice = json.loads(NOTICE.read_text())
        except (OSError, ValueError):
            notice = []
        return {"servers": servers(), "notice": notice}

    @router.post("/api/servers/save")
    async def server_save(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        d = await request.json()
        try:
            return {"ok": True, "server": await asyncio.to_thread(save_server, d)}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/api/servers/delete")
    async def server_delete(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        try:
            delete_server((await request.json()).get("id"))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    def need(request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    async def to_runner(sid, method, path, body=None):
        try:
            return await asyncio.to_thread(server_runner, sid, method, path, body)
        except ValueError as e:
            if "third_party:" in str(e):
                raise HTTPException(status_code=409, detail="third_party:" + str(e).split("third_party:")[1])
            raise HTTPException(status_code=400, detail=str(e))

    @router.get("/api/servers/models")
    async def server_models(request: Request, id: str = ""):
        need(request)
        st = await to_runner(id, "GET", "/status")
        return {"models": [dict(m, use=models_using(id, m["name"])) for m in st.get("models") or []], "downloads": st.get("downloads") or []}

    @router.post("/api/servers/download")
    async def server_download(request: Request):
        need(request)
        d = await request.json()
        await to_runner(str(d.get("id", "")), "POST", "/download", {"url": str(d.get("url", "")), "confirmed": bool(d.get("confirmed"))})
        return {"ok": True}

    @router.post("/api/servers/download/cancel")
    async def server_download_cancel(request: Request):
        need(request)
        d = await request.json()
        await to_runner(str(d.get("id", "")), "POST", "/download/cancel", {"name": str(d.get("name", ""))})
        return {"ok": True}

    @router.post("/api/servers/models/delete")
    async def server_model_delete(request: Request):
        need(request)
        d = await request.json()
        sid, name = str(d.get("id", "")), str(d.get("name", ""))
        await to_runner(sid, "POST", "/models/delete", {"name": name, "force": True})   # the page warned about models that use it
        agent.log(f"settings: deleted the model file {name} on the server {sid}")
        return {"ok": True}

    @router.post("/api/servers/notice/dismiss")
    async def notice_dismiss(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        NOTICE.unlink(missing_ok=True)
        return {"ok": True}

    @router.post("/api/remote/test")
    async def test(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        d = await request.json()
        try:
            url = norm_url(d.get("url"))
            api = d.get("api") if d.get("api") in ("llamacpp", "openai") else "llamacpp"
            keyv = str(d.get("key") or "").strip() or (_server_key(d["id"], "api") if SERVER_ID.fullmatch(str(d.get("id") or "")) else "")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        state, names = await asyncio.to_thread(probe, url, api, keyv)
        return {"state": state, "models": names}

    app.include_router(router)
