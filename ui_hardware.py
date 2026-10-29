"""Settings page, Hardware: run the local models on the CPU or on a GPU (Vulkan) in this container.
The switch runs /usr/local/bin/memory-agent-backend as its own systemd job (it rebuilds llama.cpp, which takes a while,
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
    if to not in ("vulkan", "cpu"):
        raise ValueError("vulkan or cpu")
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
        return dict(status(), running=running())

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


HTML = r"""
<div id="hw"></div>
<script>
(function(){
const info=h("div",{}),msg=h("div",{class:"mut"});
const log=h("pre",{style:"white-space:pre-wrap;word-break:break-word;font:12px/1.4 ui-monospace,Menlo,Consolas,monospace;max-height:40vh;overflow:auto;display:none"});
const btn=h("button",{class:"pri"},"");let target="";
function show(s){const gpu=s.backend==="vulkan";target=gpu?"cpu":"vulkan";
 info.replaceChildren(
  h("div",{},"Local models run on: ",h("b",{},gpu?"the GPU (Vulkan)":"the CPU")),
  h("div",{class:"mut"},s.gpus.length?"GPU in this container: "+(s.vulkan.length?s.vulkan.join(", "):s.gpus.join(", ")):
   "No GPU in this container. To add one, run on the Proxmox host: install.sh --add-gpu <this container's ID>. It passes the GPU in and switches the models to it."),
  h("div",{class:"mut"},"This is only for the models on this server; a model on an external server uses that machine's hardware."));
 btn.textContent=gpu?"Switch to the CPU":"Switch to the GPU (Vulkan)";btn.style.display=gpu||s.gpus.length?"":"none"}
async function load(){try{const s=await api("/api/hardware");show(s);if(s.running)follow()}catch(e){}}
async function follow(){log.style.display="";msg.textContent="Switching. Building llama.cpp takes a while; the model servers restart at the end.";
 while(true){await new Promise(function(r){setTimeout(r,3000)});
  try{const p=await api("/api/hardware/progress");log.textContent=p.log.join("\n");log.scrollTop=log.scrollHeight;
   if(!p.running){const t=p.log.join("\n");msg.textContent=/^done:/m.test(t)?"Done.":"It did not go through; nothing was changed. Its output is below.";load();return}}catch(e){}}}
btn.onclick=async function(){
 const gpu=target==="vulkan";
 if(!await window.warnPop(gpu?"Run the local models on the GPU?":"Run the local models on the CPU?",
  ["llama.cpp is rebuilt for "+(gpu?"Vulkan":"the CPU")+" (this takes a while), then the model servers here restart: a call running on them is cut off and done again.",
   gpu?"Each model then puts all its layers on the GPU, unless GPU layers is set under its Advanced server flags. Its memory is the GPU's.":"The models then use this container's memory and CPU cores.",
   "If the build fails, nothing is changed."],"Switch","Not now"))return;
 const r=await fetch("/api/hardware/switch",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({to:target})});
 const j=await r.json().catch(function(){return{}});if(!r.ok){msg.textContent=j.detail||"Could not start it";return}follow()};
$("#hw").replaceChildren(h("div",{class:"card"},h("h3",{},"Hardware"),info,h("div",{style:"margin-top:8px"},btn),msg,log));
load();
})();
</script>
"""
