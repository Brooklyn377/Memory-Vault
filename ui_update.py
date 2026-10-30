"""Settings page, Updates: the installed and newest version, Check now, Update now, and the update's progress."""
from fastapi import APIRouter, HTTPException, Request

import updates


def register(app, authed):
    router = APIRouter()
    updates.start()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/update/status")
    def status(request: Request):
        need(request)
        return dict(updates.status(), running=updates.running(), rollback=updates.rollback_info())

    @router.post("/api/update/check")
    def check(request: Request):
        need(request)
        return updates.check()

    @router.post("/api/update/apply")
    def apply(request: Request):
        need(request)
        try:
            updates.apply()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @router.post("/api/update/rollback")
    def rollback(request: Request):
        need(request)
        try:
            updates.rollback()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @router.get("/api/update/progress")
    def progress(request: Request):
        need(request)
        return updates.progress()

    app.include_router(router)


HTML = r"""
<div id="upd"></div>
<script>
(function(){
const short=function(v){return v?String(v).slice(0,10):"unknown"};
const when=function(t){return t?new Date(t*1000).toLocaleString([],{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}):"never"};
const msg=h("div",{class:"mut"}),log=h("pre",{style:"white-space:pre-wrap;word-break:break-word;font:12px/1.4 ui-monospace,Menlo,Consolas,monospace;max-height:40vh;overflow:auto;display:none"});
const info=h("div",{}),changes=h("div",{}),recent=h("div",{});
let polling=false;
// What is in the updates you have not applied, newest first: each release, and the changes it brought.
function showChanges(s){
 if(!s.available){changes.replaceChildren();return}
 if(!(s.changes||[]).length){changes.replaceChildren(h("div",{class:"mut",style:"margin-top:12px"},"Could not load the list of changes"+(s.changes_note?" ("+s.changes_note+")":"")+"."));return}
 const n=s.changes.length;
 changes.replaceChildren(h("div",{style:"margin-top:12px;font-weight:600"},"Changes not applied yet ("+n+(n===1?" update":" updates")+")"),
  ...(s.changes_note?[h("div",{class:"mut"},s.changes_note)]:[]),
  h("div",{style:"max-height:45vh;overflow:auto;margin-top:6px"},...s.changes.map(function(c){
   return h("div",{style:"border-top:1px solid var(--line);padding:8px 0"},
    h("div",{class:"mut"},when(Date.parse(c.date)/1000)+" · "+short(c.sha)),
    ...c.items.map(function(i){return h("div",{style:"margin:4px 0"},h("div",{},i.subject),i.detail?h("div",{class:"mut"},i.detail):null)}))})))}
// The last 10 releases with their notes (all GitHub gives), collapsed: the installed one and the ones not applied yet are marked.
function showRecent(s){
 const list=s.recent||[];
 if(!list.length){recent.replaceChildren();return}
 const seen=s.changes&&s.changes.length?new Set(s.changes.map(function(c){return c.sha})):new Set();
 const d=h("details",{class:"sec"},h("summary",{},"Recent releases (the last "+list.length+")"),
  ...list.map(function(c){
   const tag=c.sha===s.installed?"installed":seen.has(c.sha)?"not applied yet":"";
   return h("div",{style:"border-top:1px solid var(--line);padding:8px 0"},
    h("div",{class:"mut"},when(Date.parse(c.date)/1000)+" · "+short(c.sha)+(tag?" · "+tag:"")),
    ...c.items.map(function(i){return h("div",{style:"margin:4px 0"},h("div",{},i.subject),i.detail?h("div",{class:"mut"},i.detail):null)}))}));
 const was=recent.firstChild&&recent.firstChild.open;d.open=!!was;recent.replaceChildren(d)}
function showRollback(s){
 const r=s.rollback||{};
 back.style.display=r.available?"":"none";
 back.title=r.available?"Put back "+(r.version?short(r.version):"the earlier version")+(r.at?", from before the update of "+when(Date.parse(r.at+"Z")/1000):""):"";
 back.dataset.version=r.version||"";back.dataset.at=r.at||""}
function show(s){showChanges(s);showRecent(s);showRollback(s);info.replaceChildren(
 h("div",{},"Installed: ",h("b",{},short(s.installed)),s.latest?" · newest: ":"",s.latest?h("b",{},short(s.latest)):""),
 h("div",{class:"mut"},s.error?"Last check failed: "+s.error:s.available?"An update is available.":"Up to date."," Checked "+when(s.at)+"; it checks by itself every 6 hours."))}
async function load(){try{const s=await api("/api/update/status");show(s);if(s.running)follow()}catch(e){}}
async function follow(){if(polling)return;polling=true;log.style.display="";msg.textContent="Updating. The page restarts along the way; it reloads when the update is done.";
 let seenDown=false;
 while(true){await new Promise(function(r){setTimeout(r,2000)});
  try{const p=await api("/api/update/progress");log.textContent=p.log.join("\n");log.scrollTop=log.scrollHeight;
   if(!p.running){const done=p.log.join("\n");
    msg.textContent=/updated to|up to date|restored/.test(done)?"Done. Reloading...":/failed|restor/i.test(done)?"The update did not go through; the previous version was put back. Its output is below.":"The update has finished.";
    if(/updated to|up to date|restored/.test(done)||seenDown){try{await api("/api/update/check",{method:"POST"})}catch(e){}setTimeout(function(){location.reload()},1500)}
    polling=false;return}}
  catch(e){seenDown=true;msg.textContent="Updating. The web page is restarting..."}}}
const check=h("button",{onclick:async function(){msg.textContent="Checking...";try{const s=await api("/api/update/check",{method:"POST"});show(s);msg.textContent="";if(window.updBadge)window.updBadge()}catch(e){msg.textContent=e.message}}},"Check now");
const back=h("button",{style:"display:none",onclick:async function(){
 const v=back.dataset.version,at=back.dataset.at;
 if(!await window.warnPop("Roll back the last update?",["The code goes back to "+(v?"version "+short(v):"the version from before the last update")+(at?" (the one that was running before the update of "+when(Date.parse(at+"Z")/1000)+")":"")+". The services restart; any model call running now is cut off and done again afterwards.","Your settings, login, models and vault are not touched. You can update again whenever you like; the newest version will be offered again."],"Roll back","Keep the current version"))return;
 const r=await fetch("/api/update/rollback",{method:"POST"});const j=await r.json().catch(function(){return{}});
 if(!r.ok){msg.textContent=j.detail||"Could not start the rollback";return}follow()}},"Roll back");
const go=h("button",{class:"pri",onclick:async function(){
 if(!await window.warnPop("Update now?",["The newest version is downloaded and checked, the current code is backed up, and the services restart. Any model call running now is cut off and done again afterwards.","If the update fails its checks, the previous version is put back by itself."],"Update now","Not now"))return;
 const r=await fetch("/api/update/apply",{method:"POST"});const j=await r.json().catch(function(){return{}});
 if(!r.ok){msg.textContent=j.detail||"Could not start the update";return}follow()}},"Update now");
$("#upd").replaceChildren(h("div",{class:"card"},h("h3",{},"Updates"),info,h("div",{style:"margin-top:8px"},go,check,back),msg,changes,recent,log));
load();
})();
</script>
"""
