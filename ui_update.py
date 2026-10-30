"""Settings page, Updates: the installed and newest version, Check now, Update now, and the update's progress."""
from fastapi import APIRouter, HTTPException, Request

import updates


def register(app, authed):
    router = APIRouter()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/update/status")
    def status(request: Request):
        need(request)
        return dict(updates.status(), running=updates.running())

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

    @router.post("/api/update/install")
    async def install(request: Request):
        need(request)
        try:
            updates.install((await request.json()).get("sha"))
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
// One release: a line (date, version, what it was) that you tap to open its notes.
function release(c,tag,older){
 const items=c.items||[],plain=items.length===1&&/^Release from /.test(items[0].subject);
 const first=plain?"":items[0]?items[0].subject:"";
 const d=h("details",{style:"border-top:1px solid var(--line);padding:6px 0"},
  h("summary",{style:"cursor:pointer;padding:4px 0"},h("span",{class:"mut"},when(Date.parse(c.date)/1000)+" · "+short(c.sha)+(tag?" · "+tag:"")),
   first?h("div",{},first+(items.length>1?"  (+"+(items.length-1)+" more)":"")):null),
  ...(plain?[h("div",{class:"mut",style:"margin:4px 0"},"No notes were written for this release.")]:
   items.map(function(i){return h("div",{style:"margin:6px 0"},h("div",{},i.subject),i.detail?h("div",{class:"mut"},i.detail):null)})),
  ...(older?[h("button",{style:"margin:6px 0",onclick:function(){back(c)}},"Roll back to this version")]:[]));
 return d}
// Roll back to a chosen release: it is installed like an update, so the services restart and the page reloads.
async function back(c){
 if(!await window.warnPop("Roll back to this version?",["The code goes back to version "+short(c.sha)+" of "+when(Date.parse(c.date)/1000)+". The services restart; any model call running now is cut off and done again afterwards.","Your settings, login, models and vault are not touched. A much older version may not understand settings made in newer ones. You can update again whenever you like."],"Roll back","Keep the current version"))return;
 const r=await fetch("/api/update/install",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({sha:c.sha})});const j=await r.json().catch(function(){return{}});
 if(!r.ok){msg.textContent=j.detail||"Could not start the rollback";return}follow()}
// What is in the updates you have not applied, newest first: each release, and the changes it brought.
function showChanges(s){
 if(!s.available){changes.replaceChildren();return}
 if(!(s.changes||[]).length){changes.replaceChildren(h("div",{class:"mut",style:"margin-top:12px"},"Could not load the list of changes"+(s.changes_note?" ("+s.changes_note+")":"")+"."));return}
 const n=s.changes.length;
 changes.replaceChildren(h("div",{style:"margin-top:12px;font-weight:600"},"Changes not applied yet ("+n+(n===1?" update":" updates")+")"),
  ...(s.changes_note?[h("div",{class:"mut"},s.changes_note)]:[]),
  h("div",{style:"max-height:45vh;overflow:auto;margin-top:6px"},...s.changes.map(function(c){return release(c,"")})))}
// The last 10 releases (all GitHub gives), collapsed: the installed one and the ones not applied yet are marked, and each older one can be rolled back to.
function showRecent(s){
 const list=s.recent||[];
 if(!list.length){recent.replaceChildren();return}
 const at=list.findIndex(function(c){return c.sha===s.installed});   // the releases after it in the list are older
 const seen=s.changes&&s.changes.length?new Set(s.changes.map(function(c){return c.sha})):new Set();
 const d=h("details",{class:"sec"},h("summary",{},"Recent releases (the last "+list.length+")"),
  ...list.map(function(c,i){return release(c,c.sha===s.installed?"installed":seen.has(c.sha)?"not applied yet":"",at>=0&&i>at)}));
 const was=recent.firstChild&&recent.firstChild.open;d.open=!!was;recent.replaceChildren(d)}
function show(s){showChanges(s);showRecent(s);info.replaceChildren(
 h("div",{},"Installed: ",h("b",{},short(s.installed)),s.latest?" · newest: ":"",s.latest?h("b",{},short(s.latest)):""),
 h("div",{class:"mut"},s.error?"Last check failed: "+s.error:s.available?"An update is available.":"Up to date."," Checked "+when(s.at)+"; it checks when you open this page."))}
// Opening the page shows the last answer at once, then asks GitHub for the newest version.
async function load(){try{const s=await api("/api/update/status");show(s);if(s.running){follow();return}
 msg.textContent="Checking...";const c=await api("/api/update/check",{method:"POST"});show(c);msg.textContent="";if(window.updBadge)window.updBadge()}catch(e){msg.textContent=""}}
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
const go=h("button",{class:"pri",onclick:async function(){
 if(!await window.warnPop("Update now?",["The newest version is downloaded and checked, the current code is backed up, and the services restart. Any model call running now is cut off and done again afterwards.","If the update fails its checks, the previous version is put back by itself."],"Update now","Not now"))return;
 const r=await fetch("/api/update/apply",{method:"POST"});const j=await r.json().catch(function(){return{}});
 if(!r.ok){msg.textContent=j.detail||"Could not start the update";return}follow()}},"Update now");
$("#upd").replaceChildren(h("div",{class:"card"},h("h3",{},"Updates"),info,h("div",{style:"margin-top:8px"},go,check),msg,changes,recent,log));
load();
})();
</script>
"""
