"""Queue tab, and a read-only viewer for saved file originals and changed copies."""
import difflib
import html
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

import agent
import queuestate
import ui_extra

VAULT = agent.VAULT
ALLOWED = ("raw/files", "files/derived")
MAX_CHARS = 400000
PRE = "white-space:pre-wrap;word-break:break-word;font:13px/1.45 ui-monospace,Menlo,Consolas,monospace;margin:8px 0 0"


def colored(lines):
    out = []
    for l in lines[:2000]:
        if l.startswith("+") and not l.startswith("+++"):
            c = "#34d399"
        elif l.startswith("-") and not l.startswith("---"):
            c = "#f87171"
        else:
            c = "inherit"
        out.append(f'<span style="color:{c}">{html.escape(l)}</span>')
    return "\n".join(out)


def view(path):
    target = (VAULT / path).resolve()
    roots = [(VAULT / a).resolve() for a in ALLOWED]
    if not any(r in target.parents for r in roots) or not target.is_file():
        raise HTTPException(status_code=404, detail="no such file")
    rel = target.relative_to(VAULT.resolve()).as_posix()
    text = target.read_text(encoding="utf-8", errors="replace")
    body = (f'<div class="card"><h3>{html.escape(target.name)}</h3>'
            f'<div class="mut">{html.escape(rel)} · {len(text):,} characters</div></div>')
    if rel.startswith("files/derived/"):
        name = rel.split("/")[2]
        odir = VAULT / "raw" / "files" / name
        vers = sorted(odir.glob("*")) if odir.is_dir() else []
        if vers:
            orig = vers[-1].read_text(encoding="utf-8", errors="replace")
            diff = list(difflib.unified_diff(orig.splitlines(), text.splitlines(), "original", "changed", lineterm="", n=3))
            body += (f'<div class="card"><h3>Changes from the original</h3><pre style="{PRE}">'
                     + (colored(diff) if diff else "No differences.") + "</pre></div>")
    body += f'<div class="card"><h3>Contents</h3><pre style="{PRE}">{html.escape(text[:MAX_CHARS])}</pre></div>'
    return body


def register(app, authed):
    router = APIRouter()

    @router.get("/api/queue")
    def queue(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        return queuestate.snapshot()

    @router.get("/api/jobs")
    def job_list(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        import jobs
        return {"jobs": [{k: j.get(k) for k in ("id", "kind", "target", "instructions", "status", "error", "created",
                                                 "worker", "result")} for j in jobs.all_jobs()[-50:]][::-1]}

    @router.post("/api/jobs/act")
    async def job_act(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        import jobs
        d = await request.json()
        jid, a = str(d.get("id", "")), d.get("action")
        try:
            if a == "cancel":
                jobs.cancel(jid)
            elif a == "again":
                jobs.requeue(jid)
            elif a in ("approve", "reject"):
                jobs.decide(jid, a == "approve")
            else:
                raise ValueError("unknown action")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @router.get("/api/jobs/draft")
    def job_draft(request: Request, id: str = ""):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        import jobs
        j = jobs.get(id) if re.fullmatch(r"[\w-]{1,40}", id) else None
        p = jobs.DIR / f"{id}.draft"
        if not j or not p.exists():
            raise HTTPException(status_code=404, detail="no draft")
        name = (j.get("result") or {}).get("path", "draft").rsplit("/", 1)[-1]
        return PlainTextResponse(p.read_text(encoding="utf-8"), headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @router.get("/queue", response_class=HTMLResponse)
    def queue_page(request: Request):
        if not authed(request):
            return RedirectResponse("/", status_code=303)
        return ui_extra.PAGE.replace("__ACTIVE__", "queue").replace("__BODY__", HTML)

    @router.get("/file", response_class=HTMLResponse)
    def file_page(request: Request, path: str = ""):
        if not authed(request):
            return RedirectResponse("/", status_code=303)
        return ui_extra.PAGE.replace("__ACTIVE__", "file").replace("__BODY__", view(path))

    app.include_router(router)


HTML = r"""
<style>
.qh{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.qh i{width:10px;height:10px;border-radius:5px;display:inline-block}
.qh b{font-size:17px}
.qc{font-size:12px;font-weight:600;padding:2px 10px;border-radius:10px;background:var(--line);color:var(--mut)}
.qc.on{background:var(--acc);color:#fff}
.qn{padding:10px 0 2px;font-size:12px;font-weight:600;color:var(--mut);text-transform:uppercase}
.qr{padding:8px 0;border-top:1px solid var(--line)}
.qs{font-size:13px}
</style>
<div id="qp"></div>
<script>
(function(){
const M=[["sweeper","Sweeper","#4c9aff"],["reviewer","Reviewer","#f5a623"],["curator","Curator","#34d399"]];
let D=null;const SHOWN={};
function dur(s){s=Math.max(0,Math.floor(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?h+"h "+m+"m":m?m+"m "+(s%60)+"s":s+"s"}
function render(){
 if(!D)return;
 $("#qp").replaceChildren(...M.map(function(a){
  const k=a[0],name=a[1],col=a[2],n=D.now[k]||{},x=D.next[k]||{};
  const card=h("div",{class:"card"});
  card.append(h("div",{class:"qh"},h("i",{style:"background:"+col}),h("b",{},name),h("span",{class:"qc"+(n.state==="working"?" on":"")},n.state||"")));
  card.append(h("div",{class:"qn"},"Now"));
  const calls=n.calls||[];
  if(!calls.length)card.append(h("div",{class:"mut qs"},n.state==="idle"?"Nothing running right now.":"Not running."));
  calls.forEach(function(c){card.append(h("div",{class:"qr"},h("div",{},c.job+(c.what?": "+c.what:"")),c.since?h("div",{class:"mut qs"},"running for "+dur(Date.now()/1000-c.since)):null))});
  card.append(h("div",{class:"qn"},"Up next"+(x.total!=null?" ("+x.total+")":"")));
  if(x.error){card.append(h("div",{class:"mut qs"},"Could not read: "+x.error))}
  else{
   (x.notes||[]).forEach(function(t){card.append(h("div",{class:"mut qs"},t))});
   (x.items||[]).slice(0,SHOWN[k]||10).forEach(function(i){card.append(h("div",{class:"qr"},h("div",{},i.text),h("div",{class:"mut qs"},i.sub||"")))});
   const all=(x.items||[]).length,shown=Math.min(SHOWN[k]||10,all);if(shown<all){card.append(h("button",{onclick:function(){SHOWN[k]=shown+10;render()}},"See more ("+(all-shown)+" left)"))}else if(x.total>all){card.append(h("div",{class:"mut qs"},"and "+(x.total-all)+" more not listed"))}
   if(!(x.items||[]).length)card.append(h("div",{class:"mut qs"},"Nothing waiting."));
   if(x.waiting&&x.waiting.length){card.append(h("div",{class:"qn"},"Waiting for you"));x.waiting.forEach(function(i){card.append(h("div",{class:"qr"},h("div",{},i.text),h("div",{class:"mut qs"},i.sub||"")))})}
  }
  return card}))}
async function load(){try{D=await api("/api/queue");render()}catch(e){}}
load();setInterval(function(){if(!document.hidden)load()},5000);
setInterval(function(){if(!document.hidden)render()},2000);
})();
</script>
<div id="jq"></div>
<script>
(function(){
const ST={queued:"Queued",running:"Working",waiting:"Waiting for you",approved:"Approved",rejected:"Rejected",failed:"Failed",cancelled:"Cancelled"};
const KIND={doc_draft:"Draft a document",note_update:"Change a note",sort_note:"Sort a note"};
const OPEN={};
async function act(id,action){const r=await fetch("/api/jobs/act",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:id,action:action})});
 if(!r.ok){const j=await r.json().catch(function(){return{}});alert(j.detail||"Could not do that")}load()}
function detail(j){const r=j.result||{},out=[];
 if(j.instructions)out.push(h("div",{class:"mut"},"Asked: "+j.instructions));
 if(r.summary)out.push(h("div",{},r.summary));
 if(r.old!=null&&r.note)out.push(h("div",{},"In "+r.note+': "'+r.old+'" → "'+r.new+'"'),h("div",{class:"mut"},r.why+(r.source?' Quote from '+r.source.note+': "'+r.source.quote+'"':"")));
 if(r.claims)r.claims.forEach(function(c){out.push(h("div",{},"→ "+c.note+": "+c.statement),h("div",{class:"mut"},'"'+c.quote+'"'))});
 if(r.diff){out.push(h("pre",{style:"white-space:pre-wrap;word-break:break-word;font:12px/1.4 ui-monospace,Menlo,Consolas,monospace;max-height:50vh;overflow:auto"},r.diff));
  out.push(h("a",{href:"/api/jobs/draft?id="+encodeURIComponent(j.id)},"Download the draft"));
  if(j.status==="approved")out.push(h("div",{class:"mut"},"Approved. Nothing was pushed"+(r.access==="write"?"":" ("+r.server+" is read only)")+": apply the draft to "+r.server+"/"+r.path+" yourself."))}
 if(r.filed)out.push(h("div",{class:"mut"},"Filed to "+r.filed+". The inbox agent turns it into a proposal; the vault itself is not changed yet."));
 if(j.error)out.push(h("div",{style:"color:#f5a524"},j.error));
 return out}
function row(j){
 const d=h("details",{class:"sec"},h("summary",{},(KIND[j.kind]||j.kind)+(j.target?" · "+j.target:"")+" · "+(ST[j.status]||j.status)),...detail(j));
 if(j.status==="waiting")d.append(h("button",{class:"pri",onclick:function(){act(j.id,"approve")}},"Approve"),h("button",{onclick:function(){act(j.id,"reject")}},"Reject"));
 if(j.status==="queued")d.append(h("button",{onclick:function(){act(j.id,"cancel")}},"Cancel"));
 if(j.status==="failed")d.append(h("button",{onclick:function(){act(j.id,"again")}},"Queue again"));
 d.open=!!OPEN[j.id];d.ontoggle=function(){OPEN[j.id]=d.open};return d}
async function load(){try{const d=await api("/api/jobs");
 $("#jq").replaceChildren(d.jobs.length?h("div",{class:"card"},h("h3",{},"Jobs from the Assistant"),...d.jobs.map(row)):"")}catch(e){}}
load();setInterval(function(){if(!document.hidden)load()},10000);
})();
</script>
"""
