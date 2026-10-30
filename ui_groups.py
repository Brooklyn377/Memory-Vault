"""Settings page, Model groups: lists of models that are allowed to work at the same time, wherever they run, and the
priority order of the queue. A model may be in several lists."""
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
    return {"groups": cfg["groups"], "turn_minutes": cfg["turn_minutes"], "priority": cfg["priority"],
            "models": [{"key": m, "name": turns.NICE[m], "external": _external(m), "has_file": bool(settings.get(m).get("model_file"))}
                       for m in turns.MODELS],
            "loaded": [m for m in turns.MODELS if (applier.health(m) == "ready" if _external(m) else applier._active(m))],
            "turn": st["group"], "waiting": [m for m in turns.wants() if m not in st["group"]],
            "manager": turns.alive()}


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
            cfg, notes = turns.save_config(d.get("groups") or [], d.get("turn_minutes", 10), d.get("priority"))
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
let MODELS=[],boxes=[],order=[];
const prio=h("div",{style:"margin-bottom:8px"});
function drawPrio(){prio.replaceChildren(...order.map(function(k,i){
 const m=MODELS.find(function(x){return x.key===k})||{name:k};
 const row=h("div",{style:"display:flex;align-items:center;gap:10px;padding:10px;margin:4px 0;border:1px solid var(--line);border-radius:8px;touch-action:none;user-select:none"},
  h("span",{style:"font-size:20px;cursor:grab"},"\u2630"),h("span",{style:"flex:1"},(i+1)+". "+m.name));
 row.dataset.key=k;
 row.addEventListener("pointerdown",function(ev){
  ev.preventDefault();row.style.opacity="0.6";
  const move=function(e){const rows=Array.from(prio.children);
   for(const r of rows){if(r===row)continue;const b=r.getBoundingClientRect();
    if(e.clientY>b.top&&e.clientY<b.bottom){const before=rows.indexOf(row)>rows.indexOf(r);prio.insertBefore(row,before?r:r.nextSibling);break}}};
  const up=function(){window.removeEventListener("pointermove",move);window.removeEventListener("pointerup",up);window.removeEventListener("pointercancel",up);
   order=Array.from(prio.children).map(function(r){return r.dataset.key});drawPrio()};
  window.addEventListener("pointermove",move);window.addEventListener("pointerup",up);window.addEventListener("pointercancel",up)});
 return row}))}
function groupBox(models){
 const cb={};
 const row=h("div",{style:"border-top:1px solid var(--line);padding:8px 0"},h("b",{},"Group"));
 MODELS.forEach(function(m){
  const c=h("input",{type:"checkbox"});c.checked=models.indexOf(m.key)>=0;cb[m.key]=c;
  row.append(h("div",{class:"fld"},h("label",{},m.name),c))});
 const del=h("button",{onclick:function(){boxes=boxes.filter(function(b){return b.row!==row});row.remove()}},"Remove group");
 row.append(del);const b={row:row,cb:cb};boxes.push(b);return row}
function collect(){return boxes.map(function(b){return{models:Object.keys(b.cb).filter(function(k){return b.cb[k].checked})}}).filter(function(g){return g.models.length})}
async function load(){try{const d=await api("/api/groups");MODELS=d.models;mins.value=d.turn_minutes;boxes=[];order=d.priority;drawPrio();
 list.replaceChildren(...d.groups.map(function(g){return groupBox(g.models)}));
 const nm=function(a){return a.map(function(k){return(MODELS.find(function(m){return m.key===k})||{}).name||k}).join(", ")||"none"};
 status.textContent=(d.manager?"":"The turn manager is not running yet, so models are not switched. ")+"Loaded now: "+nm(d.loaded)+(d.waiting.length?" · waiting for a turn: "+nm(d.waiting):"")+"."}catch(e){}}
async function save(){
 const body={groups:collect(),turn_minutes:mins.value,priority:order};
 const r=await fetch("/api/groups/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
 const j=await r.json().catch(function(){return{}});
 if(!r.ok){msg.textContent=j.detail||"Could not save";window.warnPop("Could not save",[j.detail||"Could not save"]);return}
 msg.textContent="Saved."+((j.notes||[]).length?" "+j.notes.join(" "):"");load()}
$("#grp").replaceChildren(h("div",{class:"card"},h("h3",{},"Model groups"),
 h("div",{class:"mut"},"A group is a list of models that are allowed to work at the same time, wherever they run. A model can be in several groups. The models working at one time must all be in one group: models that no group lists together take turns, even on different machines. With no groups, one model works at a time. A server's number (on the Servers card) also caps how many may work on that server; groups never raise it."),
 h("div",{style:"margin:12px 0 4px;font-weight:600"},"Priority (drag to reorder)"),
 h("div",{class:"mut"},"The first goes first when several wait for the same hardware. It never takes the turn from a model that is working."),
 prio,
 status,list,
 h("button",{onclick:function(){list.append(groupBox([]))}},"Add group"),
 h("div",{class:"fld",style:"margin-top:8px"},h("label",{},"Minutes a model keeps its turn while others wait (0: give way as soon as it is idle)"),mins),
 h("button",{class:"pri",style:"margin-top:8px",onclick:save},"Save groups"),msg));
load();setInterval(function(){if(!document.hidden)api("/api/groups").then(function(d){const nm=function(a){return a.map(function(k){return(MODELS.find(function(m){return m.key===k})||{}).name||k}).join(", ")||"none"};status.textContent=(d.manager?"":"The turn manager is not running yet, so models are not switched. ")+"Loaded now: "+nm(d.loaded)+(d.waiting.length?" · waiting for a turn: "+nm(d.waiting):"")+"."}).catch(function(){})},5000);
})();
</script>
"""
