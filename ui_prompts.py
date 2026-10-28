"""Settings page, prompts: view and edit every model's system prompts."""
from fastapi import APIRouter, HTTPException, Request

import prompts


def register(app, authed):
    router = APIRouter()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/prompts")
    def lst(request: Request):
        need(request)
        return {"prompts": prompts.listing()}

    @router.post("/api/prompts/save")
    async def save(request: Request):
        need(request)
        d = await request.json()
        try:
            prompts.save(str(d.get("id", "")), str(d.get("text", "")), bool(d.get("skip_check")))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @router.post("/api/prompts/reset")
    async def reset(request: Request):
        need(request)
        try:
            prompts.reset(str((await request.json()).get("id", "")))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    app.include_router(router)


HTML = r"""
<style>
.pp textarea{width:100%;min-height:170px;font:13px/1.4 ui-monospace,Menlo,Consolas,monospace;box-sizing:border-box}
.pp details.pi{border-top:1px solid var(--line);padding:8px 0}
.pp summary{cursor:pointer}
.pp pre{white-space:pre-wrap;word-break:break-word;font:12px/1.4 ui-monospace,Menlo,Consolas,monospace;margin:6px 0}
.pp .pb button{margin:8px 8px 0 0}
</style>
<div id="pp" class="pp"></div>
<script>
(function(){
const MN={qwen:"Sweeper",gpt:"Reviewer",cur:"Curator",pa:"Assistant"};
const NODES={},OPEN={};
let D=[];
function when(t){return new Date(t*1000).toLocaleString([],{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"})}
async function call(path,body){const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});const j=await r.json().catch(function(){return{}});return{ok:r.ok,detail:j.detail||""}}
function item(p){
 const ta=h("textarea",{});ta.value=p.text;
 const msg=h("div",{class:"mut"}),skip=h("input",{type:"checkbox"});
 const dflt=h("pre",{class:"mut"},p.default||"(the default could not be read from the code)");dflt.style.display="none";
 const hist=h("div",{});hist.style.display="none";
 (p.history||[]).forEach(function(v){
  const pre=h("pre",{class:"mut"},v.text);pre.style.display="none";
  hist.append(h("div",{},h("span",{class:"mut"},"v"+v.n+" · "+when(v.at)+" "),
   h("button",{onclick:function(){pre.style.display=pre.style.display==="none"?"":"none"}},"View"),
   h("button",{onclick:function(){ta.value=v.text;msg.textContent="Loaded v"+v.n+" into the box. Press Save to use it."}},"Load"),pre))});
 if(!(p.history||[]).length)hist.append(h("div",{class:"mut"},"No saved versions yet."));
 async function refresh(){const d=await api("/api/prompts");D=d.prompts;const q=D.find(function(x){return x.id===p.id});if(q&&NODES[p.id]){const n=item(q);NODES[p.id].replaceWith(n);NODES[p.id]=n}}
 async function save(){msg.textContent="";const r=await call("/api/prompts/save",{id:p.id,text:ta.value,skip_check:skip.checked});if(!r.ok){msg.textContent=r.detail||"Could not save";return}await refresh()}
 async function reset(){if(!confirm("Go back to the default prompt?"))return;const r=await call("/api/prompts/reset",{id:p.id});if(!r.ok){msg.textContent=r.detail||"Could not reset";return}await refresh()}
 const state=p.missing?"default could not be read":p.edited?"edited v"+p.version:"default";
 const d=h("details",{class:"pi"},h("summary",{},p.title+" · "+state),h("div",{class:"mut",style:"margin:6px 0"},p.when),
  p.stale?h("div",{style:"color:#f5a524;margin:6px 0"},"The default changed in a code update since you edited this. Show default to compare."):null,
  ta,
  p.required.length?h("div",{class:"mut"},"Answer keys the code reads: "+p.required.join(", ")):null,
  h("div",{class:"fld"},h("label",{},"Skip the format check"),skip),
  h("div",{class:"pb"},h("button",{class:"pri",onclick:save},"Save"),h("button",{onclick:reset},"Reset to default"),
   h("button",{onclick:function(){dflt.style.display=dflt.style.display==="none"?"":"none"}},"Show default"),
   h("button",{onclick:function(){hist.style.display=hist.style.display==="none"?"":"none"}},"History")),
  msg,dflt,hist);
 d.open=!!OPEN[p.id];d.ontoggle=function(){OPEN[p.id]=d.open};
 return d}
function render(){
 const root=h("div",{});
 Object.keys(MN).forEach(function(m){
  const card=h("div",{class:"card"},h("h3",{},MN[m]+" prompts"));
  if(m==="cur")card.append(h("div",{class:"mut"},"The five note-review parts are joined: intro, then the jobs switched on in the Curator card, then the format."));
  const own=D.filter(function(p){return p.model===m});
  if(!own.length)card.append(h("div",{class:"mut"},"No prompts yet."));
  own.forEach(function(p){const n=item(p);NODES[p.id]=n;card.append(n)});
  root.append(card)});
 $("#pp").replaceChildren(root)}
(async function(){const d=await api("/api/prompts");D=d.prompts;render()})();
})();
</script>
"""
