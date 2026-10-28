"""Queue tab, and a read-only viewer for saved file originals and changed copies."""
import difflib
import html
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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
"""
