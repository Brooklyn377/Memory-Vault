"""Settings page: limits and guardrails, one collapsed section inside each model's card."""
from fastapi import APIRouter, HTTPException, Request

import tuning


def register(app, authed):
    router = APIRouter()

    @router.get("/api/tuning")
    def get(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        return {"items": tuning.listing()}

    @router.post("/api/tuning/save")
    async def save(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        d = await request.json()
        try:
            if not d.get("confirmed"):   # ask first: anything unsafe comes back for a "Save anyway"
                _, warn = tuning.save(d.get("values") or {}, dry=True)
                if warn:
                    return {"ok": False, "confirm": warn}
            r, warn = tuning.save(d.get("values") or {})
            return {"ok": True, "restart": r, "warn": warn, "notes": tuning.token_warnings()}
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    app.include_router(router)


HTML = r"""
<div id="tn"></div>
<script>
(function(){
const MN={qwen:"Sweeper",gpt:"Reviewer",cur:"Curator",pa:"Assistant"};
const SEC={},placed={};
function build(m,items){
 const inp={};
 const rows=items.map(function(it){
  const e=h("input",{type:"number",step:"1",min:"-1",placeholder:String(it.default)});
  e.value=it.saved==null?"":it.saved;inp[it.key]=e;
  return h("div",{style:"padding:8px 0;border-top:1px solid var(--line)"},
   h("div",{class:"fld"},h("label",{},it.label+" (default "+it.default+")"),e),
   h("div",{class:"mut",style:"font-size:12px"},it.help+" Usual range "+it.lo+" to "+it.hi+"; outside it you get a warning. -1: "+it.off+". "+(it.restart?"Takes effect after restarting "+it.restart+".":"Takes effect on the next use.")))});
 const msg=h("div",{class:"mut"});
 async function save(vals,confirmed){
  msg.textContent="";
  const r=await fetch("/api/tuning/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({values:vals,confirmed:!!confirmed})});
  const j=await r.json().catch(function(){return{}});
  if(r.ok&&j.confirm){   // something unsafe: ask, and save anyway if you say so
   if(await window.warnPop("These limits may not be safe",j.confirm,"Save anyway"))return save(vals,true);
   msg.textContent="Not saved.";return}
  if(!r.ok){msg.textContent=j.detail||"Could not save";window.warnPop("Could not save",[j.detail||"Could not save"]);return}
  msg.textContent="Saved."+(j.restart&&j.restart.length?" Restart to apply: "+j.restart.join(", ")+".":" Live now.")+(j.notes&&j.notes.length?" Heads up: "+j.notes.join(" "):"")}
 function values(){const o={};for(const k in inp)o[k]=inp[k].value;return o}
 const d=h("details",{class:"sec"},h("summary",{},"Limits and guardrails"),...rows,
  h("div",{class:"mut",style:"margin-top:6px"},"A blank box uses the default; -1 turns a limit off. These are separate from Apply all: they save here and never restart a model server."),
  h("button",{class:"pri",style:"margin-top:8px",onclick:function(){save(values())}},"Save limits"),
  h("button",{style:"margin-top:8px",onclick:function(){const o={};for(const k in inp){inp[k].value="";o[k]=""}save(o)}},"Reset these to defaults"),msg);
 return d}
function place(){
 let left=0;
 Object.keys(MN).forEach(function(m){
  if(placed[m]||!SEC[m])return;
  const cards=document.querySelectorAll("#mm .card");
  for(let i=0;i<cards.length;i++){
   const b=cards[i].querySelector(".mh b");
   if(b&&b.textContent===MN[m]){cards[i].insertBefore(SEC[m],cards[i].lastElementChild);placed[m]=true;return}}
  left++});
 return left}
(async function(){
 const d=await api("/api/tuning");
 Object.keys(MN).forEach(function(m){SEC[m]=build(m,d.items.filter(function(x){return x.model===m}))});
 if(place()===0)return;
 const mo=new MutationObserver(function(){if(place()===0)mo.disconnect()});
 mo.observe(document.body,{childList:true,subtree:true});
 setTimeout(function(){
  mo.disconnect();
  Object.keys(MN).forEach(function(m){if(!placed[m]&&SEC[m]){$("#tn").append(h("div",{class:"card"},h("h3",{},MN[m]+" limits"),SEC[m]));placed[m]=true}})},10000)})();
})();
</script>
"""
