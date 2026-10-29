"""Settings page: token limits, temperature and per-model options. Reads and writes settings.json."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import settings
import ui_extra

EFFORT = ("low", "medium", "high")


def capname(m, v):
    try:
        import remote
        r = remote.raw(m)
        return r["model"] if remote.enabled(m) and r.get("model") else v["model_file"]
    except Exception:
        return v["model_file"]


def caps(model_file):
    n = model_file.lower()
    return {"thinking": "qwen3" in n, "reasoning_effort": "gpt-oss" in n, "thinking_budget": "seed-oss" in n}


# key: (hard lowest, hard highest, kind, usual lowest, usual highest). A hard limit (None: none) is a value that
# cannot work, such as a probability above 1; outside the usual range a value is saved with a warning.
RANGES = {"top_p": (0, 1, "float", 0, 1), "top_k": (0, None, "int", 0, 1000), "min_p": (0, 1, "float", 0, 1),
          "typical_p": (0, 1, "float", 0, 1), "repeat_penalty": (0, None, "float", 0.5, 2),
          "repeat_last_n": (-1, None, "int", -1, 65536), "presence_penalty": (None, None, "float", -2, 2),
          "frequency_penalty": (None, None, "float", -2, 2), "mirostat": (0, 2, "int", 0, 2),
          "mirostat_tau": (0, None, "float", 0, 20), "mirostat_eta": (0, None, "float", 0, 1),
          "seed": (-1, 2147483647, "int", -1, 2147483647), "dry_multiplier": (0, None, "float", 0, 5),
          "dry_base": (0, None, "float", 1, 4), "dry_allowed_length": (0, None, "int", 1, 100),
          "xtc_probability": (0, 1, "float", 0, 1), "xtc_threshold": (0, 1, "float", 0, 0.5)}


def bounded(name, x, old, hard_lo, hard_hi, lo, hi, warn):
    """Never refuses: a changed x outside what can work (the hard limits) or outside the usual range adds a warning."""
    if x == old:
        return
    if (hard_lo is not None and x < hard_lo) or (hard_hi is not None and x > hard_hi):
        warn.append(f"{name}: {x:g} is outside what it can be (" + (f"{hard_lo:g} to {hard_hi:g}" if hard_lo is not None and hard_hi is not None
                    else f"at least {hard_lo:g}" if hard_lo is not None else f"at most {hard_hi:g}") + "); the model server may refuse or clamp it.")
    elif not lo <= x <= hi:
        warn.append(f"{name}: {x:g} is outside the usual {lo:g} to {hi:g}.")


def check_sampling(d, old=None, warn=None):
    out, old, warn = {}, old or {}, [] if warn is None else warn
    for k, v in (d or {}).items():
        if k not in RANGES:
            raise ValueError("unknown setting " + str(k))
        if v is None or str(v).strip() == "":
            continue
        hard_lo, hard_hi, kind, lo, hi = RANGES[k]
        x = float(v)
        bounded(k, x, old.get(k), hard_lo, hard_hi, lo, hi, warn)
        out[k] = int(x) if kind == "int" else x
    return out


def apply(model, data, ctx=None, dry=False):
    """Check and save one model's settings. Returns (all model settings with this change, warnings)."""
    new, warn = settings.load(), []
    cur = new[model]
    t = float(data.get("temperature", cur["temperature"]))
    bounded("temperature", t, cur["temperature"], 0, None, 0, 2, warn)
    cur["temperature"] = t
    for k, v in (data.get("max_tokens") or {}).items():
        if k not in cur["max_tokens"]:
            raise ValueError("unknown limit " + str(k))
        v = int(v)
        lim = ctx or cur["context"]
        if v < 1:
            warn.append(f"{k.capitalize()} max tokens ({v:,}) is below 1; replies will be empty or refused.")
        elif v > lim:
            warn.append(f"{k.capitalize()} max tokens ({v:,}) is more than the {lim:,} context; such a reply is cut off.")
        elif v < 256 and v != cur["max_tokens"][k]:
            warn.append(f"{k.capitalize()} max tokens ({v:,}) is below the usual 256; most replies will be cut off.")
        cur["max_tokens"][k] = v
    c = caps(capname(model, cur))
    if "thinking" in data and c["thinking"]:
        cur["thinking"] = bool(data["thinking"])
    if "reasoning_effort" in data and c["reasoning_effort"]:
        if data["reasoning_effort"] not in EFFORT:
            raise ValueError("effort must be low, medium or high")
        cur["reasoning_effort"] = data["reasoning_effort"]
    if "thinking_budget" in data and c["thinking_budget"]:
        tbv = str(data["thinking_budget"]).strip()
        if tbv == "":
            cur.pop("thinking_budget", None)
        else:
            tbn = int(float(tbv))
            if tbn < -1 and tbn != cur.get("thinking_budget"):
                warn.append(f"Thinking budget {tbn}: below -1 (unlimited); the model server may refuse it.")
            cur["thinking_budget"] = tbn
    if "sampling" in data:
        cur["sampling"] = check_sampling(data["sampling"], cur.get("sampling"), warn)
    if "json_mode" in data:
        cur["json_mode"] = bool(data["json_mode"])
    if "enabled" in data and "enabled" in cur:   # only the Assistant has this switch
        cur["enabled"] = bool(data["enabled"])
    if not dry:
        settings.save(new)
    return new, warn


def register(app, authed):
    router = APIRouter()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/settings")
    def get_settings(request: Request):
        need(request)
        s = settings.load()
        return {"settings": s, "caps": {m: caps(capname(m, v)) for m, v in s.items()}}

    @router.post("/api/settings/save")
    async def save_settings(request: Request):
        need(request)
        data = await request.json()
        if data.get("model") not in settings.DEFAULTS:
            raise HTTPException(status_code=400, detail="unknown model")
        try:
            _, warn = apply(data["model"], data)
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "warn": warn}

    @router.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        if not authed(request):
            return RedirectResponse("/")
        import ui_server, ui_account, ui_curator, ui_cpu, ui_prompts, power, ui_tuning, ui_git, ui_update, ui_hardware, ui_web, ui_groups
        return ui_extra.PAGE.replace("__BODY__", HTML + ui_server.HTML + ui_curator.HTML + ui_prompts.HTML + ui_git.HTML + ui_web.HTML + ui_hardware.HTML + ui_groups.HTML + ui_cpu.HTML + ui_account.HTML + ui_tuning.HTML + power.HTML + ui_update.HTML).replace("__ACTIVE__", "settings")

    import downloads
    downloads.register(app, authed)
    import ui_server
    ui_server.register(app, authed)
    import ui_account
    ui_account.register(app, authed)
    import ui_curator
    ui_curator.register(app, authed)
    import ui_history
    ui_history.register(app, authed)
    import ui_queue
    ui_queue.register(app, authed)
    import ui_prompts
    ui_prompts.register(app, authed)
    import ui_tuning
    ui_tuning.register(app, authed)
    import ui_git
    ui_git.register(app, authed)
    import ui_update
    ui_update.register(app, authed)
    import ui_hardware
    ui_hardware.register(app, authed)
    import ui_web
    ui_web.register(app, authed)
    import ui_groups
    ui_groups.register(app, authed)
    import ui_cpu
    ui_cpu.register(app, authed)
    import remote
    remote.register(app, authed)
    import power
    power.register(app, authed)
    app.include_router(router)


HTML = r"""
<style>
.fld{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:8px 0}
.fld.off{opacity:.4}
.fld input[type=number]{width:120px}
input,select{background:#2a2f3a;color:#e6e9f0;border:1px solid #3a4150;border-radius:8px;padding:8px 10px;box-sizing:border-box;color-scheme:dark}
.fld label{flex:0 1 auto;min-width:0;overflow-wrap:anywhere}
.fld input[type=checkbox]{flex:none}
.fld select{flex:1;min-width:0;max-width:100%}
.fld input[type=text],.fld input[type=password]{flex:1;min-width:0}
#ac input,#dl input[type=text]{display:block;width:100%;margin:0 0 12px}
#ac button,#dl button{display:block;margin-top:4px}
</style>
<div id="sp"></div>
<div id="dl"></div>
<script>
const NAMES={qwen:"Sweeper: sweep and ask",gpt:"Reviewer: review and chat",cur:"Curator: curate",pa:"Assistant: chat and jobs"};
const LAB={sweep:"Sweep",ask:"Ask",review:"Review",chat:"Chat",curate:"Curate"};
function num(v,step){const e=h("input",{type:"number"});e.value=v;if(step)e.step=step;return e}
function fld(label,el,off){return h("div",{class:"fld"+(off?" off":"")},h("label",{},label),el)}
// Warning popup listing settings that may not be safe. With a yes label it asks (true: go ahead); without, it informs.
window.warnPop=function(title,items,yes,no){return new Promise(res=>{
 const ov=h("div",{style:"position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:60;display:flex;align-items:center;justify-content:center;padding:16px"});
 const done=v=>{ov.remove();res(v)};
 const btns=yes?[h("button",{class:"pri",onclick:()=>done(true)},yes),h("button",{onclick:()=>done(false)},no||"Go back")]:[h("button",{class:"pri",onclick:()=>done(true)},"OK")];
 ov.append(h("div",{class:"card",style:"max-width:520px;width:100%;max-height:90vh;overflow:auto"},h("h3",{},"⚠ "+title),
  h("ul",{style:"margin:0 0 12px;padding-left:18px"},...items.map(t=>h("li",{class:"mut",style:"margin-bottom:6px"},t))),...btns));
 document.body.append(ov)})};
// After a settings change: tasks that failed earlier (often because a limit was too small) can go again now.
window.offerRetry=async function(){try{
 const d=await api("/api/failed");if(!d.items.length)return;
 const list=d.items.slice(0,10).map(x=>x.what+(x.why?": "+x.why:""));if(d.items.length>10)list.push("and "+(d.items.length-10)+" more");
 if(await window.warnPop(d.items.length+(d.items.length===1?" failed task is":" failed tasks are")+" waiting. Retry them with the new settings?",list,"Retry all now","Not now"))
  await fetch("/api/failed/act",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({all:true})})}catch(e){}};
function card(m,s,cp){
 const t=num(s.temperature,"0.05"),mt={};
 const rows=Object.keys(s.max_tokens).map(k=>{mt[k]=num(s.max_tokens[k]);return fld((LAB[k]||k)+" max tokens",mt[k],false)});
 const th=h("input",{type:"checkbox"});th.checked=!!s.thinking;th.disabled=!cp.thinking;
 const re=h("select",{},...["low","medium","high"].map(o=>h("option",{value:o},o)));re.value=s.reasoning_effort||"low";re.disabled=!cp.reasoning_effort;
 const msg=h("div",{class:"mut"});
 const SP=[["top_p","Top-p","0 to 1",".01"],["top_k","Top-k","0 to 1000","1"],["min_p","Min-p","0 to 1",".01"],["typical_p","Typical-p","0 to 1",".01"],["repeat_penalty","Repeat penalty","0.5 to 2",".01"],["repeat_last_n","Repeat window (tokens)","-1 to 65536","1"],["presence_penalty","Presence penalty","-2 to 2",".05"],["frequency_penalty","Frequency penalty","-2 to 2",".05"],["mirostat","Mirostat (0 off, 1 or 2)","0 to 2","1"],["mirostat_tau","Mirostat target","0 to 20",".5"],["mirostat_eta","Mirostat rate","0 to 1",".01"],["dry_multiplier","DRY repeat blocker","0 to 5",".05"],["dry_base","DRY base","1 to 4",".05"],["dry_allowed_length","DRY allowed length","1 to 100","1"],["xtc_probability","XTC probability","0 to 1",".01"],["xtc_threshold","XTC threshold","0 to 0.5",".01"],["seed","Seed (-1 random)","-1 and up","1"]];
 const sp={},sr=[],sv=s.sampling||{};
 SP.forEach(function(a){const e=h("input",{type:"number",step:a[3],placeholder:"default"});e.value=sv[a[0]]==null?"":sv[a[0]];sp[a[0]]=e;sr.push(fld(a[1]+" ("+a[2]+")",e,false))});
 const jm=h("input",{type:"checkbox"});jm.checked=!!s.json_mode;
 const en="enabled" in s?h("input",{type:"checkbox"}):null;if(en)en.checked=!!s.enabled;
 const tb=num(s.thinking_budget==null?"":s.thinking_budget);tb.disabled=!cp.thinking_budget;
 function mir(){const on=Number(sp.mirostat.value||0)>0;["top_k","top_p","min_p","typical_p"].forEach(function(k){sp[k].disabled=on});["mirostat_tau","mirostat_eta"].forEach(function(k){sp[k].disabled=!on})}
 sp.mirostat.oninput=mir;mir();
 const go=async()=>{msg.textContent="";
  const b={model:m,temperature:t.value,max_tokens:{}};
  for(const k in mt)b.max_tokens[k]=mt[k].value;
  if(cp.thinking)b.thinking=th.checked;
  if(cp.reasoning_effort)b.reasoning_effort=re.value;
  try{await api("/api/settings/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});msg.textContent="Saved."}
  catch(e){msg.textContent=e.message}};
 (window.COLS=window.COLS||{})["m_"+m]=()=>{const b={model:m,temperature:t.value,max_tokens:{}};for(const k in mt)b.max_tokens[k]=mt[k].value;if(cp.thinking)b.thinking=th.checked;if(cp.reasoning_effort)b.reasoning_effort=re.value;b.sampling={};for(const k in sp)b.sampling[k]=sp[k].value;b.json_mode=jm.checked;if(cp.thinking_budget)b.thinking_budget=tb.value;if(en)b.enabled=en.checked;return b};
 return h("div",{class:"card"},h("h3",{},NAMES[m]),
  h("div",{class:"mut"},s.model_file+" · context "+s.context.toLocaleString()),
  en?fld("Use the Assistant for chat (off: the Reviewer answers, as before)",en,false):null,
  fld("Temperature",t,false),...rows,
  fld("Thinking",th,!cp.thinking),
  fld("Reasoning effort",re,!cp.reasoning_effort),fld("Thinking budget (tokens, -1 unlimited)",tb,!cp.thinking_budget),fld("Force valid JSON (experimental)",jm,false),h("details",{},h("summary",{class:"mut"},"Sampling (blank uses the server default)"),...sr),
  h("button",{class:"pri",onclick:()=>window.applyAll()},"Apply all"),msg)}
async function load(){const d=await api("/api/settings");
 $("#sp").replaceChildren(...Object.keys(NAMES).map(m=>card(m,d.settings[m],d.caps[m])))}
function gb(n){return n>=1e9?(n/1e9).toFixed(1)+" GB":Math.round(n/1e6)+" MB"}
let dlT=null,MODELS=[];
function holdConfirm(title,text){return new Promise(res=>{
 const ov=h("div",{style:"position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:60;display:flex;align-items:center;justify-content:center;padding:16px"});
 const done=v=>{ov.remove();res(v)};
 const fill=h("div",{style:"position:absolute;left:0;top:0;bottom:0;width:0;background:#b91c1c"});
 const yes=h("button",{style:"position:relative;overflow:hidden;user-select:none;-webkit-user-select:none;touch-action:none;margin-right:8px"},fill,h("span",{style:"position:relative"},"Hold to delete"));
 let t=null;
 const stop=()=>{clearTimeout(t);t=null;fill.style.transition="width .15s";fill.style.width="0"};
 yes.onpointerdown=e=>{e.preventDefault();fill.style.transition="width 1.5s linear";fill.style.width="100%";t=setTimeout(()=>done(true),1500)};
 yes.onpointerup=stop;yes.onpointerleave=stop;yes.onpointercancel=stop;yes.oncontextmenu=e=>e.preventDefault();
 ov.append(h("div",{class:"card",style:"max-width:420px;width:100%"},h("h3",{},title),h("div",{class:"mut",style:"margin-bottom:12px"},text),yes,h("button",{class:"pri",onclick:()=>done(false)},"No")));
 document.body.append(ov)})}
async function dlDel(m){
 const sp=m.name.match(/^(.*)-\d{5}-of-(\d{5})\.gguf$/);
 const grp=sp?MODELS.filter(x=>x.name.startsWith(sp[1]+"-")&&x.name.endsWith("-of-"+sp[2]+".gguf")):[m];
 const size=grp.reduce((a,x)=>a+x.size,0);
 const ok=await holdConfirm("Delete "+(sp?sp[1]+" ("+grp.length+" parts)":m.name)+"?",gb(size)+" will be deleted from the server and cannot be undone. You can download it again from a link.");
 if(!ok)return;
 const r=await dlPost("/api/models/delete",{name:m.name});
 if(!r.ok)alert(r.detail||"Could not delete");
 dlLoad()}
const dlUrl=h("input",{type:"text",placeholder:"https://huggingface.co/.../model.gguf"});
const dlModels=h("div"),dlList=h("div");
async function dlPost(path,body){const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
 const j=await r.json().catch(()=>({}));return{ok:r.ok,status:r.status,detail:j.detail||""}}
async function dlStart(url,ok){const r=await dlPost("/api/download/start",{url:url,confirmed:ok});
 if(r.status===409&&String(r.detail).startsWith("third_party:")){
  const host=r.detail.slice(12);
  if(confirm("Third-party warning: "+host+" is not Hugging Face. Files from other sites are not checked and may be unsafe or not what they claim. Download anyway?"))return dlStart(url,true);
  return}
 if(!r.ok){alert(r.detail||"Could not start");return}
 dlUrl.value="";dlLoad()}
$("#dl").replaceChildren(h("div",{class:"card"},h("h3",{},"Models"),dlModels,
 h("div",{style:"margin-top:12px"},dlUrl),h("button",{class:"pri",onclick:()=>dlUrl.value.trim()&&dlStart(dlUrl.value.trim(),false)},"Download"),dlList));
async function dlLoad(){clearTimeout(dlT);const d=await api("/api/models");
 MODELS=d.models;if(window.refreshModelFiles)window.refreshModelFiles(d.models.map(x=>x.name));dlModels.replaceChildren(...d.models.map(m=>h("div",{class:"fld"},h("span",{style:"flex:1;min-width:0;overflow-wrap:anywhere"},m.name),h("span",{class:"mut",style:"white-space:nowrap;text-align:right"},gb(m.size)+(m.use&&m.use.length?" · in use":"")),h("button",{style:(m.use&&m.use.length?"opacity:.35;":"")+"flex:none;width:44px;margin-left:10px;padding:2px 0;min-width:0",title:m.use&&m.use.length?"In use by "+m.use.join(", "):"Delete",onclick:()=>{if(m.use&&m.use.length)alert("In use by "+m.use.join(", ")+". Pick another model there first.");else dlDel(m)}},"🗑"))));
 dlList.replaceChildren(...d.downloads.map(x=>{
  const live=x.status==="running"||x.status==="starting";
  const line=x.status==="failed"?"Failed: "+x.error:x.status==="interrupted"?"Interrupted at "+gb(x.done):gb(x.done)+" of "+(x.total?gb(x.total):"unknown size");
  const r=h("div",{style:"padding:8px 0"},h("div",{},x.name+(x.third_party?" ("+x.host+")":"")),
   h("progress",{max:x.total||1,value:x.done,style:"width:100%"}),h("div",{class:"mut"},line));
  if(!live)r.append(h("button",{onclick:()=>dlStart(x.url,x.third_party)},"Resume"));
  r.append(h("button",{onclick:async()=>{await dlPost("/api/download/cancel",{name:x.name});dlLoad()}},"Cancel"));
  return r}));
 if(d.downloads.some(x=>x.status==="running"||x.status==="starting"))dlT=setTimeout(dlLoad,2000)}
load();dlLoad();
</script>
"""
