"""Whole-vault map: every note as a dot, every [[wikilink]] as a line (Obsidian-style).
Registered from ui_extra.register(), so it sits behind the same login."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import ui_extra

MAX_NODES = 3000


def register(app, authed):
    router = APIRouter()

    @router.get("/api/graph/all")
    def graph_all(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        v = ui_extra.vault()
        rels = sorted(v.body, key=lambda r: (-ui_extra.degree(v, r), r))[:MAX_NODES]
        idx = {r: i for i, r in enumerate(rels)}
        nodes = [{"p": r, "t": v.title(r), "ty": ui_extra.ntype(v, r), "d": ui_extra.degree(v, r)} for r in rels]
        edges = [[idx[a], idx[b]] for a in rels for b in v.out[a] if b in idx and a != b]
        return {"nodes": nodes, "edges": edges, "total": len(v.body)}

    @router.get("/map", response_class=HTMLResponse)
    def map_page(request: Request):
        if not authed(request):
            return RedirectResponse("/login")
        return ui_extra.PAGE.replace("__BODY__", MAP_HTML).replace("__ACTIVE__", "map")

    app.include_router(router)


MAP_HTML = r"""
<style>
main{max-width:none!important;padding:0!important}
#wrap{position:relative;height:calc(100vh - 52px);height:calc(100dvh - 52px);min-height:420px;background:#080b16;overflow:hidden}
#cv{width:100%;height:100%;display:block;touch-action:none}
#bar{position:absolute;left:8px;right:8px;top:8px;display:flex;flex-direction:column;gap:6px;pointer-events:none}
#bar>*{pointer-events:auto}
#bar input{background:rgba(20,26,44,.85);color:#e6e9f0;border-color:#2b3350;backdrop-filter:blur(4px)}
.f{display:flex;flex-wrap:wrap;gap:4px}
.f span{font-size:13px;padding:4px 10px;border-radius:14px;border:1px solid #2b3350;background:rgba(20,26,44,.85);color:#c9cfdf;cursor:pointer;user-select:none}
.f span.off{opacity:.35}.f i{display:inline-block;width:9px;height:9px;border-radius:5px;margin-right:6px}
#info{position:absolute;left:8px;right:8px;bottom:calc(8px + env(safe-area-inset-bottom));background:rgba(20,26,44,.94);color:#e6e9f0;border:1px solid #2b3350;border-radius:12px;padding:10px 12px;display:none}
#info a{color:#8fb4ff;text-decoration:none;font-weight:600;margin-right:14px}
#st{position:absolute;right:10px;bottom:10px;color:#6b7590;font-size:12px;pointer-events:none}
</style>
<div id="wrap"><canvas id="cv"></canvas>
 <div id="bar"><input id="s" type="search" placeholder="Find a note..." autocomplete="off"><div class="f" id="flt"></div></div>
 <div id="info"></div><div id="st"></div></div>
<script>
const COL={entity:"#5b8def",summary:"#34d399",timeline:"#f5a524",conversation:"#8b93a7",raw:"#8b93a7","claude-memory":"#c084fc",note:"#f472b6",file:"#22d3ee",hub:"#a3e635"};
const col=t=>COL[t]||COL.note;
const cv=$("#cv"),ctx=cv.getContext("2d");
let N=[],E=[],adj=[],W=0,H=0,dpr=1,view={x:0,y:0,k:1},sel=-1,hit=new Set(),off=new Set(),alpha=1,running=false;
const rad=n=>2.2+Math.sqrt(n.d)*.8;
function resize(){const r=$("#wrap").getBoundingClientRect();dpr=window.devicePixelRatio||1;W=r.width;H=r.height;cv.width=W*dpr;cv.height=H*dpr;draw()}
function vis(i){return !off.has(N[i].ty)}
function initPos(){const R=Math.sqrt(N.length)*22,done=new Array(N.length).fill(false),q=[];let seed=0;
 const place=(i,x,y)=>{N[i].x=x;N[i].y=y;N[i].vx=N[i].vy=0;done[i]=true;q.push(i)};
 for(let s=0;s<N.length;s++){if(done[s])continue;const a=seed++*2.399963,r=R*Math.sqrt(Math.random());place(s,r*Math.cos(a),r*Math.sin(a));
  while(q.length){const c=q.shift();for(const j of adj[c])if(!done[j])place(j,N[c].x+(Math.random()-.5)*40,N[c].y+(Math.random()-.5)*40)}}}
function step(){
 const CELL=60,g=new Map();
 for(let i=0;i<N.length;i++){const n=N[i],key=Math.floor(n.x/CELL)+","+Math.floor(n.y/CELL);let a=g.get(key);if(!a)g.set(key,a=[]);a.push(i)}
 for(let i=0;i<N.length;i++){const n=N[i],cx=Math.floor(n.x/CELL),cy=Math.floor(n.y/CELL);
  for(let ox=-1;ox<=1;ox++)for(let oy=-1;oy<=1;oy++){const a=g.get((cx+ox)+","+(cy+oy));if(!a)continue;
   for(const j of a){if(j<=i)continue;const m=N[j];let dx=n.x-m.x,dy=n.y-m.y,d2=dx*dx+dy*dy;if(d2>CELL*CELL*4)continue;if(d2<.01){dx=Math.random()-.5;dy=Math.random()-.5;d2=.5}
    const f=(120/d2)*alpha,d=Math.sqrt(d2);n.vx+=f*dx/d*6;n.vy+=f*dy/d*6;m.vx-=f*dx/d*6;m.vy-=f*dy/d*6}}}
 for(const[a,b]of E){const p=N[a],q=N[b];let dx=q.x-p.x,dy=q.y-p.y,d=Math.sqrt(dx*dx+dy*dy)+.01,f=(d-18)*.05*alpha;
  p.vx+=f*dx/d;p.vy+=f*dy/d;q.vx-=f*dx/d;q.vy-=f*dy/d}
 for(const n of N){n.vx-=n.x*.0006*alpha;n.vy-=n.y*.0006*alpha;const sp=Math.hypot(n.vx,n.vy);if(sp>14){n.vx*=14/sp;n.vy*=14/sp}n.x+=n.vx;n.y+=n.vy;n.vx*=.6;n.vy*=.6}
}
function loop(){if(alpha<.015){running=false;if(!touched)fit();draw();return}
 const t=performance.now();do{step();alpha*=.985}while(performance.now()-t<6&&alpha>=.015);draw();requestAnimationFrame(loop)}
function kick(){alpha=Math.max(alpha,.5);if(!running){running=true;requestAnimationFrame(loop)}}
function w2s(x,y){return[(x-view.x)*view.k+W/2,(y-view.y)*view.k+H/2]}
function draw(){ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);
 const focus=sel>=0||hit.size>0,lit=new Set();if(sel>=0){lit.add(sel);adj[sel].forEach(j=>lit.add(j))}hit.forEach(j=>lit.add(j));
 ctx.lineWidth=1;
 for(const[a,b]of E){if(!vis(a)||!vis(b))continue;const on=focus&&lit.has(a)&&lit.has(b)&&(sel<0||a===sel||b===sel);
  ctx.strokeStyle=on?"rgba(150,180,255,.85)":(focus?"rgba(120,130,160,.07)":"rgba(120,130,160,.28)");
  const[x1,y1]=w2s(N[a].x,N[a].y),[x2,y2]=w2s(N[b].x,N[b].y);ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.stroke()}
 const labels=[];
 for(let i=0;i<N.length;i++){if(!vis(i))continue;const n=N[i],[x,y]=w2s(n.x,n.y);if(x<-20||y<-20||x>W+20||y>H+20)continue;
  const r=rad(n)*Math.min(Math.max(view.k,.6),1.8);ctx.globalAlpha=focus&&!lit.has(i)?.15:1;ctx.fillStyle=col(n.ty);
  ctx.beginPath();ctx.arc(x,y,r,0,6.2832);ctx.fill();
  if(i===sel){ctx.strokeStyle="#fff";ctx.lineWidth=2;ctx.stroke();ctx.lineWidth=1}
  if(focus?lit.has(i):(n.d>=8&&view.k>.5)||(n.d>=3&&view.k>1.6)||view.k>3.2)labels.push([n.t,x,y-r-3,i===sel,i===sel?1e9:n.d])}
 ctx.globalAlpha=1;ctx.font="11px system-ui";ctx.textAlign="center";
 labels.sort((a,b)=>b[4]-a[4]);const boxes=[];
 for(const[t0,x,y,s]of labels){const t=t0.length>26?t0.slice(0,25)+"…":t0,w=ctx.measureText(t).width/2+3;
  if(boxes.length>=80||boxes.some(b=>Math.abs(b[0]-x)<w+b[2]&&Math.abs(b[1]-y)<13))continue;boxes.push([x,y,w]);
  ctx.fillStyle=s?"#fff":"rgba(220,226,240,.85)";ctx.fillText(t,x,y)}
 $("#st").textContent=N.filter((_,i)=>vis(i)).length+" notes · "+E.length+" links"}
let touched=false;
function fit(){if(!N.length)return;const xs=N.map(n=>n.x).sort((a,b)=>a-b),ys=N.map(n=>n.y).sort((a,b)=>a-b),lo=Math.floor(N.length*.03),hi=Math.ceil(N.length*.97)-1;
 const a=xs[lo],c=xs[hi],b=ys[lo],d=ys[hi];view.x=(a+c)/2;view.y=(b+d)/2;view.k=Math.min(W/(c-a+60),H/(d-b+60),3)}
function pick(sx,sy){let best=-1,bd=1e9;for(let i=0;i<N.length;i++){if(!vis(i))continue;const[x,y]=w2s(N[i].x,N[i].y),d=Math.hypot(x-sx,y-sy),r=rad(N[i])*Math.min(Math.max(view.k,.6),1.8)+10;if(d<r&&d<bd){bd=d;best=i}}return best}
function select(i){sel=i;const p=$("#info");if(i<0){p.style.display="none";$("#st").style.display="";draw();return}
 const n=N[i];p.style.display="block";$("#st").style.display="none";p.replaceChildren(h("div",{},h("b",{},n.t),h("span",{class:"badge",style:"background:"+col(n.ty)},n.ty)),
  h("div",{style:"color:#8b93a7;font-size:13px;margin:2px 0 8px"},n.p+" · "+adj[i].length+" linked notes"),
  h("a",{href:"/graph#"+encodeURIComponent(n.p)},"Open note ›"),h("a",{href:"#",onclick:e=>{e.preventDefault();view.x=n.x;view.y=n.y;view.k=Math.max(view.k,2.2);draw()}},"Zoom here"),
  h("a",{href:"#",onclick:e=>{e.preventDefault();select(-1)}},"Close"));draw()}
const ptr=new Map();let last=null,moved=0,pinch=0;
cv.addEventListener("pointerdown",e=>{touched=true;cv.setPointerCapture(e.pointerId);ptr.set(e.pointerId,[e.offsetX,e.offsetY]);moved=0;if(ptr.size===2){const[a,b]=[...ptr.values()];pinch=Math.hypot(a[0]-b[0],a[1]-b[1])}});
cv.addEventListener("pointermove",e=>{if(!ptr.has(e.pointerId))return;const o=ptr.get(e.pointerId),nx=e.offsetX,ny=e.offsetY;
 if(ptr.size===1){view.x-=(nx-o[0])/view.k;view.y-=(ny-o[1])/view.k;moved+=Math.abs(nx-o[0])+Math.abs(ny-o[1])}
 ptr.set(e.pointerId,[nx,ny]);
 if(ptr.size===2){const[a,b]=[...ptr.values()],d=Math.hypot(a[0]-b[0],a[1]-b[1]);if(pinch){view.k=Math.min(8,Math.max(.1,view.k*d/pinch))}pinch=d;moved+=10}
 draw()});
const up=e=>{if(ptr.size===1&&moved<8){const i=pick(e.offsetX,e.offsetY);select(i)}ptr.delete(e.pointerId);pinch=0};
cv.addEventListener("pointerup",up);cv.addEventListener("pointercancel",e=>{ptr.delete(e.pointerId);pinch=0});
cv.addEventListener("wheel",e=>{touched=true;e.preventDefault();view.k=Math.min(8,Math.max(.1,view.k*(e.deltaY<0?1.15:1/1.15)));draw()},{passive:false});
$("#s").addEventListener("input",e=>{const q=e.target.value.trim().toLowerCase();hit=new Set();
 if(q)N.forEach((n,i)=>{if(vis(i)&&(n.t.toLowerCase().includes(q)||n.p.toLowerCase().includes(q)))hit.add(i)});
 if(hit.size&&hit.size<=40){let sx=0,sy=0;hit.forEach(i=>{sx+=N[i].x;sy+=N[i].y});view.x=sx/hit.size;view.y=sy/hit.size;view.k=Math.max(view.k,hit.size===1?2.5:1)}draw()});
window.addEventListener("resize",resize);
(async()=>{const d=await api("/api/graph/all");N=d.nodes;E=d.edges;adj=N.map(()=>[]);for(const[a,b]of E){adj[a].push(b);adj[b].push(a)}
 const types=[...new Set(N.map(n=>n.ty))].sort();
 $("#flt").replaceChildren(...types.map(t=>{const s=h("span",{onclick:()=>{off.has(t)?off.delete(t):off.add(t);s.classList.toggle("off");if(sel>=0&&!vis(sel))select(-1);draw()}},h("i",{style:"background:"+col(t)}),t);return s}));
 resize();initPos();
 alpha=1;for(let i=0;i<450;i++){step();alpha*=.993}   // rough layout first, so it does not open as a blob
 fit();draw();alpha=.25;kick()})();
</script>
"""
