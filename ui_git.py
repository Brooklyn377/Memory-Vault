"""Settings page, Git servers: add, change and remove entries, type tokens per model, test them and pull now."""
from fastapi import APIRouter, HTTPException, Request

import gitrepos


def register(app, authed):
    router = APIRouter()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/git")
    def listing(request: Request):
        need(request)
        return {"servers": [gitrepos.public(e) for e in gitrepos.entries()], "models": gitrepos.MODELS}

    async def act(request, fn):
        need(request)
        d = await request.json()
        try:
            return fn(d)
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/api/git/save")
    async def save(request: Request):
        return await act(request, lambda d: {"ok": True, "server": gitrepos.save(d)})

    @router.post("/api/git/remove")
    async def remove(request: Request):
        return await act(request, lambda d: (gitrepos.remove(str(d.get("id", ""))), {"ok": True})[1])

    @router.post("/api/git/test")
    async def test(request: Request):
        return await act(request, lambda d: {"ok": gitrepos.test(d.get("url"), str(d.get("id") or ""), d.get("model"), d.get("token"))})

    @router.post("/api/git/pull")
    async def pull(request: Request):
        def go(d):
            eid = str(d.get("id", ""))
            who = next((m for m in gitrepos.MODELS if gitrepos.has_token(eid, m)), None)
            if not who:
                raise ValueError("no model has a token for this Git server yet")
            if gitrepos.copy(eid, who, force=True) is None or (gitrepos.get(eid) or {}).get("error"):
                raise ValueError((gitrepos.get(eid) or {}).get("error") or "the pull failed")
            return {"ok": True}
        return await act(request, go)

    app.include_router(router)


HTML = r"""
<div id="gitsv"></div>
<script>
(function(){
let MODELS={};
async function post(path,body){const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});const j=await r.json().catch(function(){return{}});return{ok:r.ok,detail:j.detail||""}}
function row(label,el){return h("div",{class:"fld"},h("label",{},label),el)}
function when(t){return t?new Date(t*1000).toLocaleString([],{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}):"never"}
function form(s,open){
 const isNew=!s.id;
 const name=h("input",{type:"text",placeholder:"a short name, e.g. docs"});name.value=s.name||"";
 const desc=h("textarea",{rows:"2",placeholder:"What it is for, in your words. The Assistant reads this to decide where to look."});desc.value=s.description||"";
 const url=h("input",{type:"text",placeholder:"https://git.example.com/owner/repo.git"});url.value=s.url||"";
 const br=h("input",{type:"text",placeholder:"main"});br.value=s.branch||"main";
 const acc=h("select",{},h("option",{value:"read"},"Read only (the bot never pushes)"),h("option",{value:"write"},"Read and write (still asks you first)"));acc.value=s.access||"read";
 const msg=h("div",{class:"mut"}),tok={},clr={},tmsg={};
 const trows=Object.keys(MODELS).map(function(m){
  const have=!!(s.tokens&&s.tokens[m]);
  tok[m]=h("input",{type:"password",autocomplete:"off",placeholder:have?"saved, type to replace":"none: it cannot read this"});
  clr[m]=h("input",{type:"checkbox"});tmsg[m]=h("span",{class:"mut"});
  const tb=h("button",{onclick:async function(){tmsg[m].textContent="Testing...";const r=await post("/api/git/test",{url:url.value,id:s.id||"",model:m,token:tok[m].value});tmsg[m].textContent=r.ok?"Works":r.detail}},"Test");
  return h("div",{style:"padding:4px 0"},row(MODELS[m]+" token",tok[m]),h("div",{class:"mut",style:"font-size:12px"},have?h("label",{},clr[m]," remove the saved token "):null,tb," ",tmsg[m]))});
 async function save(){msg.textContent="";const t={},c={};for(const m in tok){t[m]=tok[m].value;c[m]=clr[m].checked}
  const r=await post("/api/git/save",{id:s.id||"",name:name.value,description:desc.value,url:url.value,branch:br.value,access:acc.value,tokens:t,clear:c});
  if(!r.ok){msg.textContent=r.detail;return}load()}
 async function del(){if(!confirm("Remove "+s.name+" from the list? Its saved tokens and the agent's local copy go. The repository itself is not touched."))return;
  const r=await post("/api/git/remove",{id:s.id});if(!r.ok){msg.textContent=r.detail;return}load()}
 async function pull(){msg.textContent="Pulling...";const r=await post("/api/git/pull",{id:s.id});msg.textContent=r.ok?"Pulled.":r.detail;if(r.ok)load()}
 const head=isNew?"New Git server":s.name+" · "+(s.access==="write"?"read and write":"read only")+" · pulled "+when(s.pulled);
 const d=h("details",{class:"sec"},h("summary",{},head),
  s.error?h("div",{style:"color:#f5a524;margin:6px 0"},"Last pull failed: "+s.error):null,
  row("Name",name),desc,row("Address",url),row("Branch",br),row("Access",acc),
  h("div",{class:"mut",style:"margin:6px 0"},"A token per model. A model without one cannot read this server. Tokens are stored root-only and never shown again or sent to a model."),
  ...trows,
  h("button",{class:"pri",onclick:save},isNew?"Add":"Save"),isNew?null:h("button",{onclick:pull},"Pull now"),isNew?h("button",{onclick:load},"Cancel"):h("button",{onclick:del},"Remove"),msg);
 d.open=!!open;return d}
async function load(){const d=await api("/api/git");MODELS=d.models;
 const list=d.servers.map(function(s){return form(s,false)});
 const add=h("button",{class:"pri",onclick:function(){add.replaceWith(form({},true))}},"Add Git server");
 $("#gitsv").replaceChildren(h("div",{class:"card"},h("h3",{},"Git servers"),
  h("div",{class:"mut"},d.servers.length?"Repositories the models can read, with what each is for.":"None yet. Add one so the models can read a repository, for example your documentation."),...list,add))}
load();
})();
</script>
"""
