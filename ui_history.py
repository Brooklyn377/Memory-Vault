"""History page: a filterable list of every action the models and you have taken."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import history
import ui_extra


def register(app, authed):
    router = APIRouter()

    @router.get("/api/history")
    def hist(request: Request, who: str = "", kinds: str = "", offset: int = 0, limit: int = 50):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        w = {x for x in who.split(",") if x in history.WHO} or set(history.WHO)
        k = {x for x in kinds.split(",") if x in history.KINDS} or set(history.KINDS) - {"system", "call"}
        events, more = history.query(w, k, max(0, offset), max(1, min(limit, 200)))
        return {"events": events, "more": more}

    @router.get("/history", response_class=HTMLResponse)
    def page(request: Request):
        if not authed(request):
            return RedirectResponse("/", status_code=303)
        return ui_extra.PAGE.replace("__BODY__", HTML).replace("__ACTIVE__", "history")

    app.include_router(router)


HTML = r"""
<style>
.chp{display:inline-block;margin:0 6px 8px 0;padding:4px 10px;border-radius:14px;border:1px solid var(--line);color:var(--mut);cursor:pointer;font-size:13px;user-select:none}
.chp.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.hrw{padding:10px 0;border-top:1px solid var(--line)}
.hh{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px}
.hh i{width:9px;height:9px;border-radius:5px;display:inline-block}
.hw{font-weight:600;font-size:13px}
.ht{flex-basis:100%;margin-top:2px}
.hd{white-space:pre-wrap;word-break:break-word;margin-top:6px;font-size:13px}
</style>
<div class="card"><div id="hw"></div><div id="hk"></div></div>
<div id="hl"></div>
<div id="hm" style="margin:12px 0"></div>
<script>
(function(){
const WHO={sweeper:["Sweeper","#4c9aff"],reviewer:["Reviewer","#f5a623"],curator:["Curator","#34d399"],assistant:["Assistant","#e879f9"],you:["You","#c084fc"],system:["System","#8b93a7"]};
const KIND={vault:"Vault changes",fact:"Facts",task:"Tasks",link:"Links",review:"Reviews",prune:"Prunes",file:"Files",ask:"Questions",chat:"Chat",publish:"Publishing",setting:"Settings",import:"Imports",problem:"Problems",system:"Startup",call:"Model calls"};
const onW=new Set(["sweeper","reviewer","curator","assistant","you"]);
const onK=new Set(Object.keys(KIND).filter(k=>k!=="system"&&k!=="call"));
let items=[],seen=new Set(),more=false,busy=false;
function chips(keys,on,label){return keys.map(k=>{const s=h("span",{class:"chp"+(on.has(k)?" on":"")},label(k));s.onclick=()=>{on.has(k)?on.delete(k):on.add(k);s.classList.toggle("on");load(true)};return s})}
function row(e){
 const d=new Date(e.t*1000);
 const r=h("div",{class:"hrw"},h("div",{class:"hh"},h("i",{style:"background:"+WHO[e.who][1]}),h("span",{class:"hw"},WHO[e.who][0]),
  h("span",{class:"mut"},d.toLocaleString([],{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"})),h("div",{class:"ht"},e.text)));
 if(e.detail||(e.open&&e.open.length)){const dt=h("div",{class:"hd mut"},e.detail||"");(e.open||[]).forEach(l=>dt.append(h("div",{style:"margin-top:8px"},h("a",{href:l.href,style:"color:var(--acc);font-weight:600",onclick:ev=>ev.stopPropagation()},l.label+" ›"))));dt.style.display="none";r.append(dt);r.onclick=()=>{dt.style.display=dt.style.display==="none"?"":"none"}}
 return r}
function paint(){
 $("#hl").replaceChildren(...(items.length?items.map(row):[h("div",{class:"card mut"},"Nothing recorded yet for these filters.")]));
 $("#hm").replaceChildren(...(more?[h("button",{onclick:()=>load(false)},"Load older")]:[]))}
async function load(reset){
 if(busy)return;
 if(!onW.size||!onK.size){items=[];more=false;paint();return}
 busy=true;
 try{
  const off=reset?0:items.length;
  const d=await api("/api/history?who="+[...onW].join(",")+"&kinds="+[...onK].join(",")+"&offset="+off+"&limit=50");
  if(reset){items=[];seen=new Set()}
  for(const e of d.events){const k=e.t+"|"+e.text;if(!seen.has(k)){seen.add(k);items.push(e)}}
  more=d.more;paint()
 }catch(e){}
 busy=false}
$("#hw").replaceChildren(...chips(Object.keys(WHO),onW,k=>WHO[k][0]));
$("#hk").replaceChildren(...chips(Object.keys(KIND),onK,k=>KIND[k]));
load(true);
setInterval(()=>{if(!document.hidden&&items.length<=50)load(true)},15000);
})();
</script>
"""
