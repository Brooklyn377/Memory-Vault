"""Thinking tab: one live chat of both models' thinking, plus token stats. Registered from ui_extra.register()."""
import json
import time
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import thinklog
import ui_extra

SERVERS = {"qwen": "http://127.0.0.1:8080", "gpt": "http://127.0.0.1:8081", "cur": "http://127.0.0.1:8082",
           "pa": "http://127.0.0.1:8083"}
BUCKET = 30  # seconds per point on the speed chart
CHAR_BUDGET = 16000  # characters of thinking text loaded per scroll


def _files():
    return sorted(thinklog.DIR.glob("think-*.jsonl"))


def _records(path, model):
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("m") == model and isinstance(r.get("t"), int):
                    out.append(r)
    except OSError:
        pass
    out.sort(key=lambda r: r["t"])
    return out


def _row(r):
    # t and c go out as strings: nanosecond times are too big for a JavaScript number
    return {"t": str(r["t"]), "m": r["m"], "c": str(r.get("c", "")), "k": r.get("k"),
            "s": r.get("s", ""), "n": r.get("n", 0)}


def _older_for(model, before, limit):
    files = _files()
    got, more = [], False
    for i in range(len(files) - 1, -1, -1):
        got = [r for r in _records(files[i], model) if r["t"] < before] + got
        if len(got) > limit:
            more = True
            break
    return [_row(r) for r in got[-limit:]], more


def read_older(before, limit):
    rows, more = [], False
    for m in SERVERS:
        r, mo = _older_for(m, before, limit)
        rows += r
        more = more or mo
    rows.sort(key=lambda x: int(x["t"]))
    keep, used = [], 0
    for r in reversed(rows):
        used += len(r.get("s", "")) + 40
        if keep and used > CHAR_BUDGET:
            more = True
            break
        keep.append(r)
    keep.reverse()
    return keep, more


def read_newer(after):
    got = []
    for m in SERVERS:
        for p in _files()[-2:]:
            got += [_row(r) for r in _records(p, m) if r["t"] > after]
    got.sort(key=lambda x: int(x["t"]))
    return got[:500]


def rate(model, minutes):
    n = minutes * 60 // BUCKET
    span = BUCKET * 10 ** 9
    start = time.time_ns() - n * span
    vals = [0] * n
    for p in _files()[-3:]:
        for r in _records(p, model):
            if r.get("k") in ("r", "o") and r["t"] >= start:
                vals[min(n - 1, (r["t"] - start) // span)] += r.get("n", 0)
    return [round(v / BUCKET, 2) for v in vals]


def _base(model):
    try:
        import remote
        return remote.base(model) or SERVERS[model] if remote.enabled(model) else SERVERS[model]
    except Exception:
        return SERVERS[model]


def live(model):
    """The call running now, from the agent's own log: for an external server whose /slots cannot be read.
    -> (call id, tokens generated so far), or None."""
    call, n = None, 0
    for p in _files()[-2:]:
        for r in _records(p, model):
            if r.get("k") == "start":
                call, n = r.get("c"), 0
            elif r.get("c") == call:
                if r.get("k") == "end":
                    call = None
                elif r.get("k") in ("r", "o"):
                    n += int(r.get("n", 0))
    return (call, n) if call else None


def slots(model):
    try:
        req = urllib.request.Request(_base(model) + "/slots")
        import remote
        if remote.enabled(model) and remote.saved_key(model):   # an external server with a key wants it here too
            req.add_header("Authorization", "Bearer " + remote.saved_key(model))
        with urllib.request.urlopen(req, timeout=2) as r:
            sl = json.load(r)
    except Exception:
        import swap
        try:
            import remote
            if remote.enabled(model):   # no /slots from that server: what this agent sent it, from the log
                import settings
                now = live(model)
                return {"up": remote.health(model) == "ready", "busy": bool(now), "calls": 1 if now else 0,
                        "task": now[0] if now else "", "ctx": int(settings.get(model)["context"]),
                        "used": now[1] if now else 0, "decoded": now[1] if now else 0}
        except Exception:
            pass
        return {"up": False, "swapped": swap.holding()}
    busy = [x for x in sl if x.get("is_processing")]

    def dec(x):
        return ((x.get("next_token") or [{}])[0]).get("n_decoded", 0)

    used = sum((x.get("n_prompt_tokens") or 0) + dec(x) for x in busy)
    return {"up": True, "busy": bool(busy), "calls": len(busy), "slots": len(sl),
            "task": ",".join(str(x.get("id_task")) for x in busy),
            "ctx": sl[0].get("n_ctx"), "used": used,
            "decoded": sum(dec(x) for x in busy)}


def _login_path(app):
    for r in app.routes:
        p = getattr(r, "path", "")
        if "login" in p and "GET" in (getattr(r, "methods", None) or []):
            return p
    return "/"


def register(app, authed):
    router = APIRouter()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/think/log")
    def think_log(request: Request, limit: int = 50, before: str = "", after: str = ""):
        need(request)
        if after != "":
            return {"rows": read_newer(int(after) if after.isdigit() else 0), "more": False}
        limit = max(1, min(limit, 5000))
        rows, more = read_older(int(before) if before.isdigit() else 2 ** 63, limit)
        return {"rows": rows, "more": more}

    @router.get("/api/think/stats")
    def think_stats(request: Request):
        need(request)
        return {"slots": {m: slots(m) for m in SERVERS}, "totals": thinklog.totals()}

    @router.get("/api/think/mem")
    def think_mem(request: Request):
        need(request)
        import memview
        return memview.memory()

    @router.get("/api/think/rate")
    def think_rate(request: Request, minutes: int = 60):
        need(request)
        minutes = max(5, min(minutes, 120))
        return {m: rate(m, minutes) for m in SERVERS}

    @router.get("/think", response_class=HTMLResponse)
    def think_page(request: Request):
        if not authed(request):
            return RedirectResponse(_login_path(app), status_code=303)
        return ui_extra.PAGE.replace("__BODY__", THINK_HTML).replace("__ACTIVE__", "think")

    app.include_router(router)


THINK_HTML = r"""
<style>
.tk{display:flex;flex-direction:column;height:calc(100vh - 64px);height:calc(100dvh - 64px);padding:10px 12px;gap:10px;min-height:0}
.chat{flex:1;min-height:180px;overflow-y:auto;border:1px solid var(--line);border-radius:12px;background:var(--card);padding:10px;font:13px/1.45 ui-monospace,Menlo,Consolas,monospace}
.chat .more{color:var(--mut);text-align:center;font-size:12px;padding:2px 0 8px}
.blk{border-left:3px solid var(--c);padding:2px 0 2px 10px;margin:0 0 14px}
.bh{font:700 12px sans-serif;color:var(--c);margin-bottom:3px}
.bb{white-space:pre-wrap;word-break:break-word}
.bb .r{color:var(--mut);font-style:italic}
.bot{border:1px solid var(--line);border-radius:12px;background:var(--card);padding:8px 10px}
#cv{width:100%;height:100px;display:block}
.mrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13px;margin-top:6px}
.mrow i{width:10px;height:10px;border-radius:5px;display:inline-block}
.mrow .nm{color:var(--mut)}
.mrow .nm b{color:inherit;font-variant-numeric:tabular-nums}
.st{font-size:12px;font-weight:600;padding:2px 10px;border-radius:10px;background:var(--line);color:var(--mut);white-space:nowrap}
.st.on{background:var(--acc);color:#fff}
.tot{margin-top:8px;padding-top:8px;border-top:1px solid var(--line);font-size:13px;color:var(--mut)}
.tot b{color:inherit;font-variant-numeric:tabular-nums}
</style>
<div class="tk">
  <div class="chat" id="chat"><div class="more" id="head"></div></div>
  <div class="bot">
    <canvas id="cv"></canvas>
    <div class="mrow" id="row-qwen"><i style="background:#4c9aff"></i><b>Sweeper</b><span class="st">…</span><span class="nm"></span></div>
    <div class="mrow" id="row-gpt"><i style="background:#f5a623"></i><b>Reviewer</b><span class="st">…</span><span class="nm"></span></div>
    <div class="mrow" id="row-cur"><i style="background:#34d399"></i><b>Curator</b><span class="st">...</span><span class="nm"></span></div>
    <div class="mrow" id="row-pa"><i style="background:#e879f9"></i><b>Assistant</b><span class="st">...</span><span class="nm"></span></div>
    <div class="tot" id="tot"></div>
  </div>
</div>
<div class="bot" style="margin:0 12px 16px"><div id="mem"></div></div>
<script>
(function(){
const M={qwen:{name:"Sweeper",color:"#4c9aff"},gpt:{name:"Reviewer",color:"#f5a623"},cur:{name:"Curator",color:"#34d399"},pa:{name:"Assistant",color:"#e879f9"}};
const IDS=Object.keys(M);
const chat=document.getElementById("chat"),head=document.getElementById("head"),cv=document.getElementById("cv"),tot=document.getElementById("tot");
const B={},S={qwen:{kind:"",prev:null},gpt:{kind:"",prev:null},cur:{kind:"",prev:null},pa:{kind:"",prev:null}},E={};
let oldest="0",newest="0",more=false,loading=false,ticking=false,rates={qwen:[],gpt:[],cur:[],pa:[]};
IDS.forEach(m=>{E[m]={st:document.querySelector("#row-"+m+" .st"),nm:document.querySelector("#row-"+m+" .nm")};});
const j=async u=>{const r=await fetch(u,{cache:"no-store"});if(!r.ok)throw new Error(r.status);return r.json();};
const fmt=n=>Number(n||0).toLocaleString();

function headText(){head.textContent=more?"↑ scroll up for older":(oldest==="0"?"waiting for the first model call…":"— start of log —");}

function hdr(o){
  o.h.textContent=M[o.m].name+(o.lab?" · "+o.lab:"")+" · "+new Date(o.t).toLocaleTimeString()+(o.tok!=null?" · "+fmt(o.tok)+" tokens":"")+(o.why?" · "+o.why:"");
}
function mk(r){
  const el=document.createElement("div");el.className="blk";el.style.setProperty("--c",M[r.m].color);
  const h=document.createElement("div");h.className="bh";
  const b=document.createElement("div");b.className="bb";
  el.append(h,b);
  const o={el:el,h:h,b:b,m:r.m,t:Math.floor(Number(r.c)/1e6),lab:"",tok:null,kind:""};
  hdr(o);B[r.c]=o;return o;
}
function line(o,r){
  if(r.k==="start"){o.lab=r.s;hdr(o);return null;}
  if(r.k==="end"){o.tok=r.n;const s=r.s||"",i=s.search(/\((failed|cut off)/);o.why=i<0?"":s.slice(i+1,-1);hdr(o);return null;}
  const s=document.createElement("span");s.textContent=r.s;if(r.k==="r")s.className="r";return s;
}

function put(rows,top){
  const atEnd=chat.scrollHeight-chat.scrollTop-chat.clientHeight<40,h0=chat.scrollHeight;
  const groups=new Map();
  rows.forEach(r=>{if(!groups.has(r.c))groups.set(r.c,[]);groups.get(r.c).push(r);});
  const frag=document.createDocumentFragment();
  groups.forEach(rs=>{
    let o=B[rs[0].c];const fresh=!o;
    if(fresh)o=mk(rs[0]);
    const nodes=document.createDocumentFragment();
    let pk=top?"":o.kind;
    rs.forEach(r=>{
      const n=line(o,r);
      if(!n)return;
      if(pk&&pk!==r.k)nodes.appendChild(document.createTextNode("\n"));
      nodes.appendChild(n);pk=r.k;
    });
    if(top)o.b.insertBefore(nodes,o.b.firstChild);
    else{o.b.appendChild(nodes);o.kind=pk;}
    if(fresh)frag.appendChild(o.el);
  });
  if(top){chat.insertBefore(frag,head.nextSibling);chat.scrollTop+=chat.scrollHeight-h0;}
  else{chat.appendChild(frag);if(atEnd)chat.scrollTop=chat.scrollHeight;
    rows.forEach(r=>{S[r.m].kind=(r.k==="r"||r.k==="o")?r.k:"";});}
}

async function older(){
  if(loading||!more||oldest==="0")return;
  loading=true;
  try{
    const d=await j("/api/think/log?limit=3000&before="+oldest);
    more=d.more;
    if(d.rows.length){put(d.rows,true);oldest=d.rows[0].t;}
    headText();
  }catch(e){}
  loading=false;
}
chat.addEventListener("scroll",()=>{if(chat.scrollTop<30)older();});

async function boot(){
  try{
    const d=await j("/api/think/log?limit=3000");
    more=d.more;
    if(d.rows.length){put(d.rows,false);oldest=d.rows[0].t;newest=d.rows[d.rows.length-1].t;chat.scrollTop=chat.scrollHeight;}
    headText();
  }catch(e){}
  for(let i=0;i<10&&more&&chat.scrollHeight<=chat.clientHeight+40;i++)await older();
}

function show(m,s){
  const e=E[m],q=S[m];
  if(!e||!e.st||!s)return;   // a model without a row on this page
  if(!s.up){e.st.textContent=m==="pa"?"not loaded":m==="cur"?(s.swapped?"loading":"unloaded"):(s.swapped?"unloaded (curator is working)":"server down");e.st.className="st";e.nm.innerHTML="this call <b>0</b> · <b>–</b> tok/s · context <b>–</b>";return;}
  const d=s.busy?s.decoded:0;
  let ph="idle";
  if(s.busy)ph=s.decoded>0?(q.kind==="r"?"thinking":q.kind==="o"?"answering":"generating"):"reading prompt";
  e.st.textContent=ph;e.st.className="st"+(s.busy?" on":"");
  const now=Date.now();let tps=null;
  if(!q.w||q.task!==s.task){q.w=[];q.task=s.task;}
  q.w.push([now,s.decoded]);
  while(q.w.length>2&&now-q.w[0][0]>30000)q.w.shift();
  const a=q.w[0],z=q.w[q.w.length-1];
  if(z[0]>a[0]&&z[1]>=a[1])tps=(z[1]-a[1])/((z[0]-a[0])/1000);
  e.nm.innerHTML=(s.calls>1?"combined, "+s.calls+" calls":"this call")+" <b>"+fmt(d)+"</b> · <b>"+(s.busy&&tps!==null?tps.toFixed(2):"–")+"</b> tok/s · context <b>"+fmt(s.used||0)+" / "+fmt(s.ctx)+"</b>"+(s.slots>1?" · <b>"+s.slots+"</b> slots":"");
}

function showTot(t,sl){
  let sum=0;const parts=[];
  IDS.forEach(m=>{
    const s=sl[m],v=(t[m]||0)+(s&&s.up&&s.busy?s.decoded:0);
    sum+=v;parts.push('<span style="color:'+M[m].color+'">'+M[m].name+' <b>'+fmt(v)+'</b></span>');
  });
  tot.innerHTML="All-time tokens generated <b>"+fmt(sum)+"</b> · "+parts.join(" · ");
}

const MEMC={qwen:"#4c9aff",gpt:"#f5a623",cur:"#34d399",pa:"#e879f9"},MEMN={qwen:"Sweeper",gpt:"Reviewer",cur:"Curator",pa:"Assistant"};
const gbs=n=>(n/1073741824).toFixed(1);
function showMem(d){
  const el=document.getElementById("mem");if(!el||!d||!d.total)return;
  const T=d.total;
  let bar='<div style="position:relative;display:flex;height:14px;border-radius:7px;overflow:hidden;background:var(--line);margin:6px 0">';
  IDS.forEach(k=>{const m=d.models[k];if(m&&m.rss)bar+='<div style="width:'+(m.rss/T*100)+'%;background:'+MEMC[k]+'"></div>'});
  bar+='<div style="width:'+(d.other/T*100)+'%;background:#8b93a7"></div>';
  bar+='</div>';
  let rows="";
  IDS.forEach(k=>{const m=d.models[k];if(!m)return;
    const grow=m.exp>m.rss*1.05?' · expected '+gbs(m.exp)+' GB when fully loaded':'';
    rows+='<div><span style="color:'+MEMC[k]+'">'+MEMN[k]+'</span> <b>'+gbs(m.rss)+' GB</b> (weights '+gbs(m.file)+' + working '+gbs(m.anon)+')'+grow+'</div>'});
  if(!rows)rows='<div>No model servers loaded.</div>';
  const sw=d.swap_total?' · swap '+gbs(d.swap_used)+' / '+gbs(d.swap_total)+' GB'+(d.swap_used>d.swap_total*0.9?' (nearly full)':''):'';
  el.innerHTML='Container memory <b>'+gbs(T)+' GB</b> · models loaded here '+gbs(d.models_total)+' GB + other '+gbs(d.other)+' GB'+bar+rows+'<div style="margin-top:4px">file cache '+gbs(d.cached)+' GB (model files count here) · free '+gbs(d.free)+' GB'+sw+'</div>'}
function draw(){
  const w=cv.clientWidth,h=cv.clientHeight,dpr=window.devicePixelRatio||1;
  if(!w)return;
  cv.width=w*dpr;cv.height=h*dpr;
  const g=cv.getContext("2d");g.scale(dpr,dpr);
  const cs=getComputedStyle(document.body);
  const mut=cs.getPropertyValue("--mut").trim()||"#888",ln=cs.getPropertyValue("--line").trim()||"#444";
  let top=0.1;IDS.forEach(m=>{top=Math.max(top,...rates[m]);});
  const base=h-16,span=base-18;
  g.strokeStyle=ln;g.lineWidth=1;g.beginPath();g.moveTo(0,base+.5);g.lineTo(w,base+.5);g.stroke();
  IDS.forEach(m=>{
    const v=rates[m];g.strokeStyle=M[m].color;g.lineWidth=2;g.beginPath();
    v.forEach((y,i)=>{const x=v.length>1?i/(v.length-1)*w:0,yy=base-y/top*span;if(i)g.lineTo(x,yy);else g.moveTo(x,yy);});
    g.stroke();
  });
  g.font="11px sans-serif";g.fillStyle=mut;g.textAlign="left";
  g.fillText("peak "+top.toFixed(2)+" tok/s",2,11);g.fillText("1 h ago",2,h-3);
  g.textAlign="right";g.fillText("now",w-2,h-3);
  let x=w-2;
  IDS.slice().reverse().forEach(m=>{g.fillStyle=M[m].color;g.fillText(M[m].name,x,11);x-=g.measureText(M[m].name).width+12;});
}

async function tick(){
  if(ticking)return;
  ticking=true;
  try{
    try{
      const d=await j("/api/think/log?after="+newest);
      if(d.rows.length){
        if(oldest==="0"){oldest=d.rows[0].t;headText();}
        put(d.rows,false);newest=d.rows[d.rows.length-1].t;
      }
    }catch(e){}
    try{
      const d=await j("/api/think/stats");
      IDS.forEach(m=>{try{show(m,d.slots[m])}catch(e2){}});   // one model's row can never blank the rest
      try{showTot(d.totals,d.slots)}catch(e2){}
    }catch(e){}
    try{showMem(await j("/api/think/mem"))}catch(e){}
  }finally{ticking=false;}
}
async function getRates(){try{rates=await j("/api/think/rate?minutes=60");draw();}catch(e){}}

addEventListener("resize",draw);
(async()=>{await boot();tick();setInterval(tick,2000);getRates();setInterval(getRates,30000);})();
})();
</script>
"""
