"""Manual power for each model (for testing) and the one-card-per-model layout of the Settings page.
Unloading is immediate and overrides everything. A model you unload stays off: its calls wait until you load it again."""
import subprocess
import time

from fastapi import APIRouter, HTTPException, Request

import agent
import applier
import gguf_info
import settings

STATE = agent.STATE
UNIT = applier.UNIT
NICE = {"qwen": "Sweeper", "gpt": "Reviewer", "cur": "Curator", "pa": "Assistant"}
GIB = 1024 ** 3


class Unsafe(ValueError):
    """Loading would pass the memory safety limit. The page asks you to confirm."""


def _flag(m, kind):
    return STATE / f"model_{kind}_{m}"


def off(m):
    return _flag(m, "off").exists()


def manual(m):
    return _flag(m, "on").exists()


def consume_kill(m):
    """True once after you killed this model with the power button, so the caller does not treat it as a failure."""
    f = _flag(m, "killed")
    if f.exists():
        fresh = time.time() - f.stat().st_mtime < 120
        f.unlink(missing_ok=True)
        return fresh
    return False


def wait_on(m):
    """Calls to an unloaded or stopped model wait here until you load or start it again. Not the Assistant (chat answers
    with the Reviewer meanwhile), and the Curator only on an external server (locally it is loaded on demand)."""
    if m == "pa":
        return
    if m == "cur":
        import remote
        if not remote.enabled(m):
            return
    while off(m):
        time.sleep(5)


def mem_used():
    d = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            if v.strip():
                d[k] = int(v.split()[0]) * 1024
    return d.get("MemTotal", 0) - d.get("MemAvailable", 0)


def state(m):
    import remote
    import swap
    if remote.enabled(m):
        if off(m):
            return {"state": "stopped", "detail": f"External server {remote.base(m)}, stopped by you: its calls wait until you start it."
                    + (" Chat answers with the Reviewer meanwhile." if m == "pa" else "")}
        return {"state": "external", "detail": remote.base(m)}
    h = applier.health(m)
    if h == "ready":
        return {"state": "loaded", "detail": ""}
    if h == "loading" or (applier._active(m) and h == "down"):
        return {"state": "loading", "detail": ""}
    import turns
    if turns.managed(m) and h != "ready" and not (h == "loading" or applier._active(m)) and not off(m):
        if m in turns.wants():
            held = [turns.NICE[x] for x in turns.active()["group"] if x != m]
            return {"state": "waiting", "detail": "Waiting for its turn" + (f": {', '.join(held)} {'has' if len(held) == 1 else 'have'} it now." if held else ".")}
        return {"state": "unloaded", "detail": "Not loaded. It is loaded when something needs it, and the others wait their turn (Settings, Model groups)."}
    if m == "pa":
        return {"state": "unloaded", "detail": "Not loaded. To run it here: choose its model file under Server, press "
                "Apply all, then hold to load. To run it on another machine: set Where it runs to External server, "
                "fill in the address and press Apply all; no model file is needed."}
    if m != "cur" and off(m):
        return {"state": "unloaded", "detail": "Unloaded by you. Its calls wait until you load it."}
    if m != "cur" and swap.holding():
        return {"state": "unloaded", "detail": "Unloaded while the Curator works.", "swapped": True}
    return {"state": "unloaded", "detail": "Loads when the Curator runs." if m == "cur" else "Not answering."}


def load(m, force=False):
    import remote
    import swap
    if remote.enabled(m):   # start an external model you stopped: its calls go out again
        if remote.managed(m):   # and with a runner, its llama-server is started again on that machine
            remote.runner(m, "POST", "/start", {})
        _flag(m, "off").unlink(missing_ok=True)
        agent.log(f"settings: {NICE[m]} (external server) started by you")
        return
    if m == "pa" and not settings.get("pa")["model_file"]:
        raise ValueError("Choose the Assistant's model file under Server and press Apply all first.")
    import turns
    if turns.managed(m):   # it takes turns: the manager loads it, together with its group, as soon as it can
        _flag(m, "off").unlink(missing_ok=True)
        _flag(m, "on").write_text("1")   # kept loaded until you unload it
        turns.want(m, urgent=True)
        agent.log(f"settings: {NICE[m]} loading requested by you; it is loaded when the models that have the turn are idle")
        return
    if m in ("qwen", "gpt") and swap.holding():
        raise ValueError("The Curator holds the memory right now. Unload the Curator first; the Sweeper and Reviewer load back after that.")
    if applier._active(m):
        _flag(m, "off").unlink(missing_ok=True)
        return
    s = settings.load()
    others = [k for k in UNIT if k != m and applier._active(k)]
    need = sum(gguf_info._fp_safe(s[k]["model_file"], s[k]["context"], s[k]["parallel"]) for k in others + [m])
    total = gguf_info.total_memory()
    limit = total - gguf_info.HEADROOM
    if need > limit and not force:
        with_ = (" next to the " + " and ".join(NICE[k] for k in others)) if others else ""
        raise Unsafe(f"Loading the {NICE[m]}{with_} is estimated at about {need / GIB:.0f} GB, above the {limit / GIB:.0f} GB safety limit "
                     f"(this container has {total / GIB:.0f} GB and 4 GB is kept free). In use right now: {mem_used() / GIB:.0f} GB. "
                     "If memory runs out, the system may kill processes.")
    _flag(m, "off").unlink(missing_ok=True)
    if m == "cur":
        _flag(m, "on").write_text("1")
    applier.write_env(m, s[m])
    if m == "pa":   # off by default
        _flag(m, "off").unlink(missing_ok=True)
        subprocess.run(["systemctl", "enable", UNIT[m]], timeout=30)
    subprocess.run(["systemctl", "start", "--no-block", UNIT[m]], timeout=30)
    agent.log(f"settings: {NICE[m]} loaded by you" + (" (over the memory limit, confirmed)" if need > limit else ""))


def unload(m):
    """Immediate: stop the unit, then kill whatever is left. Returns a note for the page, or ''."""
    import remote
    import swap
    import thinklog
    if remote.enabled(m):   # stop an external model: cut off its running calls now and hold new ones
        _flag(m, "off").write_text("1")
        thinklog.abort(m)
        note = ""
        if remote.managed(m):   # with a runner, its llama-server is stopped on that machine too, freeing its memory
            try:
                remote.runner(m, "POST", "/stop", {})
                note = " Its llama-server on that machine was stopped too."
            except ValueError as e:
                note = f" Its llama-server could not be stopped there: {e}."
        agent.log(f"settings: {NICE[m]} (external server) stopped by you; its running calls were cut off")
        return "Stopped. Its running calls were cut off; new calls wait until you start it." + note
    thinklog.abort(m)   # local: the server is stopped below; this also ends the waiting callers' connections at once
    note = ""
    _flag(m, "on").unlink(missing_ok=True)
    if m == "cur":
        was = manual(m)
        _flag(m, "on").unlink(missing_ok=True)
        if was and not swap.holding():
            _flag(m, "killed").write_text("1")
        if swap.holding():
            _flag(m, "killed").write_text("1")
            try:
                import curator
                c = curator.load_cfg()
                c["enabled"] = False
                curator.save_cfg(c)
            except Exception:
                pass
            note = "Curator stopped and its switch turned off. The Sweeper and Reviewer load back now."
    elif m == "pa":
        _flag(m, "off").write_text("1")   # chat answers with the Reviewer; no turn is taken for it
        subprocess.run(["systemctl", "disable", UNIT[m]], timeout=30)
    else:
        _flag(m, "off").write_text("1")
    subprocess.run(["systemctl", "stop", "--no-block", UNIT[m]], timeout=30)
    subprocess.run(["systemctl", "kill", "--signal=SIGKILL", UNIT[m]], timeout=30)
    agent.log(f"settings: {NICE[m]} unloaded by you")
    return note


def register(app, authed):
    router = APIRouter()

    @router.get("/api/model/power")
    def get(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        return {m: state(m) for m in UNIT}

    @router.post("/api/model/power")
    async def post(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        d = await request.json()
        m, a = d.get("model"), d.get("action")
        if m not in UNIT or a not in ("load", "unload"):
            raise HTTPException(status_code=400, detail="unknown model or action")
        try:
            if a == "load":
                load(m, bool(d.get("force")))
                return {"ok": True}
            return {"ok": True, "note": unload(m)}
        except Unsafe as e:
            raise HTTPException(status_code=409, detail="unsafe:" + str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)[:100]}")

    app.include_router(router)


HTML = r"""
<style>
.mh{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.mh i{width:10px;height:10px;border-radius:5px;display:inline-block}
.mh b{font-size:18px}
.mc{font-size:12px;font-weight:600;padding:2px 10px;border-radius:10px;background:var(--line);color:var(--mut)}
.mc.on{background:var(--acc);color:#fff}
.pw{position:relative;overflow:hidden;margin-left:auto;user-select:none;-webkit-user-select:none;touch-action:none}
details.sec{border-top:1px solid var(--line);padding:6px 0;margin-top:8px}
details.sec>summary{cursor:pointer;font-weight:600;padding:6px 0}
</style>
<div id="mm"></div>
<script>
(function(){
const M=[["qwen","Sweeper","#4c9aff"],["gpt","Reviewer","#f5a623"],["cur","Curator","#34d399"],["pa","Assistant","#e879f9"]];
const UI={};
let done=false;
function find(sel,pre){const cs=document.querySelectorAll(sel);for(let i=0;i<cs.length;i++){const t=cs[i].querySelector("h3");if(t&&t.textContent.indexOf(pre)===0)return cs[i]}return null}
function parts(card){const out=[];Array.from(card.children).forEach(function(ch,i){
 if(ch.tagName==="H3")return;
 if(ch.tagName==="BUTTON"&&/^Apply all/.test(ch.textContent))return;
 if(i<3&&ch.classList.contains("mut")&&ch.textContent.indexOf("· context")>=0)return;
 out.push(ch)});return out}
function sec(title,nodes,open){const d=h("details",{class:"sec"},h("summary",{},title));nodes.forEach(function(n){d.append(n)});d.open=!!open;return d}
async function post(body){const r=await fetch("/api/model/power",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});const j=await r.json().catch(function(){return{}});return{ok:r.ok,status:r.status,detail:j.detail||"",note:j.note||""}}
function unsafe(text){return new Promise(function(res){
 const ov=h("div",{style:"position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:60;display:flex;align-items:center;justify-content:center;padding:16px"});
 const done=function(v){ov.remove();res(v)};
 ov.append(h("div",{class:"card",style:"max-width:420px;width:100%"},h("h3",{},"Not enough memory to be safe"),h("div",{class:"mut",style:"margin-bottom:12px"},text),
  h("button",{style:"background:#b91c1c;color:#fff;margin-right:8px",onclick:function(){done(true)}},"Load anyway"),h("button",{class:"pri",onclick:function(){done(false)}},"Cancel")));
 document.body.append(ov)})}
function powerButton(key,u){
 const fill=h("div",{style:"position:absolute;left:0;top:0;bottom:0;width:0"});
 const ico=h("span",{style:"position:relative;display:inline-block;vertical-align:middle;margin-right:6px;line-height:0"});
 ico.innerHTML='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 3v9"/><path d="M6.3 6.8a8 8 0 1 0 11.4 0"/></svg>';
 const txt=h("span",{style:"position:relative;vertical-align:middle"},"");
 const btn=h("button",{class:"pw"},fill,ico,txt);
 let t=null;
 function stop(){clearTimeout(t);t=null;fill.style.transition="width .15s";fill.style.width="0"}
 btn.onpointerdown=function(e){
  if(btn.disabled||!u.act)return;
  e.preventDefault();u.msg.textContent="";
  fill.style.background=u.act==="load"?"#15803d":"#b91c1c";
  fill.style.transition="width 1.5s linear";fill.style.width="100%";
  t=setTimeout(async function(){
   stop();
   let r=await post({model:key,action:u.act});
   if(!r.ok&&r.status===409&&String(r.detail).indexOf("unsafe:")===0){
    const go=await unsafe(r.detail.slice(7));
    r=go?await post({model:key,action:u.act,force:true}):{ok:true,note:""}}
   u.msg.textContent=r.ok?(r.note||""):(r.detail||"Could not do that");
   refresh()},1500)};
 btn.onpointerup=stop;btn.onpointerleave=stop;btn.onpointercancel=stop;btn.oncontextmenu=function(e){e.preventDefault()};
 u.btn=btn;u.txt=txt;return btn}
async function refresh(){try{
 const d=await api("/api/model/power");
 const L={loaded:"Loaded",loading:"Loading",unloaded:"Unloaded",waiting:"Waiting",external:"External server",stopped:"Stopped"};
 M.forEach(function(a){const u=UI[a[0]],s=d[a[0]];if(!u||!s)return;
  u.chip.textContent=L[s.state]||s.state;u.chip.className="mc"+(s.state==="loaded"?" on":"");
  u.sub.textContent=s.detail||"";
  if(s.state==="external"){u.act="unload";u.btn.disabled=false;u.txt.textContent="Hold to stop"}
  else if(s.state==="stopped"){u.act="load";u.btn.disabled=false;u.txt.textContent="Hold to start"}
  else if(s.swapped){u.act="";u.btn.disabled=true;u.txt.textContent="Curator working"}
  else if(s.state==="unloaded"||s.state==="waiting"){u.act="load";u.btn.disabled=false;u.txt.textContent="Hold to load"}
  else{u.act="unload";u.btn.disabled=false;u.txt.textContent="Hold to unload"}})
}catch(e){}}
function ready(){
 for(let i=0;i<M.length;i++){const n=M[i][1];
  if(!find("#sp .card",n+": ")||!find("#sv .card",n+" server")||!find("#pp .card",n+" prompts"))return false}
 return !!find("#cu .card","Curator (swaps")}
function merge(){
 const mm=$("#mm"),sp=$("#sp"),cu=find("#cu .card","Curator (swaps");
 sp.parentNode.insertBefore(mm,sp);
 M.forEach(function(a){
  const key=a[0],name=a[1],col=a[2];
  const gen=find("#sp .card",name+": "),srv=find("#sv .card",name+" server"),pr=find("#pp .card",name+" prompts");
  const u=UI[key]={act:""};
  u.chip=h("span",{class:"mc"},"...");u.sub=h("div",{class:"mut"});u.msg=h("div",{class:"mut"});
  const card=h("div",{class:"card"},h("div",{class:"mh"},h("i",{style:"background:"+col}),h("b",{},name),u.chip,powerButton(key,u)),u.sub,u.msg);
  card.append(sec("Server and where it runs",parts(srv),true));
  card.append(sec("Generation settings",parts(gen),true));
  if(key==="cur"&&cu)card.append(sec("Curator behaviour",parts(cu),true));
  card.append(sec("Prompts",parts(pr),false));
  card.append(h("button",{class:"pri",style:"margin-top:10px",onclick:function(){window.applyAll()}},"Apply all models"));
  gen.remove();srv.remove();pr.remove();mm.append(card)});
 if(cu)cu.remove();
 refresh();setInterval(function(){if(!document.hidden)refresh()},3000)}
const mo=new MutationObserver(function(){if(!done&&ready()){done=true;mo.disconnect();merge()}});
mo.observe(document.body,{childList:true,subtree:true});
if(ready()){done=true;mo.disconnect();merge()}
})();
</script>
"""
