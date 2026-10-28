"""Settings page, server part: model file, context, threads and slots for each llama-server."""
import os
import re
import threading

from fastapi import APIRouter, HTTPException, Request

import applier
import gguf_info
import remote
import serverflags
import settings

MIN_CTX = 2048        # the usual lowest context; below it is a warning
HARD_MIN_CTX = 512    # below this nothing fits, so it is refused
MAX_SLOTS = 8
NAME = re.compile(r"[\w.\-]+\.gguf")


def _models():
    return [p.name for p in sorted(gguf_info.MODELS.glob("*.gguf"))]


def _threads_max():
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def limits(model, file, parallel, strict=True):
    """The largest context estimated to fit in memory. strict: refuse when even MIN_CTX does not fit."""
    if not NAME.fullmatch(file) or not (gguf_info.MODELS / file).is_file():
        raise ValueError("pick a model from the list")
    others = [(v["model_file"], v["context"], v["parallel"]) for k, v in settings.load().items() if k != model and "cur" not in (k, model)]
    try:
        cap = gguf_info.max_context(file, others, parallel)
    except Exception:
        raise ValueError("cannot read that model file, so its memory use cannot be estimated")
    if strict and cap < MIN_CTX:
        raise ValueError("not enough memory for this model next to the other server")
    return cap


NICE = {"qwen": "Sweeper", "gpt": "Reviewer", "cur": "Curator"}


def _plan(d):
    """Check every change on the page. Returns (the server restarts needed, warnings); raises on the first value that
    cannot work. A value outside its usual range, or a context larger than the memory estimate, is only a warning:
    a server that does not come up ready is rolled back by applier.apply_now."""
    import tuning
    import ui_settings
    s, pend, jobs, warn = settings.load(), applier.pending(), {}, []
    prop = settings.load()   # the model settings as they would be after this change
    for m in ("qwen", "gpt", "cur"):
        cur, f = s[m], (d.get("servers") or {}).get(m)
        vals = None
        if f and f.get("remote") is not None:
            remote.check(m, f["remote"])
            if f["remote"].get("enabled"):
                f = None
        if f:
            vals = {"model_file": str(f.get("file", "")), "context": int(f.get("context")),
                    "threads": int(f.get("threads")), "parallel": int(f.get("parallel")),
                    "flags": serverflags.check(f.get("flags") or {}, str(f.get("file", "")), cur.get("flags"), warn)}
        if vals and any(cur.get(k, {} if k == "flags" else None) != v for k, v in vals.items()):
            if vals["threads"] < 1 or vals["parallel"] < 1:
                raise ValueError(f"{NICE[m]}: threads and slots must be at least 1")
            if vals["threads"] > _threads_max() and vals["threads"] != cur["threads"]:
                warn.append(f"{NICE[m]}: {vals['threads']} threads is more than the {_threads_max()} this container has, which usually makes it slower.")
            if vals["parallel"] > MAX_SLOTS and vals["parallel"] != cur["parallel"]:
                warn.append(f"{NICE[m]}: {vals['parallel']} slots is more than the usual {MAX_SLOTS}; each slot gets a smaller share of the context.")
            if vals["context"] < HARD_MIN_CTX:
                raise ValueError(f"{NICE[m]}: context must be at least {HARD_MIN_CTX:,}")
            cap = limits(m, vals["model_file"], vals["parallel"], strict=False)
            if vals["context"] > cap:
                warn.append(f"{NICE[m]}: context {vals['context']:,} is more than the {max(cap, 0):,} estimated to fit in memory. "
                            "If the server cannot start, the change is rolled back.")
            elif vals["context"] < MIN_CTX:
                warn.append(f"{NICE[m]}: context {vals['context']:,} is below the usual {MIN_CTX:,}; most calls will not fit.")
            jobs[m] = vals
        elif m in pend:
            jobs[m] = pend[m]["values"]
        mod = (d.get("models") or {}).get(m)
        ctx = jobs[m]["context"] if m in jobs else None
        if mod:
            new, w = ui_settings.apply(m, mod, ctx=ctx, dry=True)
            prop[m], warn = new[m], warn + [f"{NICE[m]}: {x}" for x in w]
        if m in jobs:
            prop[m].update(jobs[m])
    # Calls that would no longer fit: only the ones this change brings, so an old warning does not come back every time.
    before = set(tuning.token_warnings())
    warn += [x for x in tuning.token_warnings(s=prop) if x not in before]
    return jobs, warn


def register(app, authed):
    router = APIRouter()
    applier.start()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/server/info")
    def info(request: Request):
        need(request)
        s, pend = settings.load(), applier.pending()
        return {"models": _models(), "max_threads": _threads_max(), "last": applier.last(), "flagspec": serverflags.spec(),
                "servers": {m: {"file": s[m]["model_file"], "context": s[m]["context"],
                                "threads": s[m]["threads"], "parallel": s[m]["parallel"], "flags": s[m].get("flags") or {}, "remote": remote.public(m),
                                "health": applier.health(m), "pending": pend.get(m)} for m in ("qwen", "gpt", "cur")}}

    @router.get("/api/server/limits")
    def lim(request: Request, model: str, file: str, parallel: int = 4):
        need(request)
        if model not in ("qwen", "gpt", "cur"):
            raise HTTPException(status_code=400, detail="unknown server")
        try:
            return {"max": limits(model, file, max(1, min(parallel, MAX_SLOTS))), "min": MIN_CTX}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/api/server/apply")
    async def apply(request: Request):
        need(request)
        d = await request.json()
        try:
            model = d.get("model")
            if model not in ("qwen", "gpt"):
                raise ValueError("unknown server")
            file = str(d.get("file", ""))
            ctx, thr, par = int(d.get("context")), int(d.get("threads")), int(d.get("parallel"))
            if not 1 <= thr <= _threads_max():
                raise ValueError(f"threads must be 1 to {_threads_max()}")
            if not 1 <= par <= MAX_SLOTS:
                raise ValueError(f"slots must be 1 to {MAX_SLOTS}")
            cap = limits(model, file, par)
            if not MIN_CTX <= ctx <= cap:
                raise ValueError(f"context must be {MIN_CTX:,} to {cap:,} for this model")
            cur = settings.load()[model]
            for k, v in cur["max_tokens"].items():
                if v > ctx:
                    raise ValueError(f"the {k} token limit ({v}) is larger than that context; lower it first")
            vals = {"model_file": file, "context": ctx, "threads": thr, "parallel": par}
            if all(cur[k] == v for k, v in vals.items()):
                raise ValueError("nothing changed")
            if d.get("when") == "idle":
                applier.queue(model, vals)
            elif d.get("when") == "now":
                applier.apply_now(model, vals)
            else:
                raise ValueError("choose now or idle")
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"restart failed and was rolled back: {type(e).__name__}")
        return {"ok": True}

    @router.post("/api/server/cancel")
    async def cancel(request: Request):
        need(request)
        m = (await request.json()).get("model")
        if m not in ("qwen", "gpt", "cur"):
            raise HTTPException(status_code=400, detail="unknown server")
        applier.cancel(m)
        return {"ok": True}

    @router.post("/api/settings/check")
    async def check_all(request: Request):
        need(request)
        try:
            jobs, warn = _plan(await request.json())
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "restart": [NICE[m] for m in jobs], "warn": warn}

    @router.post("/api/settings/apply_all")
    async def apply_all(request: Request):
        need(request)
        d = await request.json()
        try:
            import ui_settings
            jobs, _ = _plan(d)
            when = d.get("when")
            for _m, _f in (d.get("servers") or {}).items():
                if _f and _f.get("remote") is not None:
                    remote.save(_m, _f["remote"])
            if jobs and when not in ("now", "idle"):
                raise ValueError("choose now or idle")
            for m, mod in (d.get("models") or {}).items():
                ui_settings.apply(m, mod, ctx=jobs[m]["context"] if m in jobs else None)
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=str(e))

        def run():
            for m, vals in jobs.items():
                try:
                    applier.apply_now(m, vals)
                except Exception:
                    pass

        if jobs and when == "now":
            for m in jobs:
                applier.cancel(m)
            threading.Thread(target=run, daemon=True).start()
        else:
            for m, vals in jobs.items():
                applier.queue(m, vals)
        return {"ok": True}

    app.include_router(router)


HTML = r"""
<div id="sv"></div>
<script>
(function(){
const NM={qwen:"Sweeper server (port 8080)",gpt:"Reviewer server (port 8081)",cur:"Curator server (port 8082)"};
const E={};
let T=null,FS=[];
const lastEl=h("div",{class:"card mut"},"");
async function post(path,body){const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});const j=await r.json().catch(()=>({}));return{ok:r.ok,detail:j.detail||"",data:j}}
function num(v){const e=h("input",{type:"number"});e.value=v;return e}
function fld(l,el){return h("div",{class:"fld"},h("label",{},l),el)}
function fmt(v){return v.model_file+", context "+Number(v.context).toLocaleString()+", "+v.threads+" threads, "+v.parallel+" slots"}
function ask3(text){return new Promise(res=>{
 const ov=h("div",{style:"position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:60;display:flex;align-items:center;justify-content:center;padding:16px"});
 const done=v=>{ov.remove();res(v)};
 ov.append(h("div",{class:"card",style:"max-width:420px;width:100%"},h("h3",{},"Restart needed"),h("div",{class:"mut",style:"margin-bottom:12px"},text),
  h("button",{class:"pri",onclick:()=>done("now")},"Apply now"),h("button",{onclick:()=>done("idle")},"Apply when idle"),h("button",{onclick:()=>done(null)},"Cancel")));
 document.body.append(ov)})}
async function limit(m){const e=E[m];
 try{const d=await api("/api/server/limits?model="+m+"&file="+encodeURIComponent(e.file.value)+"&parallel="+(e.par.value||4));
  e.lim.textContent="Max safe context for this model: "+d.max.toLocaleString()+" (min "+d.min.toLocaleString()+")"}
 catch(err){e.lim.textContent=err.message}}
function mk(m,s,models,maxT){
 const e=E[m]={};
 e.file=h("select",{},...models.map(n=>h("option",{value:n},n)));e.file.value=s.file;
 e.ctx=num(s.context);e.thr=num(s.threads);e.par=num(s.parallel);
 e.fx={};
 const frows=(FS||[]).map(function(f){
  let inp;
  if(f.kind==="bool"){inp=h("input",{type:"checkbox"});inp.checked=!!(s.flags&&s.flags[f.key])}
  else if(f.kind==="choice"||f.kind==="model"){inp=h("select",{},h("option",{value:""},"default"),...(f.kind==="model"?models:f.choices).map(function(o){return h("option",{value:o},o)}));inp.value=(s.flags&&s.flags[f.key])||""}
  else{inp=h("input",{type:"number",step:f.step||"1",placeholder:"default"});inp.value=(s.flags&&s.flags[f.key]!=null)?s.flags[f.key]:""}
  inp.disabled=!f.ok;e.fx[f.key]=inp;
  return fld(f.label+(f.ok?"":" (not in this llama.cpp build)"),inp)});
 e.adv=h("details",{},h("summary",{class:"mut"},"Advanced server flags (blank uses the default)"),...frows);
 e.lim=h("div",{class:"mut"});
 const rs=s.remote||{};
 e.where=h("select",{},h("option",{value:"local"},"Local model on this server"),h("option",{value:"remote"},"External server"));e.where.value=rs.enabled?"remote":"local";
 e.ru=h("input",{type:"text",placeholder:"http://192.168.1.50:8080"});e.ru.value=rs.url||"";
 e.rt=h("select",{},h("option",{value:"llamacpp"},"llama.cpp server"),h("option",{value:"openai"},"Other OpenAI-compatible"));e.rt.value=rs.api||"llamacpp";
 e.rn=h("input",{type:"text",placeholder:"needed for non-llama.cpp servers"});e.rn.value=rs.model||"";
 e.rk=h("input",{type:"password",placeholder:rs.key_set?"saved, type to replace":"optional",autocomplete:"off"});
 e.rc=h("input",{type:"checkbox"});
 e.ra=h("input",{type:"checkbox"});e.ra.checked=!!rs.ack;
 e.rm=h("div",{class:"mut"});
 e.rtest=h("button",{onclick:async function(){
  e.rm.textContent="Testing...";
  const r=await fetch("/api/remote/test",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model_key:m,url:e.ru.value,api:e.rt.value,key:e.rk.value})});
  const j=await r.json().catch(function(){return{}});
  e.rm.textContent=r.ok?("Server says: "+j.state+(j.models&&j.models.length?". Models it lists: "+j.models.join(", "):"")):(j.detail||"Could not test")}},"Test connection");
 e.rbox=h("div",{},fld("Server address",e.ru),fld("Server type",e.rt),fld("Model name",e.rn),fld("API key",e.rk),fld("Clear the saved key",e.rc),fld("Server is outside my network: send my notes to it",e.ra),e.rtest,e.rm);
 function sync(){const r=e.where.value==="remote";[e.file,e.ctx,e.thr,e.par].forEach(function(x){x.disabled=r});FS.forEach(function(f){if(e.fx[f.key])e.fx[f.key].disabled=r||!f.ok});e.rbox.style.display=r?"":"none"}
 e.where.onchange=sync;sync();e.st=h("div",{class:"mut"});e.msg=h("div",{class:"mut"});
 e.file.onchange=()=>limit(m);e.par.onchange=()=>limit(m);
 const go=async()=>{e.msg.textContent="";
  const when=await ask3("Applying this restarts the "+NM[m]+". A call it is running right now would be cut off.");
  if(!when)return;
  const r=await post("/api/server/apply",{model:m,file:e.file.value,context:e.ctx.value,threads:e.thr.value,parallel:e.par.value,when:when});
  e.msg.textContent=r.ok?(when==="idle"?"Queued. It applies when no call is running.":"Restarting..."):r.detail;stat()};
 e.cancel=h("button",{style:"display:none",onclick:async()=>{await post("/api/server/cancel",{model:m});stat()}},"Cancel queued change");
 limit(m);
 (window.COLS=window.COLS||{})["s_"+m]=()=>({model:m,file:e.file.value,context:e.ctx.value,threads:e.thr.value,parallel:e.par.value,remote:{enabled:e.where.value==="remote",url:e.ru.value,api:e.rt.value,model:e.rn.value,key:e.rk.value,clear_key:e.rc.checked,ack:e.ra.checked},flags:(function(){const o={};for(const k in e.fx){const x=e.fx[k];o[k]=x.type==="checkbox"?(x.checked?true:""):x.value}return o})()});
 return h("div",{class:"card"},h("h3",{},NM[m]),e.st,fld("Where it runs",e.where),e.rbox,fld("Model file",e.file),fld("Context (tokens)",e.ctx),e.lim,
  fld("Threads (1 to "+maxT+")",e.thr),fld("Parallel slots",e.par),e.adv,h("button",{class:"pri",onclick:()=>window.applyAll()},"Apply all"),e.cancel,e.msg)}
async function stat(){clearTimeout(T);const d=await api("/api/server/info");let busy=false;
 for(const m of ["qwen","gpt","cur"]){const s=d.servers[m],e=E[m];if(!e)continue;
  e.st.textContent="Server: "+(m==="cur"&&s.health==="down"?"unloaded (loads when the curator runs)":s.health)+(s.pending?" · change queued, waiting for idle":"");
  if(s.remote&&s.remote.enabled)e.st.textContent="External server "+s.remote.url+": "+s.health;
  if(s.remote&&s.remote.back)e.st.textContent+=" · switching back to the local model; calls stay on the external server until it has loaded";
  e.cancel.style.display=s.pending?"":"none";
  if((s.health!=="ready"&&!(m==="cur"&&s.health==="down"))||s.pending)busy=true}
 const l=d.last;
 lastEl.textContent=!l?"No server change made yet.":l.error?"Last change ("+NM[l.model]+") failed and was rolled back: "+l.error:"Last change, "+NM[l.model]+": "+fmt(l.before)+"  →  "+fmt(l.after);
 if(busy)T=setTimeout(stat,2000)}
window.applyAll=async()=>{
 const C=window.COLS||{},body={models:{},servers:{}};
 for(const m of ["qwen","gpt","cur"]){if(C["m_"+m])body.models[m]=C["m_"+m]();if(C["s_"+m])body.servers[m]=C["s_"+m]()}
 const note=t=>{for(const m of ["qwen","gpt","cur"])if(E[m])E[m].msg.textContent=t};
 note("");
 const c=await post("/api/settings/check",body);
 if(!c.ok){note(c.detail);return}
 if(c.data.warn&&c.data.warn.length&&!await window.warnPop("These settings may not be safe",c.data.warn,"Apply anyway"))return;
 const need=c.data.restart;let when="now";
 if(need.length){
  when=await ask3("This restarts the "+need.join(" and ")+" server. Anything running there is cut off. Every other unsaved or queued change on this page is applied too.");
  if(!when)return}
 const r=await post("/api/settings/apply_all",{models:body.models,servers:body.servers,when:when});
 note(!r.ok?r.detail:!need.length?"Saved.":when==="idle"?"Everything saved. Server changes apply when idle.":"Everything saved. Restarting...");
 stat()};
(async()=>{const d=await api("/api/server/info");
 FS=d.flagspec||[];$("#sv").replaceChildren(...["qwen","gpt","cur"].map(m=>mk(m,d.servers[m],d.models,d.max_threads)),lastEl);stat()})();
})();
</script>
"""
