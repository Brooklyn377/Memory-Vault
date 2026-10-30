"""This server's hardware (the Servers card): the GPU set-up, and how many models may work at once on its CPU and GPU.
The GPU set-up runs /usr/local/bin/memory-agent-backend as its own systemd job (it rebuilds llama.cpp, which takes a while,
and restarts the model servers); the page shows its output."""
import subprocess

from fastapi import APIRouter, HTTPException, Request

import agent

HELPER = "/usr/local/bin/memory-agent-backend"
LOG = agent.STATE / "backend.log"
UNIT = "backend-memory-agent"   # not memory-*: the updater's rollback restarts every running memory-* unit


def running():
    return subprocess.run(["systemctl", "is-active", "--quiet", UNIT], timeout=10).returncode == 0


def status():
    try:
        out = subprocess.run([HELPER, "--status"], capture_output=True, text=True, timeout=60).stdout
    except Exception as e:
        return {"backend": "cpu", "gpus": [], "vulkan": [], "error": f"{type(e).__name__}: {str(e)[:120]}"}
    lines = out.splitlines()
    pick = lambda pre: [l[len(pre):].strip() for l in lines if l.startswith(pre)]
    gpus = (pick("GPU devices:") or [""])[0]
    return {"backend": (pick("backend:") or ["cpu"])[0], "gpus": [] if gpus in ("", "none") else gpus.split(),
            "vulkan": pick("Vulkan device:"), "error": ""}


def switch(to):
    if to not in ("vulkan", "cuda", "rocm", "cpu"):
        raise ValueError("vulkan, cuda, rocm or cpu")
    if running():
        raise ValueError("a switch is already running")
    subprocess.run(["systemctl", "reset-failed", UNIT], capture_output=True, timeout=10)
    r = subprocess.run(["systemd-run", f"--unit={UNIT}", "--collect", "--quiet",
                        f"--property=StandardOutput=truncate:{LOG}", f"--property=StandardError=append:{LOG}",
                        HELPER, to], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise ValueError(f"could not start it: {(r.stderr or r.stdout).strip()[:200]}")
    agent.log(f"settings: local models switching to {'the GPU (Vulkan)' if to == 'vulkan' else 'the CPU'}")


def register(app, authed):
    router = APIRouter()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/hardware")
    def get(request: Request):
        need(request)
        import turns
        return dict(status(), running=running(), limits=turns.host_limits())

    @router.post("/api/hardware/limit")
    async def limit(request: Request):
        need(request)
        d = await request.json()
        import turns
        try:
            turns.set_host_limit(d.get("key"), d.get("n"))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @router.post("/api/hardware/switch")
    async def post(request: Request):
        need(request)
        try:
            switch((await request.json()).get("to"))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @router.get("/api/hardware/progress")
    def progress(request: Request):
        need(request)
        try:
            lines = LOG.read_text(errors="replace").splitlines()[-40:]
        except OSError:
            lines = []
        return {"running": running(), "log": lines}

    app.include_router(router)

