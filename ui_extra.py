"""Graph explorer + clarifying-question pages. Plugs into web.py with:  ui_extra.register(app, authed)
`authed(request) -> bool` is web.py's own login check, so these pages sit behind the same login."""
import html
import re
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import agent
import questions
import ui_nav
from graph import Vault

WL = re.compile(r"\[\[([^\]\|#]+?)(?:#[^\]\|]*)?(?:\|([^\]]*))?\]\]")
_cache = {"t": 0.0, "v": None}


def vault():
    if _cache["v"] is None or time.time() - _cache["t"] > 15:
        _cache["v"], _cache["t"] = Vault(agent.VAULT), time.time()
    return _cache["v"]


def is_file(v, rel):
    return rel in v.files


def file_chat(v, rel):
    """The chat a saved original came from: its version name ends with the first 8 characters of the chat's id."""
    if not rel.startswith("raw/files/"):
        return None
    if not hasattr(v, "_chat_by_id"):
        v._chat_by_id = {r[:-3][-8:]: r for r in sorted(v.body) if r.startswith("raw/conversations/")}
    return v._chat_by_id.get(Path(rel).stem.rsplit("-", 1)[-1])


def file_title(rel):
    parts = rel.split("/")   # raw/files/<name>/<stamp>-<chat>.<ext>
    stamp = Path(rel).name[:8]
    day = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if stamp.isdigit() else ""
    return f"{parts[2]} ({day})" if rel.startswith("raw/files/") and len(parts) > 3 and day else rel


def ntype(v, rel):
    if is_file(v, rel):
        return "derived-file" if rel.startswith("files/derived/") else "file"
    t = v.meta[rel].get("type")
    return str(t) if t else ("raw" if rel.startswith("raw/") else "note")


def degree(v, rel):
    if is_file(v, rel):
        return len(v.file_back.get(rel, ())) + (1 if file_chat(v, rel) else 0)
    return len(v.out[rel]) + len(v.back[rel]) + len(v.file_out.get(rel, ()))


def brief(v, rel):
    if is_file(v, rel):
        return {"path": rel, "title": file_title(rel), "type": ntype(v, rel), "deg": degree(v, rel)}
    return {"path": rel, "title": v.title(rel), "type": ntype(v, rel), "deg": degree(v, rel)}


def render_body(v, text):
    esc = html.escape(text, quote=False)

    def sub(m):
        target = html.unescape(m.group(1))
        label = m.group(2) or target
        dest = v.resolve(target) or v.resolve_file(target)
        if dest:
            return f'<a href="#" class="wl" data-p="{html.escape(dest, quote=True)}">{label}</a>'
        return f'<span class="bl">{label}</span>'
    return WL.sub(sub, esc)


def _around(v, rel):
    """Everything one step from a note or a saved file, for the little map."""
    if is_file(v, rel):
        chat = file_chat(v, rel)
        return set(v.file_back.get(rel, ())) | ({chat} if chat else set())
    return v.out[rel] | v.back[rel] | v.file_out.get(rel, set())


def _links_to(v, rel):
    if is_file(v, rel):
        chat = file_chat(v, rel)
        return {chat} if chat else set()
    return v.out[rel] | v.file_out.get(rel, set())


def hood(v, rel, hops):
    dist, queue = {rel: 0}, deque([rel])
    while queue:
        cur = queue.popleft()
        if dist[cur] >= hops:
            continue
        for nxt in sorted(_around(v, cur)):
            if nxt not in dist:
                dist[nxt] = dist[cur] + 1
                queue.append(nxt)
    keep = sorted(dist, key=lambda r: (dist[r], -degree(v, r), r))
    ks = set(keep)
    nodes = [dict(brief(v, r), dist=dist[r]) for r in keep]
    edges = [{"s": a, "t": b} for a in keep for b in _links_to(v, a) if b in ks]
    return {"center": rel, "nodes": nodes, "edges": edges, "truncated": False}


def register(app, authed):
    router = APIRouter()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    def note_or_404(v, path):
        if path not in v.body and path not in v.files:
            raise HTTPException(status_code=404, detail="no such note")
        return path

    @router.get("/api/graph/hubs")
    def hubs(request: Request):
        need(request)
        v = vault()   # everything in the vault, most connected first: notes, chats and every other file
        every = sorted([*v.body, *v.files], key=lambda r: (-degree(v, r), r))
        return {"stats": v.stats(), "notes": [brief(v, r) for r in every]}

    @router.get("/api/graph/search")
    def search(request: Request, q: str = ""):
        need(request)
        v, q = vault(), q.strip().lower()[:80]
        if not q:
            return {"notes": []}
        hits = []
        for r in v.body:
            names = [r.lower(), v.title(r).lower()] + [a.lower() for a in v.meta[r].get("aliases") or []
                                                         if isinstance(a, str)]
            if any(q in n for n in names):
                exact = any(n == q or n.rsplit("/", 1)[-1][:-3] == q for n in names)
                hits.append((not exact, r.startswith("raw/"), -degree(v, r), r))
        for f in v.files:   # the saved originals, by their name
            if q in f.lower():
                hits.append((q != Path(f).name.lower(), True, -degree(v, f), f))
        return {"notes": [brief(v, h[3]) for h in sorted(hits)]}

    @router.get("/api/graph/note")
    def note(request: Request, path: str):
        need(request)
        v = vault()
        rel = note_or_404(v, path)
        if is_file(v, rel):   # a saved original: its exact text, nothing added
            text = (agent.VAULT / rel).read_text(encoding="utf-8", errors="replace")
            chat = file_chat(v, rel)
            raw = rel.startswith("raw/files/")
            meta = {"type": ntype(v, rel), "filename": rel.split("/")[2] if raw else Path(rel).name,
                    "size": f"{len(text):,} characters"}
            if raw:
                meta.update({"saved": file_title(rel).split("(")[-1].rstrip(")"), "read-only": "yes"})
            return {**brief(v, rel), "meta": meta, "aliases": [], "out": [brief(v, chat)] if chat else [],
                    "back": [brief(v, r) for r in sorted(v.file_back.get(rel, ()))], "broken": [], "files": [],
                    "body": html.escape(text, quote=False), "truncated": False}
        meta = {k: str(val)[:200] for k, val in v.meta[rel].items() if k != "aliases"}
        return {**brief(v, rel), "meta": meta, "aliases": v.meta[rel].get("aliases") or [],
                "out": [brief(v, r) for r in sorted(v.out[rel])],
                "back": [brief(v, r) for r in sorted(v.back[rel])],
                "broken": sorted(v.unresolved[rel]), "files": [brief(v, f) for f in sorted(v.file_out.get(rel, ()))],
                "body": render_body(v, v.body[rel]), "truncated": False}

    @router.post("/api/graph/delete")
    async def delete_entry(request: Request):
        need(request)
        d = await request.json()
        path, v = str(d.get("path", "")), vault()
        if path not in v.body and path not in v.files:
            raise HTTPException(status_code=404, detail="no such note or file")
        root = Path(agent.VAULT).resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(status_code=404, detail="no such note or file")
        target.unlink()
        folder = target.parent
        if path.startswith("raw/files/") and folder != root / "raw" / "files" and not any(folder.iterdir()):
            folder.rmdir()   # the last saved version of a file: its empty folder goes too
        agent.log(f"deleted {path} from the List page")
        _cache["v"] = None
        return {"ok": True}

    @router.get("/api/graph/hood")
    def hood_api(request: Request, path: str, hops: int = 1):
        need(request)
        v = vault()
        return hood(v, note_or_404(v, path), 2 if hops >= 2 else 1)

    @router.get("/api/questions")
    def list_q(request: Request, status: str = "open"):
        need(request)
        if status == "closed":
            import qhistory
            return {"questions": [qhistory.view(q) for q in questions.list_questions() if q["status"] != "open"]}
        return {"questions": questions.list_questions(status)}

    @router.get("/api/questions/count")
    def count_q(request: Request):
        need(request)
        return {"open": questions.count_open()}

    @router.post("/api/questions/answer")
    async def answer_q(request: Request):
        need(request)
        data = await request.json()
        try:
            questions.answer(str(data.get("id", "")), data.get("answer", ""))
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "open": questions.count_open()}

    @router.post("/api/questions/dismiss")
    async def dismiss_q(request: Request):
        need(request)
        data = await request.json()
        try:
            questions.dismiss(str(data.get("id", "")))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="no such question")
        return {"ok": True, "open": questions.count_open()}

    @router.post("/api/questions/undo")
    async def undo_q(request: Request):
        need(request)
        data = await request.json()
        import qhistory
        try:
            qhistory.undo(str(data.get("id", "")))
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "open": questions.count_open()}

    @router.post("/api/questions/change")
    async def change_q(request: Request):
        need(request)
        data = await request.json()
        import qhistory
        try:
            qhistory.change(str(data.get("id", "")), str(data.get("answer", "")))
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "open": questions.count_open()}

    @router.get("/graph", response_class=HTMLResponse)
    def graph_page(request: Request):
        if not authed(request):
            return RedirectResponse("/login")
        return PAGE.replace("__BODY__", GRAPH_HTML).replace("__ACTIVE__", "graph")

    @router.get("/ask", response_class=HTMLResponse)
    def ask_page(request: Request):
        if not authed(request):
            return RedirectResponse("/login")
        return PAGE.replace("__BODY__", ASK_HTML).replace("__ACTIVE__", "ask")

    app.include_router(router)
    import ui_map
    ui_map.register(app, authed)
    import ui_todo
    ui_todo.register(app, authed)
    import ui_think
    ui_think.register(app, authed)
    import ui_settings
    ui_settings.register(app, authed)
    import ui_chat
    ui_chat.register(app, authed)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, interactive-widget=resizes-content">
<title>Memory vault</title>
__THEME_JS__
<style>
__THEME_CSS__
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,sans-serif}
nav{display:flex;gap:4px;padding:8px 12px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:5}
nav a{padding:8px 10px;white-space:nowrap;border-radius:8px;color:var(--mut);text-decoration:none;font-weight:600}
nav a.on{background:var(--acc);color:#fff}nav .n{background:var(--warn);color:#fff;border-radius:10px;padding:0 7px;margin-left:5px;font-size:12px}
main{padding:12px;max-width:760px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin:10px 0}
input[type=search],textarea{width:100%;padding:11px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);font:inherit}
button{font:inherit;padding:10px 14px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--ink);margin:4px 6px 4px 0}
button.pri{background:var(--acc);color:#fff;border-color:var(--acc)}
.chip{display:inline-block;padding:5px 10px;margin:3px 4px 3px 0;border-radius:14px;border:1px solid var(--line);background:var(--bg);cursor:pointer;font-size:14px}
.badge{font-size:12px;padding:1px 8px;border-radius:9px;color:#fff;margin-left:6px}
.mut{color:var(--mut);font-size:14px}h2,h3{margin:6px 0}
.body{white-space:pre-wrap;word-break:break-word;font-size:15px}.wl{color:var(--acc)}.bl{color:var(--warn)}
svg{width:100%;height:auto;background:var(--bg);border-radius:10px;touch-action:manipulation}
.legend span{margin-right:10px;font-size:12px;color:var(--mut)}.legend i{display:inline-block;width:10px;height:10px;border-radius:5px;margin-right:4px}
</style></head><body>
__DRAWER__
<script>
const $=s=>document.querySelector(s);
function h(t,a,...k){const e=document.createElement(t);for(const[x,y]of Object.entries(a||{})){if(typeof y==="function")e.addEventListener(x.slice(2),y);else if(x==="class")e.className=y;else e.setAttribute(x,y)}
 for(const c of k.flat())if(c!=null)e.append(c.nodeType?c:document.createTextNode(c));return e}
async function api(p,o){const r=await fetch(p,o);if(r.status===401){location="/login?next="+encodeURIComponent(location.pathname);throw 0}
 if(!r.ok){let m=r.statusText;try{m=(await r.json()).detail}catch(e){}throw new Error(m)}return r.json()}
async function badge(){try{const c=(await api("/api/questions/count")).open,b=$("#qn");b.hidden=!c;b.textContent=c}catch(e){}
 try{const c=(await api("/api/todo/count")).open,b=$("#tn");b.hidden=!c;b.textContent=c}catch(e){}}
const badgeTodo=badge;
// Push and hold to confirm: the fill is fast at first and slow at the end, and the phone buzzes when it takes effect.
function holdConfirm(title,text){return new Promise(res=>{
 const ov=h("div",{style:"position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:60;display:flex;align-items:center;justify-content:center;padding:16px"});
 const done=v=>{ov.remove();res(v)};
 const fill=h("div",{style:"position:absolute;left:0;top:0;bottom:0;width:0;background:#b91c1c"});
 const yes=h("button",{style:"position:relative;overflow:hidden;user-select:none;-webkit-user-select:none;-webkit-touch-callout:none;-webkit-tap-highlight-color:transparent;touch-action:none;margin-right:8px"},fill,h("span",{style:"position:relative"},"Hold to delete"));
 let t=null;
 const stop=()=>{clearTimeout(t);t=null;fill.style.transition="width .15s";fill.style.width="0"};
 yes.onpointerdown=e=>{e.preventDefault();fill.style.transition="width 1.5s cubic-bezier(.05,.8,.3,1)";fill.style.width="100%";t=setTimeout(()=>{if(navigator.vibrate)navigator.vibrate(40);done(true)},1500)};
 yes.onpointerup=stop;yes.onpointerleave=stop;yes.onpointercancel=stop;yes.oncontextmenu=e=>e.preventDefault();
 ov.append(h("div",{class:"card",style:"max-width:420px;width:100%"},h("h3",{},title),h("div",{class:"mut",style:"margin-bottom:12px"},text),yes,h("button",{class:"pri",onclick:()=>done(false)},"No")));
 document.body.append(ov)})}
(function(){const act="__ACTIVE__";document.querySelectorAll("nav a").forEach(a=>{if(a.getAttribute("href")==="/"+act)a.classList.add("on")})})();
badge();
</script>
<main>__BODY__</main>
</body></html>"""

PAGE = PAGE.replace("__DRAWER__", ui_nav.DRAWER).replace("__THEME_JS__", ui_nav.THEME_JS).replace("__THEME_CSS__", ui_nav.THEME_CSS)

GRAPH_HTML = r"""
<style>.lrow{display:flex;align-items:center;gap:10px;padding:10px 4px;border-bottom:1px solid var(--line);cursor:pointer}.lt{flex:1;min-width:0}.lt>div{overflow-wrap:anywhere}.lp{font-size:12px}
.tg{display:inline-block;font-size:13px;padding:4px 10px;margin:2px 4px 2px 0;border-radius:14px;border:1px solid var(--line);background:var(--card);cursor:pointer;user-select:none}.tg.off{opacity:.35}.tg i{display:inline-block;width:9px;height:9px;border-radius:5px;margin-right:6px}
.gdel{color:#f87171;background:none;border:0;padding:0 8px;font-size:13px;margin:0 0 0 6px}</style>
<input id="q" type="search" placeholder="Search notes, names, aliases..." autocomplete="off">
<div id="stats" class="mut" style="margin:6px 2px"></div>
<div id="view"></div>
<div id="tags"></div>
<div id="list"></div>
<script>
const COL={entity:"#5b8def",summary:"#34d399",timeline:"#f5a524",conversation:"#8b93a7",raw:"#8b93a7","claude-memory":"#c084fc",note:"#f472b6",file:"#22d3ee","file-note":"#22d3ee","updated-file":"#facc15","derived-file":"#a78bfa",hub:"#a3e635"};   // the fallback; the colors set under Settings, Tag colors replace them
const col=t=>COL[t]||COL.note;
function row(n){return h("div",{class:"lrow",onclick:()=>openNote(n.path)},h("div",{class:"lt"},h("div",{},n.title),h("div",{class:"mut lp"},n.path)),h("span",{class:"badge",style:"background:"+col(n.type)},n.type))}
function chip(n,on){return h("span",{class:"chip",onclick:()=>openNote(n.path)},n.title,h("span",{class:"badge",style:"background:"+col(n.type)},n.type))}
const hide=new Set();let curD=null,curLabel="";
async function showList(url,label){const d=await api(url);if(d.stats)$("#stats").textContent=d.stats.notes+" notes, "+d.stats.links+" links, "+d.stats.broken_links+" broken, "+d.stats.orphans+" unlinked";
 curD=d;curLabel=label;paint()}
function paint(){const d=curD,types={};d.notes.forEach(n=>types[n.type]=(types[n.type]||0)+1);
 const shown=d.notes.filter(n=>!hide.has(n.type));
 $("#tags").replaceChildren(...Object.keys(types).sort().map(t=>h("span",{class:"tg"+(hide.has(t)?" off":""),onclick:()=>{hide.has(t)?hide.delete(t):hide.add(t);paint()}},h("i",{style:"background:"+col(t)}),t+" "+types[t])));
 $("#list").replaceChildren(h("div",{class:"mut"},curLabel+" ("+shown.length+(shown.length<d.notes.length?" of "+d.notes.length:"")+")"),...shown.map(row))}
let t;$("#q").addEventListener("input",e=>{clearTimeout(t);t=setTimeout(()=>{const v=e.target.value.trim();
 v?showList("/api/graph/search?q="+encodeURIComponent(v),"Matches"):showList("/api/graph/hubs","Everything, most connected first")},250)});
let hops=1,cur=null;
function layout(d,W,H){const N=d.nodes,idx={};N.forEach((n,i)=>{idx[n.path]=i;const a=i*2.4,r=n.dist?60*n.dist+((i*37)%25):0;n.x=W/2+r*Math.cos(a);n.y=H/2+r*Math.sin(a);n.vx=n.vy=0});
 const E=d.edges.map(e=>[idx[e.s],idx[e.t]]);
 const its=Math.max(30,Math.min(260,Math.floor(4e7/(N.length*N.length+1))));   // a very busy note gets fewer passes so the page stays quick
 for(let it=0;it<its;it++){const cool=1-it/its;
  for(let i=0;i<N.length;i++)for(let j=i+1;j<N.length;j++){let dx=N[i].x-N[j].x,dy=N[i].y-N[j].y,d2=dx*dx+dy*dy+.1,f=2600/d2,dd=Math.sqrt(d2);
   N[i].vx+=f*dx/dd;N[i].vy+=f*dy/dd;N[j].vx-=f*dx/dd;N[j].vy-=f*dy/dd}
  for(const[a,b]of E){let dx=N[b].x-N[a].x,dy=N[b].y-N[a].y,dd=Math.sqrt(dx*dx+dy*dy)+.1,f=(dd-70)*.04;
   N[a].vx+=f*dx/dd;N[a].vy+=f*dy/dd;N[b].vx-=f*dx/dd;N[b].vy-=f*dy/dd}
  for(const n of N){if(n.dist===0){n.x=W/2;n.y=H/2;n.vx=n.vy=0;continue}
   n.vx+=(W/2-n.x)*.004;n.vy+=(H/2-n.y)*.004;n.x+=Math.max(-12,Math.min(12,n.vx))*cool;n.y+=Math.max(-12,Math.min(12,n.vy))*cool;n.vx*=.6;n.vy*=.6;
   n.x=Math.max(14,Math.min(W-14,n.x));n.y=Math.max(14,Math.min(H-14,n.y))}}
 return E}
function svg(d){const W=360,H=340,E=layout(d,W,H),N=d.nodes,S="http://www.w3.org/2000/svg",s=document.createElementNS(S,"svg");
 s.setAttribute("viewBox","0 0 "+W+" "+H);
 for(const[a,b]of E){const l=document.createElementNS(S,"line");l.setAttribute("x1",N[a].x);l.setAttribute("y1",N[a].y);l.setAttribute("x2",N[b].x);l.setAttribute("y2",N[b].y);l.setAttribute("stroke","#8892a6");l.setAttribute("stroke-opacity",".45");s.append(l)}
 for(const n of N){const c=document.createElementNS(S,"circle");c.setAttribute("cx",n.x);c.setAttribute("cy",n.y);const rad=n.dist?5+Math.min(n.deg,8):12;c.setAttribute("r",rad);
  c.setAttribute("fill",col(n.type));c.setAttribute("stroke",n.dist?"none":"#fff");c.setAttribute("stroke-width","2");c.style.cursor="pointer";c.addEventListener("click",()=>openNote(n.path));
  const tt=document.createElementNS(S,"title");tt.textContent=n.title;c.append(tt);s.append(c);
  if(n.dist===0||n.deg>=4||N.length<=14){const x=document.createElementNS(S,"text");x.setAttribute("x",n.x);x.setAttribute("y",n.y-rad-4);x.setAttribute("text-anchor","middle");x.setAttribute("font-size","10");
   x.setAttribute("fill","currentColor");x.setAttribute("pointer-events","none");x.textContent=n.title.length>22?n.title.slice(0,21)+"…":n.title;s.append(x)}}
 return s}
async function openNote(path){cur=path;const[n,g]=await Promise.all([api("/api/graph/note?path="+encodeURIComponent(path)),api("/api/graph/hood?path="+encodeURIComponent(path)+"&hops="+hops)]);
 const body=h("div",{class:"body"});body.innerHTML=n.body;body.querySelectorAll("a.wl").forEach(a=>a.addEventListener("click",e=>{e.preventDefault();openNote(a.dataset.p)}));
 const hopBtn=h("button",{onclick:()=>{hops=hops===1?2:1;openNote(cur)}},hops===1?"Show 2 hops":"Show 1 hop");
 const legend=h("div",{class:"legend"},...Object.entries({entity:1,summary:1,timeline:1,raw:1,file:1,"file-note":1,"updated-file":1,"claude-memory":1}).map(([k])=>h("span",{},h("i",{style:"background:"+col(k)}),k)));
 $("#view").replaceChildren(h("div",{class:"card"},
  h("h2",{},n.title,h("span",{class:"badge",style:"background:"+col(n.type)},n.type)),h("div",{class:"mut"},n.path),
  n.aliases.length?h("div",{class:"mut"},"aliases: "+n.aliases.join(", ")):null,
  svg(g),legend,h("div",{class:"mut"},g.nodes.length+" notes within "+hops+" hop"+(hops>1?"s":"")+(g.truncated?" (showing the closest "+g.nodes.length+")":"")),hopBtn,h("button",{class:"gdel",onclick:()=>deleteEntry(n)},"Delete")),
  h("div",{class:"card"},n.files&&n.files.length?h("div",{},h("h3",{},"Files ("+n.files.length+")"),...n.files.map(chip)):null,h("h3",{},"Links out ("+n.out.length+")"),...(n.out.length?n.out.map(chip):[h("span",{class:"mut"},"none")]),
   h("h3",{},"Backlinks ("+n.back.length+")"),...(n.back.length?n.back.map(chip):[h("span",{class:"mut"},"none")]),
   n.broken.length?h("div",{class:"mut",style:"color:var(--warn)"},"Broken links: "+n.broken.join(", ")):null),
  h("div",{class:"card"},h("div",{class:"mut"},Object.entries(n.meta).map(([k,v])=>k+": "+v).join("  ·  ")),h("hr"),body,n.truncated?h("div",{class:"mut"},"(shortened)"):null));
 window.scrollTo({top:$("#view").offsetTop-60,behavior:"smooth"})}
function refreshList(){const v=$("#q").value.trim();return v?showList("/api/graph/search?q="+encodeURIComponent(v),"Matches"):showList("/api/graph/hubs","Everything, most connected first")}
async function deleteEntry(n){
 if(!await holdConfirm("Delete "+n.title+"?",n.path+" will be deleted from the vault and cannot be undone. Anything that links to it will have a broken link."))return;
 try{await api("/api/graph/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:n.path})});
  history.replaceState(null,"",location.pathname);$("#view").replaceChildren(h("div",{class:"card mut"},"Deleted "+n.path));await refreshList()}
 catch(e){alert(e.message)}}
api("/api/tagcolors").then(d=>Object.assign(COL,d.colors)).catch(()=>{}).then(()=>{
 showList("/api/graph/hubs","Everything, most connected first");
 if(location.hash.length>1)openNote(decodeURIComponent(location.hash.slice(1)))});
</script>
"""

ASK_HTML = r"""
<div id="open"></div>
<style>
.hrow{padding:10px 0;border-top:1px solid var(--line)}
.hq{margin-bottom:4px}
.chip{font-size:12px;font-weight:600;padding:2px 10px;border-radius:10px;background:var(--line);color:var(--mut)}
.chip.queued{background:#f5a524;color:#000}
.chip.working{background:var(--acc);color:#fff}
.chip.done{background:#34d399;color:#000}
.hb{margin-top:8px}
</style>
<details class="card" open><summary class="mut">History (kept 30 days)</summary><div id="done"></div></details>
<script>
// A change to a file in a Git repository: exactly which repository, branch and file, a summary, and the diff line by line.
function diffBox(p){const lines=String(p.diff||"").split("\n").map(function(l){
  const c=l.startsWith("+++")||l.startsWith("---")?"var(--mut)":l.startsWith("+")?"#34d399":l.startsWith("-")?"#f87171":l.startsWith("@@")?"#60a5fa":"inherit";
  return h("div",{style:"color:"+c+";white-space:pre-wrap;word-break:break-word"},l||" ")});
 return h("div",{style:"margin:8px 0"},
  p.local?null:h("div",{},h("b",{},"Repository: "),p.repo+" ("+p.url+"), branch "+p.branch),
  h("div",{},h("b",{},p.local?"Vault note: ":"File: "),p.file),
  p.summary?h("div",{},h("b",{},"Summary: "),p.summary):null,
  p.local?null:h("div",{class:"mut",style:"margin-top:4px"},p.commit?"Approve and commit commits exactly this diff to that file and pushes it. Nothing else changes.":"This server is read only: approving only marks the draft approved."),
  h("div",{style:"font:12px/1.45 ui-monospace,Menlo,Consolas,monospace;max-height:60vh;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:8px;margin-top:6px"},...lines))}
function card(q){const c=h("div",{class:"card"},h("h3",{},q.question),q.why?h("div",{class:"mut"},q.why):null,q.payload&&q.payload.diff?diffBox(q.payload):null);
 const finish=async(path,body)=>{try{await api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});c.remove();badge();load()}catch(e){alert(e.message)}};
 for(const o of q.options)c.append(h("button",{class:"pri",onclick:()=>finish("/api/questions/answer",{id:q.id,answer:o.value})},o.label));
 if(q.kind==="free"){const t=h("textarea",{rows:"3",placeholder:"Your answer..."});c.append(t,h("button",{class:"pri",onclick:()=>t.value.trim()&&finish("/api/questions/answer",{id:q.id,answer:t.value})},"Send"))}
 c.append(h("button",{onclick:()=>finish("/api/questions/dismiss",{id:q.id})},"Skip"));return c}
const SL={queued:"Queued",working:"Working",done:"Done"};
function row(q){
 const act=async(path,body)=>{try{await api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});badge();load()}catch(e){alert(e.message)}};
 const r=h("div",{class:"hrow"},h("div",{class:"hq"},q.question),h("div",{},h("span",{class:"chip "+q.stage},SL[q.stage]||q.stage),h("span",{class:"mut"}," "+q.result)));
 if(q.undo){const b=h("div",{class:"hb"},h("button",{onclick:()=>act("/api/questions/undo",{id:q.id})},"Undo"));
  if(q.flip)b.append(h("button",{onclick:()=>act("/api/questions/change",{id:q.id,answer:q.flip})},"Change to "+(q.flip==="drop"?"Drop":"Keep")));
  r.append(b)}
 return r}
let allDone=false;
async function load(){const[o,d]=await Promise.all([api("/api/questions?status=open"),api("/api/questions?status=closed")]);
 $("#open").replaceChildren(...(o.questions.length?o.questions.map(card):[h("div",{class:"card mut"},"No questions right now. When the agent is unsure about something it will show up here.")]));
 const shown=allDone?d.questions:d.questions.slice(0,10);
 $("#done").replaceChildren(...shown.map(row),...(d.questions.length>shown.length?[h("button",{onclick:()=>{allDone=true;load()}},"See more ("+(d.questions.length-shown.length)+")")]:[]))}
load();
</script>
"""
