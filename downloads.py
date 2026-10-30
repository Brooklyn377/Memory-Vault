"""Model downloads for the Settings page. Hugging Face links start directly; any other host needs your OK."""
import json
import os
import re
import shutil
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

import agent

MODELS = Path("/opt/models")
STATE = Path("/var/lib/memory-agent/downloads")
HF = ("huggingface.co", "hf.co")
CHUNK = 1024 * 1024
SAVE_EVERY = 8 * CHUNK
LIVE, STOP = {}, set()


def _path(name):
    return STATE / f"{name}.json"


def _save(st):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = _path(st["name"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, _path(st["name"]))


def _is_hf(host):
    return host in HF or host.endswith(".huggingface.co")


def _run(st):
    name, part = st["name"], MODELS / (st["name"] + ".part")
    try:
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(st["url"], headers={"Range": f"bytes={have}-"} if have else {})
        with urllib.request.urlopen(req, timeout=60) as r:
            if have and r.status != 206:
                have = 0
            total = have + int(r.headers.get("Content-Length") or 0)
            if shutil.disk_usage(MODELS).free < total - have:
                raise OSError("not enough free disk space")
            st.update(status="running", done=have, total=total, error="")
            _save(st)
            since = 0
            with open(part, "ab" if have else "wb") as f:
                while name not in STOP:
                    buf = r.read(CHUNK)
                    if not buf:
                        break
                    f.write(buf)
                    st["done"] += len(buf)
                    since += len(buf)
                    if since >= SAVE_EVERY:
                        since = 0
                        _save(st)
        if name in STOP:
            part.unlink(missing_ok=True)
            st.update(status="cancelled")
        elif total and st["done"] < total:
            raise OSError("connection closed early")
        else:
            os.replace(part, MODELS / name)
            st.update(status="done")
            agent.log(f"downloads: {name} downloaded ({st['done'] / 1e9:.2f} GB)")
    except Exception as e:
        st.update(status="failed", error=f"{type(e).__name__}: {str(e)[:120]}")
        agent.log(f"downloads: {name} failed: {st['error']}")
    finally:
        STOP.discard(name)
        LIVE.pop(name, None)
        if st["status"] in ("done", "cancelled"):
            _path(name).unlink(missing_ok=True)
        else:
            _save(st)


def start(url, confirmed):
    u = urllib.parse.urlparse(url.strip())
    if u.scheme != "https" or not u.hostname:
        raise ValueError("use a full https:// link")
    name = urllib.parse.unquote(Path(u.path).name)
    if not re.fullmatch(r"[\w.\-]+\.gguf", name):
        raise ValueError("the link must end in a .gguf file name")
    host = u.hostname.lower()
    if not _is_hf(host) and not confirmed:
        raise PermissionError(host)
    if (MODELS / name).exists():
        raise ValueError(name + " already exists")
    if name in LIVE:
        raise ValueError("already downloading")
    path = u.path.replace("/blob/", "/resolve/", 1) if _is_hf(host) else u.path
    st = {"name": name, "url": urllib.parse.urlunparse(u._replace(path=path)), "host": host,
          "third_party": not _is_hf(host), "status": "starting", "done": 0, "total": 0, "error": ""}
    _save(st)
    agent.log(f"downloads: {name} started from {host}")
    t = threading.Thread(target=_run, args=(st,), daemon=True)
    LIVE[name] = t
    t.start()


SPLIT = re.compile(r"(.*)-\d{5}-of-(\d{5})\.gguf")


def group(name):
    """All files that make up one model: every part of a split model, or just the file."""
    m = SPLIT.fullmatch(name)
    if m:
        return sorted(p.name for p in MODELS.glob(f"{m.group(1)}-*-of-{m.group(2)}.gguf"))
    return [name]


def in_use():
    """File name -> which servers still use it: saved settings, queued changes and the env files."""
    import settings
    nice = {"qwen": "the Sweeper", "gpt": "the Reviewer", "cur": "the Curator", "pa": "the Assistant"}
    used = {}

    def add(f, who):
        for g in group(f):
            used.setdefault(g, set()).add(who)

    for k, v in settings.load().items():
        add(v["model_file"], nice.get(k, k))
        dm = (v.get("flags") or {}).get("draft_model")
        if dm:
            add(dm, nice.get(k, k) + " (draft model)")
    try:
        for k, job in json.loads(Path("/var/lib/memory-agent/pending_apply.json").read_text()).items():
            add(job["values"]["model_file"], nice.get(k, k) + " (queued change)")
    except (OSError, ValueError, KeyError):
        pass
    for k, f in (("qwen", "llama-qwen"), ("gpt", "llama-gpt"), ("cur", "llama-cur")):
        try:
            for line in Path(f"/etc/memory-agent/{f}.env").read_text().splitlines():
                if line.startswith("MODEL="):
                    add(line[6:].strip(), nice[k])
        except OSError:
            pass
    return {k: sorted(v) for k, v in used.items()}


def downloads():
    out = []
    for p in sorted(STATE.glob("*.json")) if STATE.is_dir() else []:
        try:
            st = json.loads(p.read_text())
        except ValueError:
            continue
        if st["status"] in ("running", "starting") and st["name"] not in LIVE:
            st["status"] = "interrupted"
        out.append(st)
    return out


def register(app, authed):
    router = APIRouter()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/models")
    def models(request: Request):
        need(request)
        U = in_use()
        return {"models": [{"name": p.name, "size": p.stat().st_size, "use": U.get(p.name, [])} for p in sorted(MODELS.glob("*.gguf"))],
                "downloads": downloads()}

    @router.post("/api/download/start")
    async def dl_start(request: Request):
        need(request)
        data = await request.json()
        try:
            start(str(data.get("url", "")), bool(data.get("confirmed")))
        except PermissionError as e:
            raise HTTPException(status_code=409, detail=f"third_party:{e}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @router.post("/api/download/cancel")
    async def dl_cancel(request: Request):
        need(request)
        name = str((await request.json()).get("name", ""))
        if Path(name).name != name or not name.endswith(".gguf"):
            raise HTTPException(status_code=400, detail="bad name")
        if name in LIVE:
            STOP.add(name)
        else:
            (MODELS / (name + ".part")).unlink(missing_ok=True)
            _path(name).unlink(missing_ok=True)
        return {"ok": True}

    @router.post("/api/models/delete")
    async def delete_model(request: Request):
        need(request)
        name = str((await request.json()).get("name", ""))
        if Path(name).name != name or not name.endswith(".gguf") or not (MODELS / name).is_file():
            raise HTTPException(status_code=400, detail="no such model file")
        files = group(name)
        used = in_use()
        busy = sorted({w for f in files for w in used.get(f, [])})
        if busy:
            raise HTTPException(status_code=409, detail="In use by " + ", ".join(busy) + ". Pick another model there first.")
        for f in files:
            (MODELS / f).unlink(missing_ok=True)
        try:
            import agent
            agent.log("settings: deleted model " + ", ".join(files))
        except Exception:
            pass
        return {"ok": True, "deleted": files}

    app.include_router(router)
