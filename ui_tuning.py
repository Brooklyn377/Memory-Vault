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
            r = tuning.save(d.get("values") or {})
            return {"ok": True, "restart": r, "warn": tuning.warnings()}
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    app.include_router(router)


HTML = r"""
<div id="tn"></div>
<script>
(function(){
const MN={qwen:"Sweeper",gpt:"Reviewer",cur:"Curator"};
const SEC={},placed={};
function build(m,items){
 const inp={};
 const rows=items.map(function(it){
  const e=h("input",{type:"number",step:"1",placeholder:String(it.default)});
  e.value=it.saved==null?"":it.saved;inp[it.key]=e;
  return h("div",{style:"padding:8px 0;border-top:1px solid var(--line)"},
   h("div",{class:"fld"},h("label",{},it.label+" (default "+it.default+")"),e),
   h("div",{class:"mut",style:"font-size:12px"},it.help+" Range "+it.lo+" to "+it.hi+". "+(it.restart?"Takes effect after restarting "+it.restart+".":"Takes effect on the next use.")))});
 const msg=h("div",{class:"mut"});
 async function save(vals){
  msg.textContent="";
  const r=await fetch("/api/tuning/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({values:vals})});
  const j=await r.json().catch(function(){return{}});
  msg.textContent=r.ok?"Saved."+(j.restart&&j.restart.length?" Restart to apply: "+j.restart.join(", ")+".":" Live now.")+(j.warn&&j.warn.length?" Heads up: "+j.warn.join(" "):""):(j.detail||"Could not save")}
 function values(){const o={};for(const k in inp)o[k]=inp[k].value;return o}
 const d=h("details",{class:"sec"},h("summary",{},"Limits and guardrails"),...rows,
  h("div",{class:"mut",style:"margin-top:6px"},"A blank box uses the default. These are separate from Apply all: they save here and never restart a model server."),
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
