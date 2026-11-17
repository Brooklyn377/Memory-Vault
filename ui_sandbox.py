"""Settings page, Sandbox internet: the hosts that commands in the Assistant's sandbox may always reach. Any other host
asks you in the chat first (see sandbox.py)."""
from fastapi import APIRouter, HTTPException, Request

import sandbox


def register(app, authed):
    router = APIRouter()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/sandbox/hosts")
    def get(request: Request):
        need(request)
        return {"hosts": [{"host": h, "where": sandbox.where(h)} for h in sandbox.hosts()]}

    @router.post("/api/sandbox/hosts")
    async def change(request: Request):
        """{"add": host} or {"remove": host}."""
        need(request)
        d = await request.json()
        try:
            if d.get("remove"):
                sandbox.remove_host(d["remove"])
            else:
                sandbox.add_host(d.get("add"))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)[:1].upper() + str(e)[1:] + ".")
        return get(request)

    app.include_router(router)


HTML = r"""
<script>
(function(){
const msg=h("div",{class:"mut"}),list=h("div",{}),box=h("input",{type:"text",placeholder:"host name, for example pypi.org"});
function draw(d){list.replaceChildren(...(d.hosts.length?d.hosts.map(function(x){
 return h("div",{style:"display:flex;gap:8px;align-items:center;margin:4px 0"},h("span",{},x.host+(x.where?" ("+x.where+")":"")),
  h("button",{style:"padding:0 8px",onclick:function(){send({remove:x.host})}},"Remove"))}):[h("div",{class:"mut"},"No hosts yet.")]))}
async function send(body){msg.textContent="";
 try{draw(await api("/api/sandbox/hosts",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}));if(body.add)box.value=""}
 catch(e){msg.textContent=e.message}}
// Held by the Chats card (ui_chats.py), as its Sandbox internet section.
window.sandboxNode=h("div",{},
 h("div",{class:"mut"},"Hosts that sandbox commands may always reach. Any other host asks you in the chat first."),
 list,h("div",{style:"display:flex;gap:8px;margin-top:6px"},box,h("button",{onclick:function(){if(box.value.trim())send({add:box.value})}},"Add")),msg);
api("/api/sandbox/hosts").then(draw).catch(function(){});
})();
</script>
"""
