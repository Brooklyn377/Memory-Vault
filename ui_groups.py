"""Settings page, Model groups: which local models may be loaded together. No groups (the default): one local model at a
time and the others take turns. Models on an external server need no turn and are not part of any group."""
from fastapi import APIRouter, HTTPException, Request

import applier
import settings
import turns


def _external(m):
    try:
        import remote
        return remote.enabled(m)
    except Exception:
        return False


def info():
    cfg = turns.config()
    st = turns.active()
    return {"groups": cfg["groups"], "turn_minutes": cfg["turn_minutes"],
            "models": [{"key": m, "name": turns.NICE[m], "external": _external(m), "has_file": bool(settings.get(m).get("model_file"))}
                       for m in turns.MODELS],
            "loaded": [m for m in turns.MODELS if not _external(m) and applier._active(m)],
            "turn": st["group"], "waiting": [m for m in turns.wants() if m not in st["group"] and not _external(m)],
            "manager": turns.alive()}


def memory_warnings(groups):
    """A group whose models (local, with a model file) are estimated to need more than the container has: a warning, never a
    refusal, because the estimate is only a recommendation."""
    try:
        import gguf_info
        limit = gguf_info.total_memory() - gguf_info.HEADROOM
    except Exception:
        return []
    out = []
    for g in groups:
        mem = {}
        for m in g["models"]:
            s = settings.get(m)
            if not _external(m) and s.get("model_file"):
                try:
                    mem[m] = gguf_info._fp_safe(s["model_file"], s["context"], s["parallel"])
                except Exception:
                    pass
        if len(mem) > 1 and sum(mem.values()) > limit:
            names = " + ".join(turns.NICE[m] for m in mem)
            out.append(f"{names}: about {sum(mem.values()) / gguf_info.GIB:.0f} GB estimated together, above the "
                       f"{limit / gguf_info.GIB:.0f} GB this container can safely give models. If memory runs out, the system may kill processes.")
    return out


def register(app, authed):
    router = APIRouter()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/groups")
    def get(request: Request):
        need(request)
        return info()

    @router.post("/api/groups/save")
    async def save(request: Request):
        need(request)
        d = await request.json()
        try:
            cfg, notes = turns.save_config(d.get("groups") or [], d.get("turn_minutes", 10)) if d.get("confirmed") else (None, [])
            if cfg is None:   # first press: say what is unsafe before saving, and save only if the person goes on
                clean = []
                seen = set()
                for g in d.get("groups") or []:
                    ms = [m for m in (g.get("models") or []) if m in turns.MODELS and m not in seen]
                    seen.update(ms)
                    if ms:
                        clean.append({"models": ms})
                warn = memory_warnings(clean)
                if warn:
                    return {"ok": False, "confirm": warn}
                cfg, notes = turns.save_config(d.get("groups") or [], d.get("turn_minutes", 10))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "notes": notes, **info()}

    app.include_router(router)


HTML = r"""
<div id="grp"></div>
<script>
(function(){
const msg=h("div",{class:"mut"}),list=h("div",{}),status=h("div",{class:"mut"});
const mins=h("input",{type:"number",min:"0",style:"width:90px"});
let MODELS=[],boxes=[];
function groupBox(models){
 const cb={};
 const row=h("div",{style:"border-top:1px solid var(--line);padding:8px 0"},h("b",{},"Group"));
 MODELS.forEach(function(m){
  const c=h("input",{type:"checkbox"});c.checked=models.indexOf(m.key)>=0;c.disabled=m.external;cb[m.key]=c;
  row.append(h("div",{class:"fld"+(m.external?" off":"")},h("label",{},m.name+(m.external?" (external server: needs no turn)":"")),c))});
 const del=h("button",{onclick:function(){boxes=boxes.filter(function(b){return b.row!==row});row.remove()}},"Remove group");
 row.append(del);const b={row:row,cb:cb};boxes.push(b);return row}
function collect(){return boxes.map(function(b){return{models:Object.keys(b.cb).filter(function(k){return b.cb[k].checked})}}).filter(function(g){return g.models.length})}
async function load(){try{const d=await api("/api/groups");MODELS=d.models;mins.value=d.turn_minutes;boxes=[];
 list.replaceChildren(...d.groups.map(function(g){return groupBox(g.models)}));
 const nm=function(a){return a.map(function(k){return(MODELS.find(function(m){return m.key===k})||{}).name||k}).join(", ")||"none"};
 status.textContent=(d.manager?"":"The turn manager is not running yet, so models are not switched. ")+"Loaded now: "+nm(d.loaded)+(d.waiting.length?" · waiting for a turn: "+nm(d.waiting):"")+"."}catch(e){}}
async function save(confirmed){
 const body={groups:collect(),turn_minutes:mins.value,confirmed:!!confirmed};
 const r=await fetch("/api/groups/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
 const j=await r.json().catch(function(){return{}});
 if(r.ok&&j.confirm){if(await window.warnPop("These models may not fit together",j.confirm,"Save anyway"))return save(true);msg.textContent="Not saved.";return}
 if(!r.ok){msg.textContent=j.detail||"Could not save";window.warnPop("Could not save",[j.detail||"Could not save"]);return}
 msg.textContent="Saved."+((j.notes||[]).length?" "+j.notes.join(" "):"");load()}
$("#grp").replaceChildren(h("div",{class:"card"},h("h3",{},"Model groups"),
 h("div",{class:"mut"},"Local models that are in one group can be loaded at the same time. Any other local model waits its turn: it is loaded when the group that is loaded has nothing to do, or has had its turn for the minutes below. With no groups, one local model is loaded at a time. A model on an external server needs no turn and never waits."),
 status,list,
 h("button",{onclick:function(){list.append(groupBox([]))}},"Add group"),
 h("div",{class:"fld",style:"margin-top:8px"},h("label",{},"Minutes a group keeps its turn while others wait (0: give way as soon as it is idle; a chat message from you never waits)"),mins),
 h("button",{class:"pri",style:"margin-top:8px",onclick:function(){save(false)}},"Save groups"),msg));
load();setInterval(function(){if(!document.hidden)api("/api/groups").then(function(d){const nm=function(a){return a.map(function(k){return(MODELS.find(function(m){return m.key===k})||{}).name||k}).join(", ")||"none"};status.textContent=(d.manager?"":"The turn manager is not running yet, so models are not switched. ")+"Loaded now: "+nm(d.loaded)+(d.waiting.length?" · waiting for a turn: "+nm(d.waiting):"")+"."}).catch(function(){})},5000);
})();
</script>
"""
