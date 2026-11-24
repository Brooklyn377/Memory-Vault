"""Stats page: memory per device (this server, each machine with a model runner, each graphics card), whether each
runner is up to date, and the last server change. Nothing here changes anything: it only reads. Registered from ui_extra.register()."""
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import ui_extra

NICE = {"qwen": "Sweeper", "gpt": "Reviewer", "cur": "Curator", "pa": "Assistant", "hp": "Helper"}


def _here():
    import memview
    out = {"memory": memview.memory(), "gpus": []}
    try:
        import serverflags
        if serverflags.backend() != "cpu":   # a GPU is set up in this container
            import model_runner
            out["gpus"] = model_runner.gpus()
    except Exception:
        pass
    return out


def _server(p, ru, s):
    import remote
    used = [NICE[m] for m in NICE if (s.get(m, {}).get("remote") or {}).get("enabled")
            and (s[m].get("remote") or {}).get("server") == p["id"]]
    d = {"name": p["name"], "url": p["url"], "runner": bool(p.get("runner")), "update": ru.get(p["id"]), "used_by": used}
    if p.get("runner"):
        try:
            d["status"] = remote.server_runner(p["id"], "GET", "/status", timeout=6)
        except ValueError as e:
            d["error"] = str(e)
    return d


def stats():
    import applier
    import remote
    import runner_update
    import settings
    s, ru, servers = settings.load(), runner_update.load(), remote.servers()
    with ThreadPoolExecutor(max_workers=max(1, len(servers)) + 1) as pool:   # every runner is asked at once
        here = pool.submit(_here)
        got = [pool.submit(_server, p, ru, s) for p in servers]
        return {"here": here.result(), "servers": [g.result() for g in got], "last": applier.last()}


def register(app, authed):
    router = APIRouter()

    @router.get("/api/stats")
    def api_stats(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        return stats()

    @router.get("/stats", response_class=HTMLResponse)
    def stats_page(request: Request):
        if not authed(request):
            import ui_think
            return RedirectResponse(ui_think._login_path(app), status_code=303)
        return ui_extra.PAGE.replace("__BODY__", HTML).replace("__ACTIVE__", "stats")

    app.include_router(router)


HTML = r"""
<style>
.stw{padding:4px 12px 24px}
.stw h2{font-size:18px;margin:18px 0 2px}
.stw .url{color:var(--mut);font-weight:400;font-size:14px}
.stw .ln{margin:4px 0}
.stw .dev{margin:12px 0 4px;font-weight:600}
.sbar{display:flex;height:16px;border-radius:8px;overflow:hidden;background:var(--line);margin:6px 0}
.sbar div{height:100%}
.lg{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:14px}
.lg i{display:inline-block;width:10px;height:10px;border-radius:5px;margin-right:6px;vertical-align:middle}
</style>
<div class="stw" id="st"><div class="mut">Loading...</div></div>
<script>
(function(){
const C={qwen:"#4c9aff",gpt:"#f5a623",cur:"#34d399",pa:"#e879f9",hp:"#22d3ee"};
const N={qwen:"Sweeper",gpt:"Reviewer",cur:"Curator",pa:"Assistant",hp:"Helper"};
const OTHER="#8b93a7",CACHE="#5b6475",USED="#4c9aff",FREE="transparent";
const gb=b=>(Number(b||0)/1073741824).toFixed(1)+" GB";
function line(t,cls){return h("div",{class:"ln"+(cls?" "+cls:"")},t)}
// One device: a bar of its parts and a legend under it. parts: [label, bytes, colour, extra text]
function bar(parts,total){
 const b=h("div",{class:"sbar"});
 parts.forEach(p=>{if(p[1]>0&&total>0)b.append(h("div",{style:"width:"+Math.min(100,p[1]/total*100)+"%;background:"+p[2],title:p[0]}))});
 const lg=h("div",{class:"lg"},...parts.map(p=>h("span",{},h("i",{style:"background:"+(p[2]===FREE?"var(--line)":p[2])}),p[0]+" "+gb(p[1])+(p[3]||""))));
 return [b,lg]}
function cards(list){
 if(!list||!list.length)return [line("Card memory: cannot be read on this machine","mut")];
 const out=[];
 list.forEach(g=>{out.push(h("div",{class:"dev"},g.name+" · card memory "+gb(g.used)+" of "+gb(g.total)+(g.gtt==null?"":" · system RAM used by the card (GTT) "+(Number(g.gtt)/1073741824).toFixed(2)+" GB")));
  out.push(...bar([["Used",g.used,USED],["Free",Math.max(0,g.total-g.used),FREE]],g.total))});
 return out}
// Whether this server's runner runs the same model_runner.py as this agent (runner_update.py keeps it so).
function runnerLine(u){
 if(!u||!u.state)return "Runner: not checked yet; the agent checks it once a minute.";
 const c=String(u.commit||"").slice(0,7),t=u.at?new Date(u.at*1000).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"}):"";
 switch(u.state){
  case "current":return "Runner: up to date (same as this agent, commit "+c+").";
  case "updating":return "Runner: updating to commit "+c+". It downloads model_runner.py from GitHub and restarts.";
  case "failed":return "Runner: the update failed at "+t+": "+String(u.error||"").replace(/\.$/,"")+". It keeps running its current version.";
  case "waiting":return "Runner: an update is waiting (commit "+c+"); the runner cannot be reached now. It updates as soon as it can be reached.";
  case "old":return "Runner: this runner is too old to update itself. Update it once by hand (README, \"Model runner\"); after that it updates along with the agent.";
  case "unknown":return "Runner: it differs from this agent's copy, but it cannot be updated from here: this agent's installed version is unknown (no /opt/memory-agent/VERSION).";
 }
 return "Runner: "+u.state}
function who(list){return list.length?", for the "+list.join(" and the "):""}
function here(d){
 const m=d.memory,out=[h("h2",{},"This server")];
 if(m&&m.total){
  out.push(h("div",{class:"dev"},"Memory · "+gb(m.total)));
  const parts=[];
  Object.keys(N).forEach(k=>{const x=m.models[k];if(x&&x.rss)parts.push([N[k],x.rss,C[k]," (weights "+(x.file/1073741824).toFixed(1)+" + working "+(x.anon/1073741824).toFixed(1)+")"+(x.exp>x.rss*1.05?" · expected "+gb(x.exp)+" when fully loaded":"")])});
  parts.push(["Other",m.other,OTHER],["File cache",m.cached,CACHE," (model files count here)"],["Free",m.free,FREE]);
  out.push(...bar(parts,m.total));
  if(m.swap_total)out.push(line("Swap "+gb(m.swap_used)+" of "+gb(m.swap_total)+(m.swap_used>m.swap_total*0.9?" (nearly full)":""),"mut"))}
 if(d.gpus&&d.gpus.length)out.push(...cards(d.gpus));
 return out}
function server(p){
 const out=[h("h2",{},p.name+" ",h("span",{class:"url"},"· "+p.url))];
 if(!p.runner){out.push(line("No runner on that machine, so nothing can be read from it.","mut"));return out}
 out.push(line(runnerLine(p.update)));
 if(p.error){out.push(line("The runner cannot be reached: "+p.error));return out}
 const s=p.status||{},n=(s.models||[]).length,mf=(s.settings||{}).model_file;
 out.push(line("llama-server: "+(s.stopped?"stopped":s.health||"?")+" · "+(mf?mf+who(p.used_by):"no model chosen yet")+" · "+n+" model file"+(n===1?"":"s")+" on that machine"));
 if(!("ram" in s)){out.push(line("This runner does not report memory yet; it will after it updates.","mut"));return out}
 const r=s.ram;
 if(r){out.push(h("div",{class:"dev"},"Memory · "+gb(r.limit)+" (the container's limit)"));
  const b=bar([["In use",r.used,USED],["File cache",r.cache,CACHE],["Free",r.free,FREE]],r.limit);
  if(s.cache_mib)b[1].append(h("span",{},"· prompt cache limit "+gb(s.cache_mib*1048576)));
  out.push(...b)}
 out.push(...cards(s.gpus));
 return out}
function last(l){
 const f=v=>v.model_file+", context "+Number(v.context).toLocaleString()+", "+v.threads+" threads, "+v.parallel+" slots";
 const nm=l&&(N[l.model]||l.model)+" server";
 return [h("h2",{},"Last change"),line(!l?"No server change made yet.":l.error?nm+" failed and was rolled back: "+l.error:nm+": "+f(l.before)+"  →  "+f(l.after))]}
async function load(){
 try{const d=await api("/api/stats");$("#st").replaceChildren(h("h1",{},"Stats"),...here(d.here),...d.servers.flatMap(server),...last(d.last))}
 catch(e){if(e)$("#st").replaceChildren(line("Could not load the stats: "+(e.message||e)))}}
load();setInterval(function(){if(!document.hidden)load()},5000);
})();
</script>
"""
