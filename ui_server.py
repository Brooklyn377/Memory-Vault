"""Settings page, server part: model file, context, threads and slots for each llama-server."""
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

import agent
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


NICE = {"qwen": "Sweeper", "gpt": "Reviewer", "cur": "Curator", "pa": "Assistant"}
MODELS = tuple(NICE)


def _running_ctx(m):
    """The context the server is really running with (from its /slots), or None if it is not answering."""
    try:
        import ui_think
        s = ui_think.slots(m, whole_server=True)
        return int(s["ctx"]) if s.get("up") and s.get("ctx") and "slots" in s else None
    except Exception:
        return None


def _runner_status(m, r):
    """The model runner's status for the remote settings on the page (r), or None without a runner or reply."""
    r = remote.with_server(r)   # a chosen server's address and runner token
    url = str((r or {}).get("runner") or "").strip()
    if not url:
        return None
    try:
        key = str(r.get("runner_key") or "").strip() or ("" if r.get("runner_clear") else remote.runner_key(m))
        return remote.runner(m, "GET", "/status", url=remote.norm_url(url), key=key, timeout=10)
    except ValueError:
        return None


def _differs(running, vals, sup):
    """True if the runner's llama-server does not run with these values (model, context, slots, flags). Compared with
    what the runner reports, so a value saved here earlier, before the runner was added, still gets applied there."""
    if not running:
        return True

    def n(x):
        try:
            return int(float(x))
        except (TypeError, ValueError):
            return None
    return (str(running.get("model_file")) != vals["model_file"] or n(running.get("context")) != n(vals["context"])
            or n(running.get("parallel")) != n(vals["parallel"])
            or (running.get("flags") or {}) != serverflags.runner_flags(vals, sup))


def _runner_info(m):
    """For the page: the saved runner's model files, options, settings and health, or its error, or None."""
    if not remote.runner_url(m):
        return None
    try:
        return remote.runner(m, "GET", "/status", timeout=4)
    except ValueError as e:
        return {"error": str(e)}


def _apply_ext(m, vals, before):
    """Have the model runner restart that machine's llama-server with these settings. On failure the settings here go
    back, and an Ask card says what llama-server said."""
    import thinklog
    now = datetime.now().isoformat(timespec="seconds")
    try:
        sup = (remote.runner(m, "GET", "/status", timeout=15).get("flags")) or []
        thinklog.abort(m)   # calls running there now are cut off; the restart would end them anyway
        r = remote.runner(m, "POST", "/apply", {"model_file": vals["model_file"], "context": vals["context"],
                                                 "threads": None,   # its own default: threads are not set for an external server
                                                 "parallel": vals["parallel"],
                                                 "flags": serverflags.runner_flags(vals, sup)}, timeout=1000)
        if not r.get("ok"):
            raise ValueError(r.get("error", "the runner could not apply it") + ". llama-server said: "
                             + " | ".join(l[-200:] for l in (r.get("log") or [])[-3:]))
        applier._atomic(applier.LAST, json.dumps({"model": m, "at": now, "before": {k: before.get(k) for k in applier.KEYS},
                                                  "after": {k: vals.get(k) for k in applier.KEYS}}))
        agent.log(f"settings: {NICE[m]} on its external server restarted with {vals['model_file']}, context {vals['context']}")
        import power
        power._flag(m, "off").unlink(missing_ok=True)   # the server is running now: it is no longer "stopped by you"
    except Exception as e:
        s = settings.load()
        s[m].update({k: before[k] for k in ("model_file", "context", "threads", "parallel", "flags") if k in before})
        settings.save(s)
        applier._atomic(applier.LAST, json.dumps({"model": m, "at": now, "error": str(e)[:200]}))
        import questions
        questions.ask(f"The {NICE[m]}'s external server did not take the new settings (context {vals['context']:,}, "
                      f"{vals['model_file']}), so its previous settings were kept.", why=str(e)[-450:],
                      kind="serverfail", options=[{"label": "OK", "value": "ok"}], payload={"model": m, "at": now})


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
    For a model on an external server with a model runner, every server setting is applied on that machine through
    the runner (ext_jobs). Without a runner, only its context is saved, for the token checks.
    Returns (jobs, warnings, direct, ext_jobs)."""
    import tuning
    import ui_settings
    s, pend, jobs, warn, direct, ext_jobs = settings.load(), applier.pending(), {}, [], {}, {}
    prop = settings.load()   # the model settings as they would be after this change
    for m in MODELS:
        cur, f = s[m], (d.get("servers") or {}).get(m)
        cure = dict(cur, device=serverflags.device(cur))   # what it runs on now, for the comparisons below
        vals, ext = None, False
        if f and f.get("remote") is not None:
            remote.check(m, f["remote"], warn)
            ext = bool(f["remote"].get("enabled"))
        st = _runner_status(m, f["remote"]) if f and ext else None
        if f and ext and st:   # an external server with a runner: its settings are applied on that machine
            names = [x["name"] for x in st.get("models") or []]
            # The model is "Model on that server" (the one model setting for an external server): the runner takes
            # the file name, without a folder such as /models/. Blank keeps what that machine runs now.
            mfile = str(f["remote"].get("model") or "").replace("\\", "/").rsplit("/", 1)[-1] or (st.get("settings") or {}).get("model_file", "")
            vals = {"model_file": mfile, "context": _num(f, "context", "Context", m),
                    "threads": cur["threads"],   # not shown for an external server: llama-server there picks its own
                    "parallel": _num(f, "parallel", "Parallel slots", m),
                    "flags": serverflags.check(f.get("flags") or {}, mfile, cur.get("flags"), warn,
                                               sup=st.get("flags") or [], models=names)}
            if (vals["threads"] < 1 and vals["threads"] != cur["threads"]) or (vals["parallel"] < 1 and vals["parallel"] != cur["parallel"]):
                warn.append(f"{NICE[m]}: threads and slots below 1 cannot work; that server will not start (it is rolled back).")
            if vals["context"] < HARD_MIN_CTX and vals["context"] != cur["context"]:
                warn.append(f"{NICE[m]}: context {vals['context']:,} is below {HARD_MIN_CTX:,}; almost no call will fit.")
            if vals["model_file"] not in names:
                warn.append(f"{NICE[m]}: \"{mfile or '(none)'}\" is not a model file on that machine; choose one under Model on "
                            "that server. Until then its settings are saved here "
                            "but not applied there.")
                direct[m] = vals
            elif _differs(st.get("settings"), vals, st.get("flags") or []):   # against what that machine runs, not our copy
                ext_jobs[m] = vals
            prop[m].update(vals)
            vals = None
        elif f and ext:
            ctx_ext = _num(f, "context", "Context", m)
            if ctx_ext < HARD_MIN_CTX and ctx_ext != cur["context"]:
                warn.append(f"{NICE[m]}: context {ctx_ext:,} is below {HARD_MIN_CTX:,}; almost no call will fit.")
            direct[m] = {"context": ctx_ext}
            prop[m].update(direct[m])
        elif f:
            vals = {"model_file": str(f.get("file", "")), "context": _num(f, "context", "Context", m),
                    "threads": _num(f, "threads", "Threads", m), "parallel": _num(f, "parallel", "Parallel slots", m),
                    "flags": serverflags.check(f.get("flags") or {}, str(f.get("file", "")), cur.get("flags"), warn),
                    "device": serverflags.device(dict(cur, device=f["device"])) if f.get("device") in ("cpu", "gpu") else cure["device"]}
        if vals and any(cure.get(k, {} if k == "flags" else None) != v for k, v in vals.items()):
            if (vals["threads"] < 1 and vals["threads"] != cur["threads"]) or (vals["parallel"] < 1 and vals["parallel"] != cur["parallel"]):
                warn.append(f"{NICE[m]}: threads and slots below 1 cannot work; the server will not start (it is rolled back).")
            if vals["threads"] > _threads_max() and vals["threads"] != cur["threads"]:
                warn.append(f"{NICE[m]}: {vals['threads']} threads is more than the {_threads_max()} this container has, which usually makes it slower.")
            if vals["parallel"] > MAX_SLOTS and vals["parallel"] != cur["parallel"]:
                warn.append(f"{NICE[m]}: {vals['parallel']} slots is more than the usual {MAX_SLOTS}; each slot gets a smaller share of the context.")
            if vals["context"] < HARD_MIN_CTX and vals["context"] != cur["context"]:
                warn.append(f"{NICE[m]}: context {vals['context']:,} is below {HARD_MIN_CTX:,}; almost no call will fit.")
            if not NAME.fullmatch(vals["model_file"]) or not (gguf_info.MODELS / vals["model_file"]).is_file():
                # no such model file: save the values, but there is nothing to restart with
                warn.append(f"{NICE[m]}: pick a model from the list. The values are saved, but its server is not restarted until a model file is chosen.")
                direct[m], vals = vals, None
                prop[m].update(direct[m])
        if vals and any(cure.get(k, {} if k == "flags" else None) != v for k, v in vals.items()):
            if HARD_MIN_CTX <= vals["context"] < MIN_CTX:
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
    # Two models on one runner share its llama-server: each Apply all would restart it with that card's settings.
    by_runner = {}
    for m in MODELS:
        r = remote.with_server(((d.get("servers") or {}).get(m) or {}).get("remote") or {})
        if r.get("enabled") and str(r.get("runner") or "").strip():
            by_runner.setdefault(str(r["runner"]).strip().rstrip("/"), []).append(m)
    for url, ms in by_runner.items():
        vs = [ext_jobs.get(m) or direct.get(m) or {} for m in ms]
        if len(ms) > 1 and any(v and v != vs[0] for v in vs):
            warn.append(f"{' and '.join(NICE[m] for m in ms)} use the same runner ({url}) with different settings; "
                        "they share one llama-server there, so each Apply all would restart it with the other's settings. "
                        "Give them the same model, context and slots, or separate runners.")
    # Calls that would no longer fit: only the ones this change brings, so an old warning does not come back every time.
    before = set(tuning.token_warnings())
    warn += [x for x in tuning.token_warnings(s=prop) if x not in before]
    return jobs, warn, direct, ext_jobs


def register(app, authed):
    router = APIRouter()
    applier.start()
    import turns
    turns.start()   # the turn manager: model groups

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/server/info")
    def info(request: Request):
        """The saved settings of every model: fast, nothing is asked of a server, so the page can draw at once."""
        need(request)
        s, pend = settings.load(), applier.pending()
        return {"models": _models(), "max_threads": _threads_max(), "last": applier.last(), "flagspec": serverflags.spec(),
                "servers_list": remote.servers(), "gpu": serverflags.backend() != "cpu",
                "servers": {m: {"file": s[m]["model_file"], "context": s[m]["context"],
                                "threads": s[m]["threads"], "parallel": s[m]["parallel"], "flags": s[m].get("flags") or {},
                                "remote": remote.public(m), "device": serverflags.device(s[m]), "pending": pend.get(m)} for m in MODELS}}

    @router.get("/api/server/status")
    def status(request: Request):
        """What the servers say now: whether each is up, the context it really runs, and its runner. These are asked of
        the servers, all at once, and can be slow when one does not answer; the page fills them in when they arrive."""
        need(request)
        with ThreadPoolExecutor(max_workers=3 * len(MODELS)) as pool:
            asked = {m: (pool.submit(applier.health, m), pool.submit(_running_ctx, m), pool.submit(_runner_info, m)) for m in MODELS}
            return {m: {"health": a[0].result(), "running": a[1].result(), "runner": a[2].result()} for m, a in asked.items()}

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
            jobs, warn, _, ext_jobs = _plan(await request.json())
            jobs = dict(jobs, **ext_jobs)   # both restart a server: the page asks before either
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "restart": [NICE[m] for m in jobs], "warn": warn}

    @router.post("/api/settings/apply_all")
    async def apply_all(request: Request):
        need(request)
        d = await request.json()
        try:
            import ui_settings
            jobs, _, direct, ext_jobs = _plan(d)
            when = d.get("when")
            if (jobs or ext_jobs) and when not in ("now", "idle"):
                raise ValueError("choose now or idle")
            for _m, _f in (d.get("servers") or {}).items():
                if _f and _f.get("remote") is not None:
                    r = _f["remote"]
                    where = "server " + str((remote.get_server(str(r.get("server") or "")) or {}).get("name") or r.get("server")) if r.get("enabled") else \
                        "this server's " + ("GPU" if _f.get("device") == "gpu" else "CPU")
                    agent.log(f"settings: Apply all: the {NICE[_m]} runs on {where}")
                    remote.save(_m, _f["remote"])
            before = {m: dict(settings.get(m)) for m in ext_jobs}
            if direct or ext_jobs:   # external servers: saved here now (the token checks use them); a runner applies them there
                s = settings.load()
                for m, vals in list(direct.items()) + list(ext_jobs.items()):
                    s[m].update(vals)
                settings.save(s)
            for m, mod in (d.get("models") or {}).items():
                ctx = (jobs.get(m) or ext_jobs.get(m) or direct.get(m) or {}).get("context")
                ui_settings.apply(m, mod, ctx=ctx)
            for m, vals in ext_jobs.items():   # a runner restart takes a while: in the background, like local restarts
                threading.Thread(target=_apply_ext, args=(m, vals, before[m]), daemon=True).start()
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
let T=null,FS=[],SL=[],HAS_GPU=false;   // SL: the servers on the Servers card
// After the Servers card changed: every card's Where it runs list, keeping each card's choice.
window.refreshServers=async function(){try{const d=await api("/api/server/info");SL=d.servers_list;HAS_GPU=d.gpu;
 for(const m of IDS){const e=E[m];if(e&&e.fillWhere){e.fillWhere(e.where.value);e.askNames();e.sync&&e.sync()}}}catch(err){}};
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
 const rs=s.remote||{};
 // Where it runs: this server's CPU or GPU, or one of the servers on the Servers card.
 e.where=h("select",{});
 e.fillWhere=function(v){e.where.replaceChildren(h("option",{value:"cpu"},"Local model on this server CPU"),
  ...(HAS_GPU?[h("option",{value:"gpu"},"Local model on this server GPU")]:[]),
  ...SL.map(function(p){return h("option",{value:"server:"+p.id},p.name)}));
  e.where.value=Array.prototype.some.call(e.where.options,function(o){return o.value===v})?v:"cpu"};
 e.fillWhere(rs.enabled&&rs.server?"server:"+rs.server:(s.device==="gpu"?"gpu":"cpu"));
 e.ext=function(){return e.where.value.indexOf("server:")===0};
 e.sid=function(){return e.ext()?e.where.value.slice(7):""};
 e.srv=function(){return SL.find(function(p){return p.id===e.sid()})};
 // The model's own name on that server (also tells this page which thinking switches apply); its list comes from the server.
 e.rnl=h("datalist",{id:"rn-"+m});
 e.rn=h("input",{type:"text",list:"rn-"+m,placeholder:"pick from the server's list"});e.rn.value=rs.model||"";e.rn0=e.rn.value;
 // The model is picked from a list when the machine tells us its models (its runner's model files, or the server's own
 // list); a box to type in is only for a server that lists none.
 e.rsel=h("select",{style:"display:none"});
 e.rsel.onchange=function(){e.rn.value=e.rsel.value;dirty=true};
 e.setNames=function(names){names=Array.from(new Set(names||[]));
  e.rnl.replaceChildren(...names.map(function(n){return h("option",{value:n})}));
  if(!names.length){e.rsel.style.display="none";e.rn.style.display="";return}
  if(!e.rn.value&&names.length===1){e.rn.value=names[0];dirty=true;e.rm.textContent+=" Model filled in from the server; press Apply all to save it."}
  const cur=e.rn.value,base=cur.replace(/\\/g,"/").split("/").pop(),hit=names.find(function(n){return n===cur||n.replace(/\\/g,"/").split("/").pop()===base});
  e.rsel.replaceChildren(...(cur&&!hit?[h("option",{value:cur},cur+" (not on that machine)")]:[]),...(cur?[]:[h("option",{value:""},"Choose a model")]),...names.map(function(n){return h("option",{value:n},n)}));
  e.rsel.value=hit||cur||"";e.rsel.style.display="";e.rn.style.display="none"};
 e.names=function(names){if(e.rst&&(e.rst.models||[]).length)return;e.setNames(names)};
 e.rm=h("div",{class:"mut"});
 e.rtest=h("button",{onclick:async function(){
  const p=e.srv();if(!p)return;e.rm.textContent="Testing...";
  const r=await fetch("/api/remote/test",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:p.id,url:p.url,api:p.api})});
  const j=await r.json().catch(function(){return{}});
  e.rm.textContent=r.ok?("Server says: "+j.state+(j.models&&j.models.length?". Models it lists: "+j.models.join(", "):"")):(j.detail||"Could not test");
  if(r.ok)e.names(j.models)}},"Test connection");
 e.askNames=function(){const p=e.srv();if(!p)return;
  fetch("/api/remote/test",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:p.id,url:p.url,api:p.api})})
   .then(function(r){return r.ok?r.json():null}).then(function(j){if(j)e.names(j.models)}).catch(function(){})};
 e.rrm=h("div",{class:"mut"});
 e.rbox=h("div",{},fld("Model on that server",h("span",{},e.rsel,e.rn)),e.rnl,e.rtest,e.rm,e.rrm);
 // Without a runner, an external server needs only Context here (the token checks use it); the model file, threads,
 // slots, flags and the memory estimate are this container's hardware and are hidden. With a runner they all apply there.
 const hw=[fld("Model file",e.file),fld("Threads (1 to "+maxT+")",e.thr),fld("Parallel slots",e.par),e.adv];
 e.loc=h("div",{},hw[0],fld("Context (tokens)",e.ctx),...hw.slice(1));
 e.extnote=h("div",{class:"mut",style:"margin:6px 0"},"");
 e.rst=null;   // the runner's last status: its model files and llama-server options
 // With a runner, "Model on that server" also offers the model files in that machine's models folder: it is the one
 // model setting for an external server, and Apply all has the runner load the one chosen there.
 e.useRunner=function(st){if(!st||st.error){e.rst=null;return}e.rst=st;
  e.setNames((st.models||[]).map(function(x){return x.name}));sync()};
 function sync(){const r=e.ext(),mg=r&&!!(e.srv()||{}).runner;
  FS.forEach(function(f){if(e.fx[f.key])e.fx[f.key].disabled=mg&&e.rst?(e.rst.flags||[]).indexOf(f.flag)<0:!f.ok});
  e.rbox.style.display=r?"":"none";hw.forEach(function(x){x.style.display=r&&!mg?"none":""});
  hw[0].style.display=r?"none":"";   // Model file lists this container's models: only for a local model
  hw[1].style.display=r?"none":"";   // CPU threads: this container's; an external server (often a GPU) picks its own
  e.extnote.style.display=r?"":"none";
  e.extnote.textContent=mg?"Managed through the runner on that machine: Apply all restarts its llama-server with the model, context, slots and flags below, and puts the old ones back if it does not come up.":"On an external server without a runner, set Context to the context it runs with, so the token checks here are right. Its model, threads and hardware are set on that machine; add a runner to that server on the Servers card to set them from here."}
 e.sync=sync;e.where.onchange=function(){e.askNames();sync();dirty=true};e.useRunner(s.runner);e.askNames();sync();e.st=h("div",{class:"mut"});e.msg=h("div",{class:"mut"});
 e.cancel=h("button",{style:"display:none",onclick:async()=>{await post("/api/server/cancel",{model:m});stat()}},"Cancel queued change");
 (window.COLS=window.COLS||{})["s_"+m]=()=>({model:m,file:e.file.value,context:e.ctx.value,threads:e.thr.value,parallel:e.par.value,device:e.where.value==="gpu"?"gpu":"cpu",remote:{enabled:e.ext(),server:e.sid(),model:e.rn.value},flags:(function(){const o={};for(const k in e.fx){const x=e.fx[k];o[k]=x.type==="checkbox"?(x.checked?true:""):x.value}return o})()});
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
// Unsaved edits in a section with its own Save button (data-saves) are tracked per section, and cleared when that section
// saves; a control that saves by itself (data-instant) is not an unsaved change at all.
const ownEdits=new Set();
window.markSaved=function(el){ownEdits.delete(el)};
["input","change"].forEach(function(t){document.addEventListener(t,function(ev){const el=ev.target;
 if(!(el.closest&&el.closest("#mm,#sp,#sv"))||el.closest("[data-instant]"))return;
 const own=el.closest("[data-saves]");if(own)ownEdits.add(own);else dirty=true},true)});
addEventListener("beforeunload",function(ev){if(dirty||[...ownEdits].some(function(e){return e.isConnected})){ev.preventDefault();ev.returnValue=""}});
async function stat(){clearTimeout(T);
 const [d,st]=await Promise.all([api("/api/server/info"),api("/api/server/status")]);let busy=false;
 for(const m of IDS){const s=Object.assign({},d.servers[m],st[m]),e=E[m];if(!e)continue;
  e.st.textContent="Server: "+(m==="cur"&&s.health==="down"?"unloaded (loads when the curator runs)":m==="pa"&&s.health==="down"?"not loaded":s.health)+(s.pending?" · change queued, waiting for idle":"");
  if(s.remote&&s.remote.enabled)e.st.textContent="External server "+s.remote.url+": "+s.health;
  // llama.cpp rounds the context up to a multiple of 256, so only a real difference is shown
  if(s.running&&(s.running<s.context||s.running>=s.context+256))e.st.textContent+=" · running with "+s.running.toLocaleString()+" tokens of context, not the "+Number(s.context).toLocaleString()+" you set"+
   (s.remote&&s.remote.enabled&&!s.runner?" (set on that server)":" (llama.cpp lowered it, most likely to the model's own maximum or to fit memory; see Fit context under Advanced server flags)");
  if(s.runner){   // the model runner on that machine
   e.rrm.textContent=s.runner.error?"Runner: "+s.runner.error:"Runner: llama-server "+(s.runner.stopped?"stopped":s.runner.health)+" · "+(s.runner.models||[]).length+" model file(s) on that machine";
   if(!s.runner.error&&(!e.rst||JSON.stringify(e.rst.models)!==JSON.stringify(s.runner.models)))e.useRunner(s.runner)}
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
 if(r.ok){dirty=false;if(window.offerRetry)window.offerRetry()}
 else window.warnPop("Could not apply",[r.detail||"Could not apply"]);
 note(!r.ok?r.detail:!need.length?"Saved.":when==="idle"?"Everything saved. Server changes apply when idle.":"Everything saved. Restarting...");
 if(r.ok)for(const m of IDS){const e=E[m];if(e&&e.rn&&e.ext()&&e.rn.value!==e.rn0){e.rn0=e.rn.value;
  e.msg.textContent+=" Reload the page to update the thinking switches for "+(e.rn.value||"this model")+"."}}
 stat()};
(async()=>{const d=await api("/api/server/info");
 FS=d.flagspec||[];SL=d.servers_list||[];HAS_GPU=!!d.gpu;$("#sv").replaceChildren(...IDS.map(m=>mk(m,d.servers[m],d.models,d.max_threads)),lastEl);stat()})();
})();
</script>
"""
