"""Settings page, Web access: let the Assistant search the web and read pages. Off by default."""
from fastapi import APIRouter, HTTPException, Request

import webtools


def register(app, authed):
    router = APIRouter()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/web")
    def get(request: Request):
        need(request)
        return webtools.config()

    @router.post("/api/web/save")
    async def save(request: Request):
        need(request)
        try:
            return webtools.save_config(await request.json())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    app.include_router(router)


HTML = r"""
<div id="web"></div>
<script>
(function(){
const msg=h("div",{class:"mut"});
const on=h("input",{type:"checkbox"});
const eng=h("select",{},h("option",{value:"duckduckgo"},"DuckDuckGo (no account)"),h("option",{value:"searxng"},"SearXNG (your own)"));
const sx=h("input",{type:"text",placeholder:"http://192.168.1.60:8888"});
const pa=h("input",{type:"checkbox"});
function fld2(l,el,off){return h("div",{class:"fld"+(off?" off":"")},h("label",{},l),el)}
const sxrow=fld2("SearXNG address",sx);
function sync(){sxrow.style.display=eng.value==="searxng"?"":"none"}
eng.onchange=sync;
async function load(){try{const c=await api("/api/web");on.checked=!!c.enabled;eng.value=c.engine;sx.value=c.searxng_url||"";pa.checked=!!(c.models&&c.models.pa);sync()}catch(e){}}
async function save(){
 if(on.checked&&!await window.warnPop("Turn on web access?",["The Assistant can then search the web and open pages when the vault and your Git servers have nothing on a question. Its search words and the page addresses it opens leave this container for the internet; your notes are not sent, only the words it chooses to search for (keys and tokens are removed).","Addresses on your own network, this machine and link-local addresses are never opened.","Every reply that used the web lists what it searched and opened, and gives its sources."],"Turn on","Not now")){on.checked=false;return}
 const r=await fetch("/api/web/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:on.checked,engine:eng.value,searxng_url:sx.value,models:{pa:pa.checked}})});
 const j=await r.json().catch(function(){return{}});msg.textContent=r.ok?"Saved.":(j.detail||"Could not save");if(!r.ok)window.warnPop("Could not save",[j.detail||"Could not save"])}
$("#web").replaceChildren(h("div",{class:"card"},h("h3",{},"Web access"),
 h("div",{class:"mut"},"Off by default. The Assistant looks in the vault and your Git servers first, and uses the web only for what they do not have."),
 fld2("Allow web access",on),fld2("Search engine",eng),sxrow,
 h("div",{class:"mut",style:"margin-top:6px"},"Which models may use it:"),
 fld2("Assistant",pa),
 h("div",{class:"mut"},"The Sweeper, Reviewer and Curator read chats and notes only; they have no tools that could use the web, so there is nothing to switch for them."),
 h("button",{class:"pri",style:"margin-top:8px",onclick:save},"Save"),msg));
load();
})();
</script>
"""
