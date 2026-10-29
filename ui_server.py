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
    # The servers that share memory with this one: not the Curator (it swaps them out), and the Assistant only
    # while its server runs, so turning it on is what changes the others' estimates.
    others = [(v["model_file"], v["context"], v["parallel"]) for k, v in settings.load().items()
              if k != model and "cur" not in (k, model) and v.get("model_file") and (k != "pa" or applier._active("pa"))]
    try:
        cap = gguf_info.max_context(file, others, parallel)
    except Exception:
        raise ValueError("cannot read that model file, so its memory use cannot be estimated")
    if strict and cap < MIN_CTX:
        raise ValueError("not enough memory for this model next to the other server")
    return cap


NICE = {"qwen": "Sweeper", "gpt": "Reviewer", "cur": "Curator", "pa": "Assistant"}
MODELS = tuple(NICE)


def _running_ctx(m):
    """The context the server is really running with (from its /slots), or None if it is not answering."""
    try:
        import ui_think
        s = ui_think.slots(m)
        return int(s["ctx"]) if s.get("up") and s.get("ctx") and "slots" in s else None
    except Exception:
        return None


def _num(f, key, label, m):
    """A number typed on the page; text that is not a number is the one thing refused, with a plain reason."""
    try:
        return int(float(f.get(key)))
    except (TypeError, ValueError):
        raise ValueError(f"{NICE[m]}: {label} must be a number, not \"{f.get(key)}\"")


def _plan(d):
    """Check every change on the page. Returns (the server restarts needed, warnings, values to save directly). Nothing
    is refused for being unsafe: a value outside its usual range or below what can work, a context larger than the
    memory estimate, or a model file that is not there is a warning, applied if you say so. A server that does not
    come up ready is rolled back by applier.apply_now; without a model file the values are saved with no restart.
    Only text that cannot be read as the setting (not a number, an odd address) raises.
    For a model on an external server only its context is saved, with no restart and no memory check: the token
    checks use it. Model file, threads, slots and flags are that machine's business and stay as they were here."""
    import tuning
    import ui_settings
    s, pend, jobs, warn, direct = settings.load(), applier.pending(), {}, [], {}
    prop = settings.load()   # the model settings as they would be after this change
    for m in MODELS:
        cur, f = s[m], (d.get("servers") or {}).get(m)
        vals, ext = None, False
        if f and f.get("remote") is not None:
            remote.check(m, f["remote"], warn)
            ext = bool(f["remote"].get("enabled"))
        if f and ext:
            ctx_ext = _num(f, "context", "Context", m)
            if ctx_ext < HARD_MIN_CTX and ctx_ext != cur["context"]:
                warn.append(f"{NICE[m]}: context {ctx_ext:,} is below {HARD_MIN_CTX:,}; almost no call will fit.")
            direct[m] = {"context": ctx_ext}
            prop[m].update(direct[m])
        elif f:
            vals = {"model_file": str(f.get("file", "")), "context": _num(f, "context", "Context", m),
                    "threads": _num(f, "threads", "Threads", m), "parallel": _num(f, "parallel", "Parallel slots", m),
                    "flags": serverflags.check(f.get("flags") or {}, str(f.get("file", "")), cur.get("flags"), warn)}
        if vals and any(cur.get(k, {} if k == "flags" else None) != v for k, v in vals.items()):
            if (vals["threads"] < 1 and vals["threads"] != cur["threads"]) or (vals["parallel"] < 1 and vals["parallel"] != cur["parallel"]):
                warn.append(f"{NICE[m]}: threads and slots below 1 cannot work; the server will not start (it is rolled back).")
            if vals["threads"] > _threads_max() and vals["threads"] != cur["threads"]:
                warn.append(f"{NICE[m]}: {vals['threads']} threads is more than the {_threads_max()} this container has, which usually makes it slower.")
            if vals["parallel"] > MAX_SLOTS and vals["parallel"] != cur["parallel"]:
                warn.append(f"{NICE[m]}: {vals['parallel']} slots is more than the usual {MAX_SLOTS}; each slot gets a smaller share of the context.")
            if vals["context"] < HARD_MIN_CTX and vals["context"] != cur["context"]:
                warn.append(f"{NICE[m]}: context {vals['context']:,} is below {HARD_MIN_CTX:,}; almost no call will fit.")
            try:
                cap = limits(m, vals["model_file"], max(1, vals["parallel"]), strict=False)
            except ValueError as e:   # no such model file: save the values, but there is nothing to restart with
                warn.append(f"{NICE[m]}: {e}. The values are saved, but its server is not restarted until a model file is chosen.")
                direct[m], vals = vals, None
                prop[m].update(direct[m])
                cap = None
        if vals and any(cur.get(k, {} if k == "flags" else None) != v for k, v in vals.items()):
            if vals["context"] > cap:
                warn.append(f"{NICE[m]}: context {vals['context']:,} is more than the {max(cap, 0):,} estimated to fit in memory. "
                            "If the server cannot start, the change is rolled back.")
            elif HARD_MIN_CTX <= vals["context"] < MIN_CTX:
                warn.append(f"{NICE[m]}: context {vals['context']:,} is below the usual {MIN_CTX:,}; most calls will not fit.")
            jobs[m] = vals
        elif m in pend:
            jobs[m] = pend[m]["values"]
        mod = (d.get("models") or {}).get(m)
        ctx = jobs[m]["context"] if m in jobs else direct[m]["context"] if m in direct else None
        if mod:
            new, w = ui_settings.apply(m, mod, ctx=ctx, dry=True)
            prop[m], warn = new[m], warn + [f"{NICE[m]}: {x}" for x in w]
        if m in jobs:
            prop[m].update(jobs[m])
    # Calls that would no longer fit: only the ones this change brings, so an old warning does not come back every time.
    before = set(tuning.token_warnings())
    warn += [x for x in tuning.token_warnings(s=prop) if x not in before]
    return jobs, warn, direct


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
                                "health": applier.health(m), "pending": pend.get(m), "running": _running_ctx(m)} for m in MODELS}}

    @router.get("/api/server/limits")
    def lim(request: Request, model: str, file: str, parallel: int = 4):
        need(request)
        if model not in MODELS:
            raise HTTPException(status_code=400, detail="unknown server")
        try:
            return {"max": limits(model, file, max(1, min(parallel, MAX_SLOTS))), "min": MIN_CTX}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/api/server/cancel")
    async def cancel(request: Request):
        need(request)
        m = (await request.json()).get("model")
        if m not in MODELS:
            raise HTTPException(status_code=400, detail="unknown server")
        applier.cancel(m)
        return {"ok": True}

    @router.post("/api/settings/check")
    async def check_all(request: Request):
        need(request)
        try:
            jobs, warn, _ = _plan(await request.json())
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "restart": [NICE[m] for m in jobs], "warn": warn}

    @router.post("/api/settings/apply_all")
    async def apply_all(request: Request):
        need(request)
        d = await request.json()
        try:
            import ui_settings
            jobs, _, direct = _plan(d)
            when = d.get("when")
            if jobs and when not in ("now", "idle"):
                raise ValueError("choose now or idle")
            for _m, _f in (d.get("servers") or {}).items():
                if _f and _f.get("remote") is not None:
                    remote.save(_m, _f["remote"])
            if direct:   # models on an external server: only their context, for the token checks
                s = settings.load()
                for m, vals in direct.items():
                    s[m].update(vals)
                settings.save(s)
            for m, mod in (d.get("models") or {}).items():
                ui_settings.apply(m, mod, ctx=jobs[m]["context"] if m in jobs else direct[m]["context"] if m in direct else None)
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
const NM={qwen:"Sweeper server (port 8080)",gpt:"Reviewer server (port 8081)",cur:"Curator server (port 8082)",pa:"Assistant server (port 8083)"};
const IDS=Object.keys(NM);
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
 if(!e.file.value){e.lim.textContent=m==="pa"?"Choose a model file to run the Assistant here, or use an external server.":"";return}
 try{const d=await api("/api/server/limits?model="+m+"&file="+encodeURIComponent(e.file.value)+"&parallel="+(e.par.value||4));
  e.lim.textContent="Max safe context for this model: "+d.max.toLocaleString()+" (min "+d.min.toLocaleString()+")"}
 catch(err){e.lim.textContent=err.message}}
function mk(m,s,models,maxT){
 const e=E[m]={};
 e.file=h("select",{},...(s.file?[]:[h("option",{value:""},"No model chosen")]),...models.map(n=>h("option",{value:n},n)));e.file.value=s.file;
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
 // The external server's own model list, offered as a dropdown; the name also tells this page which thinking switches apply.
 e.rnl=h("datalist",{id:"rn-"+m});
 e.rn=h("input",{type:"text",list:"rn-"+m,placeholder:"pick from the server's list"});e.rn.value=rs.model||"";e.rn0=e.rn.value;
 e.names=function(names){e.rnl.replaceChildren(...(names||[]).map(function(n){return h("option",{value:n})}));
  if(!e.rn.value&&names&&names.length===1){e.rn.value=names[0];dirty=true;e.rm.textContent+=" Model filled in from the server; press Apply all to save it."}};
 e.rk=h("input",{type:"password",placeholder:rs.key_set?"saved, type to replace":"optional",autocomplete:"off"});
 e.rc=h("input",{type:"checkbox"});
 e.ra=h("input",{type:"checkbox"});e.ra.checked=!!rs.ack;
 e.rm=h("div",{class:"mut"});
 e.rtest=h("button",{onclick:async function(){
  e.rm.textContent="Testing...";
  const r=await fetch("/api/remote/test",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model_key:m,url:e.ru.value,api:e.rt.value,key:e.rk.value})});
  const j=await r.json().catch(function(){return{}});
  e.rm.textContent=r.ok?("Server says: "+j.state+(j.models&&j.models.length?". Models it lists: "+j.models.join(", "):"")):(j.detail||"Could not test");
  if(r.ok)e.names(j.models)}},"Test connection");
 if(rs.enabled&&rs.url)fetch("/api/remote/test",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model_key:m,url:rs.url,api:rs.api||"llamacpp",key:""})})
  .then(function(r){return r.ok?r.json():null}).then(function(j){if(j)e.names(j.models)}).catch(function(){});
 e.rbox=h("div",{},fld("Server address",e.ru),fld("Server type",e.rt),fld("Model on that server",e.rn),e.rnl,fld("API key",e.rk),fld("Clear the saved key",e.rc),fld("Server is outside my network: send my notes to it",e.ra),e.rtest,e.rm);
 // On an external server only Context applies here (the token checks use it). The model file, threads, slots,
 // server flags and the memory estimate describe this container's hardware, so they are hidden and kept as they are.
 const hw=[fld("Model file",e.file),e.lim,fld("Threads (1 to "+maxT+")",e.thr),fld("Parallel slots",e.par),e.adv];
 e.loc=h("div",{},hw[0],fld("Context (tokens)",e.ctx),...hw.slice(1));
 e.extnote=h("div",{class:"mut",style:"margin:6px 0"},"On an external server, set Context to the context it runs with, so the token checks here are right. Its model, threads and hardware are set on that machine.");
 function sync(){const r=e.where.value==="remote";FS.forEach(function(f){if(e.fx[f.key])e.fx[f.key].disabled=!f.ok});e.rbox.style.display=r?"":"none";hw.forEach(function(x){x.style.display=r?"none":""});e.extnote.style.display=r?"":"none"}
 e.where.onchange=sync;sync();e.st=h("div",{class:"mut"});e.msg=h("div",{class:"mut"});
 e.file.onchange=()=>limit(m);e.par.onchange=()=>limit(m);
 e.cancel=h("button",{style:"display:none",onclick:async()=>{await post("/api/server/cancel",{model:m});stat()}},"Cancel queued change");
 limit(m);
 (window.COLS=window.COLS||{})["s_"+m]=()=>({model:m,file:e.file.value,context:e.ctx.value,threads:e.thr.value,parallel:e.par.value,remote:{enabled:e.where.value==="remote",url:e.ru.value,api:e.rt.value,model:e.rn.value,key:e.rk.value,clear_key:e.rc.checked,ack:e.ra.checked},flags:(function(){const o={};for(const k in e.fx){const x=e.fx[k];o[k]=x.type==="checkbox"?(x.checked?true:""):x.value}return o})()});
 return h("div",{class:"card"},h("h3",{},NM[m]),e.st,fld("Where it runs",e.where),e.rbox,e.extnote,e.loc,
  h("button",{class:"pri",onclick:()=>window.applyAll()},"Apply all"),e.cancel,e.msg)}
// The model list changed (a download finished or a file was deleted): refresh every Model file list, keeping choices.
window.refreshModelFiles=function(models){
 for(const m of IDS){const e=E[m];if(!e)continue;
  const fill=function(sel,first){const v=sel.value;
   sel.replaceChildren(...first,...models.map(function(n){return h("option",{value:n},n)}));
   if(v&&models.indexOf(v)<0)sel.append(h("option",{value:v},v+" (missing)"));sel.value=v};
  fill(e.file,e.file.value?[]:[h("option",{value:""},"No model chosen")]);
  FS.forEach(function(f){if(f.kind==="model"&&e.fx[f.key])fill(e.fx[f.key],[h("option",{value:""},"default")])})}};
// Changes typed on the page are kept only by Apply all: warn before leaving the page with some not applied.
let dirty=false;
["input","change"].forEach(function(t){document.addEventListener(t,function(ev){if(ev.target.closest&&ev.target.closest("#mm,#sp,#sv"))dirty=true},true)});
addEventListener("beforeunload",function(ev){if(dirty){ev.preventDefault();ev.returnValue=""}});
async function stat(){clearTimeout(T);const d=await api("/api/server/info");let busy=false;
 for(const m of IDS){const s=d.servers[m],e=E[m];if(!e)continue;
  e.st.textContent="Server: "+(m==="cur"&&s.health==="down"?"unloaded (loads when the curator runs)":m==="pa"&&s.health==="down"?"not loaded":s.health)+(s.pending?" · change queued, waiting for idle":"");
  if(s.remote&&s.remote.enabled)e.st.textContent="External server "+s.remote.url+": "+s.health;
  if(s.running&&(s.running<s.context||s.running>=s.context+256))e.st.textContent+=   // llama.cpp rounds up to a multiple of 256" · running with "+s.running.toLocaleString()+" tokens of context, not the "+Number(s.context).toLocaleString()+" you set"+
   (s.remote&&s.remote.enabled?" (set on that server)":" (llama.cpp lowered it, most likely to fit memory; see Fit context under Advanced server flags)");
  e.cancel.style.display=s.pending?"":"none";
  if((s.health!=="ready"&&!((m==="cur"||m==="pa")&&s.health==="down"))||s.pending)busy=true}
 const l=d.last;
 lastEl.textContent=!l?"No server change made yet.":l.error?"Last change ("+NM[l.model]+") failed and was rolled back: "+l.error:"Last change, "+NM[l.model]+": "+fmt(l.before)+"  →  "+fmt(l.after);
 if(busy)T=setTimeout(stat,2000)}
window.applyAll=async()=>{
 const C=window.COLS||{},body={models:{},servers:{}};
 for(const m of IDS){if(C["m_"+m])body.models[m]=C["m_"+m]();if(C["s_"+m])body.servers[m]=C["s_"+m]()}
 const note=t=>{for(const m of IDS)if(E[m])E[m].msg.textContent=t};
 note("");
 const c=await post("/api/settings/check",body);
 if(!c.ok){note(c.detail);window.warnPop("Could not apply",[c.detail||"Could not apply"]);return}
 if(c.data.warn&&c.data.warn.length&&!await window.warnPop("These settings may not be safe",c.data.warn,"Apply anyway"))return;
 const need=c.data.restart;let when="now";
 if(need.length){
  when=await ask3("This restarts the "+need.join(" and ")+" server. Anything running there is cut off. Every other unsaved or queued change on this page is applied too.");
  if(!when)return}
 const r=await post("/api/settings/apply_all",{models:body.models,servers:body.servers,when:when});
 if(r.ok)dirty=false;
 else window.warnPop("Could not apply",[r.detail||"Could not apply"]);
 note(!r.ok?r.detail:!need.length?"Saved.":when==="idle"?"Everything saved. Server changes apply when idle.":"Everything saved. Restarting...");
 if(r.ok)for(const m of IDS){const e=E[m];if(e&&e.rn&&e.where.value==="remote"&&e.rn.value!==e.rn0){e.rn0=e.rn.value;
  e.msg.textContent+=" Reload the page to update the thinking switches for "+(e.rn.value||"this model")+"."}}
 stat()};
(async()=>{const d=await api("/api/server/info");
 FS=d.flagspec||[];$("#sv").replaceChildren(...IDS.map(m=>mk(m,d.servers[m],d.models,d.max_threads)),lastEl);stat()})();
})();
</script>
"""
