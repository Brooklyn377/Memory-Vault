"""Settings page, curator part: on/off switch, jobs, prompt size and live status."""
from fastapi import APIRouter, HTTPException, Request

import settings

FLAGS = ("enabled", "verify", "connect", "prune", "cycle", "map")


def register(app, authed):
    router = APIRouter()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/curator")
    def get(request: Request):
        need(request)
        import curator
        return curator.summary()

    @router.post("/api/curator/save")
    async def save(request: Request):
        need(request)
        import curator
        d = await request.json()
        try:
            c = curator.load_cfg()
            for k in FLAGS:
                if k in d:
                    c[k] = bool(d[k])
            if "prompt_tokens" in d:
                s = settings.get("cur")
                top = s["context"] - s["max_tokens"]["curate"] - 2000
                n = int(d["prompt_tokens"])
                if not 4000 <= n <= top:
                    raise ValueError(f"prompt size must be 4,000 to {top:,} (context minus the token limit)")
                c["prompt_tokens"] = n
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        curator.save_cfg(c)
        return {"ok": True}

    app.include_router(router)


HTML = r"""
<div id="cu"></div>
<script>
(function(){
const F=[["enabled","Run when the Sweeper and Reviewer are idle"],["verify","Verify facts against the chats"],["connect","Find new connections between notes"],["prune","Prune notes that hold nothing useful"],["cycle","Keep going with new pairs after every note is checked"],["map","Map pass: link and group the whole vault once per-note review has nothing left"]];
const E={};
const pt=h("input",{type:"number","data-instant":"1"});
const st=h("div",{class:"mut"}),sm=h("div",{class:"mut"}),msg=h("div",{class:"mut"});
async function save(){msg.textContent="";const b={prompt_tokens:pt.value};for(const [k] of F)b[k]=E[k].checked;
 const r=await fetch("/api/curator/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});
 const j=await r.json().catch(()=>({}));
 msg.textContent=r.ok?"Saved. Switching off finishes the current note, then swaps the Sweeper and Reviewer back in.":(j.detail||"Could not save")}
const rows=F.map(([k,l])=>{const e=h("input",{type:"checkbox","data-instant":"1"});E[k]=e;e.onchange=save;return h("div",{class:"fld"},h("label",{},l),e)});
pt.onchange=save;
async function load(first){const d=await api("/api/curator");
 if(first){for(const [k] of F)E[k].checked=!!d.cfg[k];pt.value=d.cfg.prompt_tokens}
 const s=d.status||{},t=d.stats||{};
 st.textContent="Status: "+(s.state||"unknown")+(s.why?" - "+s.why:"")+(s.note?" - reading "+s.note:"");
 sm.textContent=d.checked+" of "+d.total+" entity notes checked, "+(t.links||0)+" links added, "+(t.asked||0)+" questions asked, "+(t.pruned||0)+" pruned, "+(t.verified||0)+" facts verified"}
$("#cu").replaceChildren(h("div",{class:"card"},h("h3",{},"Curator (swaps in for the Sweeper and Reviewer when both are idle)"),...rows,
 h("div",{class:"fld"},h("label",{},"Prompt size per note (tokens)"),pt),st,sm,msg));
load(true);setInterval(()=>load(false),5000);
})();
</script>
"""
