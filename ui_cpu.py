"""Settings page, CPU speed: asks a small helper on the Proxmox host (the container cannot touch host CPU settings)."""
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


HTML = r"""
<style>
#cp input[type=radio]{width:20px;height:20px;padding:0;margin:0 8px 0 0;vertical-align:middle}
</style>
<div id="cp"></div>
<script>
(function(){
const GL={performance:"Performance (full speed)",powersave:"Power saver (lowest speed)",ondemand:"On demand (speeds up under load)",conservative:"Conservative (ramps up gently)",schedutil:"Scheduler-driven",userspace:"Manual (userspace)"};
const ghz=k=>(k/1e6).toFixed(2)+" GHz";
const rs=h("input",{type:"radio",name:"cpumode"}),rf=h("input",{type:"radio",name:"cpumode"});
const sg=h("select",{}),sf=h("select",{});
const go=h("button",{class:"pri"},"Apply CPU setting");
const st=h("div",{class:"mut"}),msg=h("div",{class:"mut"});
let built=false;
function sync(){sg.disabled=!rs.checked;sf.disabled=!rf.checked}
rs.onchange=sync;rf.onchange=sync;
go.onclick=async()=>{msg.textContent="";
 const mode=rf.checked?"speed":"state";
 const value=mode==="speed"?Number(sf.value):sg.value;
 const r=await fetch("/api/cpu/set",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:mode,value:value})});
 const j=await r.json().catch(()=>({}));
 msg.textContent=r.ok?"Applied.":(j.detail||"Could not apply");load()};
async function load(){
 const d=await api("/api/cpu");
 if(!d.ok){built=false;st.textContent=d.why||"Not available";[rs,rf,sg,sf,go].forEach(e=>e.disabled=true);return}
 st.textContent="Now: "+(GL[d.gov]||d.gov)+", average "+ghz(d.avg_khz)+" across "+d.cores+" cores, allowed "+ghz(d.min_khz)+" to "+ghz(d.max_khz);
 if(built)return;
 built=true;rs.disabled=false;rf.disabled=false;go.disabled=false;
 sg.replaceChildren(...d.governors.map(g=>h("option",{value:g},GL[g]||g)));
 const n=d.freqs.length,turbo=n>1&&d.freqs[n-1]-d.freqs[n-2]<20000;
 sf.replaceChildren(...d.freqs.map((k,i)=>h("option",{value:k},ghz(k)+(turbo&&i===n-1?" + Turbo":turbo&&i===n-2?" (no Turbo)":""))));
 sg.value=d.gov;
 if(d.mode==="speed")sf.value=d.max_khz;
 rs.checked=d.mode==="state";rf.checked=d.mode==="speed";sync()}
$("#cp").replaceChildren(h("div",{class:"card"},h("h3",{},"CPU speed (host)"),st,
 h("div",{class:"fld"},h("label",{},rs,"Power state"),sg),
 h("div",{class:"fld"},h("label",{},rf,"Fixed speed"),sf),go,msg));
load();setInterval(load,5000);
})();
</script>
"""
