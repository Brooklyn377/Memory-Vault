"""Settings > Tag colours: the colour of each kind of note on the Map. One list, kept on the server, so every device
shows the same. A kind with no colour of its own uses the colour of "note". Only the colours you pick are saved."""
import json
import re

from fastapi import APIRouter, HTTPException, Request

import agent
import ui_extra

FILE = agent.STATE / "tag_colors.json"
DEFAULTS = {"entity": "#5b8def", "summary": "#34d399", "timeline": "#f5a524", "conversation": "#8b93a7", "raw": "#8b93a7",
            "claude-memory": "#c084fc", "note": "#f472b6", "file": "#22d3ee", "file-note": "#22d3ee", "updated-file": "#facc15",
            "hub": "#a3e635"}
HEX = re.compile(r"#[0-9a-fA-F]{6}")


def saved():
    try:
        d = json.loads(FILE.read_text())
        return {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, str) and HEX.fullmatch(v)}
    except (OSError, ValueError, AttributeError):
        return {}


def listing():
    v = ui_extra.vault()
    counts = {}
    for rel in v.body:
        t = ui_extra.ntype(v, rel)
        counts[t] = counts.get(t, 0) + 1
    mine = saved()
    tags = sorted(set(DEFAULTS) | set(counts) | set(mine))
    rows = [{"tag": t, "color": mine.get(t) or DEFAULTS.get(t) or DEFAULTS["note"], "default": DEFAULTS.get(t) or DEFAULTS["note"],
             "custom": t in mine, "count": counts.get(t, 0)} for t in tags]
    return {"tags": rows, "colors": {r["tag"]: r["color"] for r in rows}}


def register(app, authed):
    router = APIRouter()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/tagcolors")
    def get(request: Request):
        need(request)
        return listing()

    @router.post("/api/tagcolors/save")
    async def save(request: Request):
        need(request)
        d = await request.json()
        mine = saved()
        for tag, color in (d.get("colors") or {}).items():
            if not isinstance(tag, str) or not tag.strip() or not isinstance(color, str) or not HEX.fullmatch(color):
                raise HTTPException(status_code=400, detail="a colour looks like #aabbcc")
            mine[tag.strip()] = color.lower()
        for tag in d.get("reset") or []:
            mine.pop(str(tag), None)
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(mine, indent=1))
        agent.log(f"settings: tag colours changed ({', '.join(sorted((d.get('colors') or {}).keys() | set(d.get('reset') or []))) or 'none'})")
        return listing()

    app.include_router(router)


HTML = r"""
<div id="tc"></div>
<script>
(function(){
const msg=h("div",{class:"mut"}),box=h("div",{});
function row(r){
 const pick=h("input",{type:"color","data-instant":"1",style:"width:44px;height:30px;padding:0;border:0;background:none;margin:0 8px 0 0"});pick.value=r.color;
 const rs=h("button",{style:"padding:2px 8px;font-size:13px"},"Reset");rs.style.display=r.custom?"":"none";
 async function send(body){try{const d=await api("/api/tagcolors/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});draw(d);msg.textContent="Saved."}catch(e){msg.textContent=e.message}}
 pick.onchange=function(){send({colors:{[r.tag]:pick.value}})};rs.onclick=function(){send({reset:[r.tag]})};
 return h("div",{style:"display:flex;align-items:center;padding:4px 0"},pick,h("span",{style:"flex:1"},r.tag+" ",h("span",{class:"mut"},"("+r.count+(r.count===1?" note":" notes")+")")),rs)}
function draw(d){box.replaceChildren(...d.tags.map(row))}
$("#tc").replaceChildren(h("div",{class:"card"},h("h3",{},"Tag colours"),
 h("div",{class:"mut"},"The colour of each kind of note on the Map. A kind that has no colour of its own uses the colour of note. Colours are saved as you pick them and are the same on every device."),box,msg));
api("/api/tagcolors").then(draw).catch(function(e){msg.textContent=e.message});
})();
</script>
"""
