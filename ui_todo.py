"""To-do page: what the reviewer found that you have not finished. Registered from ui_extra.register()."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import todo
import ui_extra


def register(app, authed):
    router = APIRouter()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/todo")
    def list_t(request: Request):
        need(request)
        out = {"open": todo.list_tasks("open"), "done": todo.list_tasks("done")[-15:]}
        for t in out["open"] + out["done"]:
            t["waiting"] = sum(not u["processed"] for u in t["updates"])
        return out

    @router.get("/api/todo/count")
    def count_t(request: Request):
        need(request)
        return {"open": todo.count_open()}

    @router.post("/api/todo/update")
    async def update_t(request: Request):
        need(request)
        data = await request.json()
        try:
            todo.add_update(str(data.get("id", "")), data.get("text", ""), bool(data.get("finished")))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="no such task")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "open": todo.count_open()}

    @router.post("/api/todo/drop")
    async def drop_t(request: Request):
        need(request)
        data = await request.json()
        try:
            todo.drop(str(data.get("id", "")))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="no such task")
        return {"ok": True, "open": todo.count_open()}

    @router.get("/todo", response_class=HTMLResponse)
    def todo_page(request: Request):
        if not authed(request):
            return RedirectResponse("/")
        return ui_extra.PAGE.replace("__BODY__", TODO_HTML).replace("__ACTIVE__", "todo")

    app.include_router(router)


TODO_HTML = r"""
<p class="mut" style="margin:4px 2px">Things the reviewer found in your notes that look unfinished. Type what you did or where it stands, and it goes to the reviewer, which updates its notes.</p>
<div id="open"></div>
<details class="card"><summary class="mut">Finished recently</summary><div id="done" class="mut"></div></details>
<script>
function task(t){
 const box=h("textarea",{rows:"3",placeholder:"What did you do, or where does it stand?"});
 const send=async(path,body)=>{try{await api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});badgeTodo();load()}catch(e){alert(e.message)}};
 return h("div",{class:"card"},h("h3",{},t.title),h("div",{class:"mut"},t.detail),
  t.quote?h("div",{class:"mut",style:"border-left:3px solid var(--line);padding-left:8px;margin:6px 0"},t.quote):null,
  h("div",{class:"mut"},"From ",h("a",{class:"wl",href:"/graph#"+encodeURIComponent(t.source)},t.source)),
  t.state_line?h("div",{style:"margin-top:6px"},"Current state: "+t.state_line):null,
  t.waiting?h("div",{class:"mut",style:"color:var(--warn)"},t.waiting+" update(s) waiting for the reviewer"):null,
  box,
  h("button",{class:"pri",onclick:()=>box.value.trim()&&send("/api/todo/update",{id:t.id,text:box.value})},"Send update"),
  h("button",{onclick:()=>send("/api/todo/update",{id:t.id,text:box.value,finished:true})},"I finished this"),
  h("button",{onclick:()=>send("/api/todo/drop",{id:t.id})},"Not a task"))}
async function load(){const d=await api("/api/todo");
 $("#open").replaceChildren(...(d.open.length?d.open.map(task):[h("div",{class:"card mut"},"Nothing open. The reviewer adds tasks here as it reads your notes.")]));
 $("#done").replaceChildren(...d.done.map(t=>h("div",{},"✔ "+t.title+(t.state_line?" — "+t.state_line:""))))}
load();
</script>
"""
