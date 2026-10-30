"""This server's CPU speed (shown on the Servers card): asks a small helper on the Proxmox host (the container cannot touch host CPU settings)."""
import json
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

import config

_C = config.load().get("cpu_helper") or {}
HOST = str(_C.get("url") or "").rstrip("/")  # blank: the helper is an optional extra, not set up
TOKEN = Path(_C.get("token_file") or "/etc/memory-agent/cpu-speed.token")


def _call(path, body=None):
    if not HOST:
        raise LookupError("cpu_helper.url is not set in config.yaml")
    headers, data = {"Content-Type": "application/json"}, None
    if body is not None:
        try:
            headers["X-Token"] = TOKEN.read_text().strip()
        except OSError:
            raise RuntimeError(f"the token file {TOKEN} is missing")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(HOST + path, data=data, headers=headers), timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get("error", "")
        except Exception:
            detail = ""
        raise ValueError(detail or f"the helper answered {e.code}")


def register(app, authed):
    router = APIRouter()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/cpu")
    def get(request: Request):
        need(request)
        try:
            return _call("/state")
        except LookupError:
            return {"ok": False, "why": "No host CPU helper is set up (cpu_helper.url in config.yaml)."}
        except Exception as e:
            return {"ok": False, "why": f"The host helper is not answering ({type(e).__name__}). Install it on the Proxmox host first."}

    @router.post("/api/cpu/set")
    async def set_(request: Request):
        need(request)
        d = await request.json()
        if d.get("mode") not in ("state", "speed"):
            raise HTTPException(status_code=400, detail="pick a power state or a fixed speed")
        try:
            return _call("/set", {"mode": d["mode"], "value": d.get("value")})
        except (ValueError, RuntimeError, LookupError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"host helper unreachable: {type(e).__name__}")

    app.include_router(router)

