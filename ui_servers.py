"""Settings page, Servers: where models run. This server's CPU and GPU (the hardware switch and the host CPU speed) and the
list of external servers any model's card can pick. Each one has a number: how many models may work on it at the same
time (default 1). The API is in remote.py (the list), ui_hardware.py and ui_cpu.py (this server)."""

HTML = r"""
<style>
#srv input[type=radio]{width:20px;height:20px;padding:0;margin:0 8px 0 0;vertical-align:middle}
</style>
<div id="srv"></div>
<script>
(function(){
const GL={performance:"Performance (full speed)",powersave:"Power saver (lowest speed)",ondemand:"On demand (speeds up under load)",conservative:"Conservative (ramps up gently)",schedutil:"Scheduler-driven"};
const ghz=k=>(k/1e6).toFixed(2)+" GHz";
const root=h("div",{class:"card"},h("h3",{},"Servers"));
const notice=h("div",{});
const hostBox=h("div",{}),extBox=h("div",{});
let HW={backend:"cpu",gpus:[],vulkan:[],limits:{cpu:1,gpu:1},running:false};
async function postJ(path,body){const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});const j=await r.json().catch(function(){return{}});return{ok:r.ok,detail:j.detail||"",data:j}}
// A number of models at the same time: below 1 is treated as 1, said in a popup that lets you carry on.
async function limitOk(v){const n=Number(v);if(Number.isFinite(n)&&n>=1&&Number.isInteger(n))return true;
 return await window.warnPop("Models at the same time",["Less than 1, or not a whole number: it is used as 1 (they take turns)."],"Save anyway","Cancel")}
// ---------- this server ----------
const cpuLimit=h("input",{type:"number",min:"1",style:"width:90px"}),gpuLimit=h("input",{type:"number",min:"1",style:"width:90px"});
const hwMsg=h("div",{class:"mut"}),gpuInfo=h("div",{class:"mut"}),gpuBtn=h("button",{class:"pri"},""),gpuLog=h("pre",{style:"white-space:pre-wrap;word-break:break-word;font:12px/1.4 ui-monospace,Menlo,Consolas,monospace;max-height:40vh;overflow:auto;display:none"});
let gpuTarget="";
const saveLimits=h("button",{class:"pri",onclick:async function(){
 if(!await limitOk(cpuLimit.value))return;if(HW.backend==="vulkan"&&!await limitOk(gpuLimit.value))return;
 let r=await postJ("/api/hardware/limit",{key:"cpu",n:cpuLimit.value});
 if(r.ok&&HW.backend==="vulkan")r=await postJ("/api/hardware/limit",{key:"gpu",n:gpuLimit.value});
 hwMsg.textContent=r.ok?"Saved.":r.detail;if(!r.ok)window.warnPop("Could not save",[r.detail])}},"Save");
function drawHost(){
 const gpu=HW.backend==="vulkan";gpuTarget=gpu?"cpu":"vulkan";
 cpuLimit.value=HW.limits.cpu;gpuLimit.value=HW.limits.gpu;
 gpuInfo.textContent=HW.gpus.length?"GPU in this container: "+(HW.vulkan.length?HW.vulkan.join(", "):HW.gpus.join(", ")):
  "No GPU in this container. To add one, run on the Proxmox host: install.sh --add-gpu <this container's ID>. It passes the GPU in; then set it up here.";
 gpuBtn.textContent=gpu?"Remove the GPU set-up (CPU only)":"Set up the GPU (Vulkan)";gpuBtn.style.display=gpu||HW.gpus.length?"":"none";
 hostBox.replaceChildren(
  h("div",{style:"font-weight:600;margin-top:4px"},"This server"),
  h("div",{class:"mut"},"Models set to run on this server's CPU or GPU work on that hardware. Each has its own number of models that may work at the same time (1 means they take turns)."),
  h("div",{class:"fld"},h("label",{},"CPU: models at the same time"),cpuLimit),
  ...(gpu?[h("div",{class:"fld"},h("label",{},"GPU: models at the same time"),gpuLimit)]:[]),
  saveLimits,hwMsg,
  h("div",{style:"margin-top:12px"},gpuInfo,gpuBtn,gpuLog),
  cpuSpeed)}
async function loadHw(){try{HW=await api("/api/hardware");drawHost();if(HW.running)followHw()}catch(e){}}
async function followHw(){gpuLog.style.display="";hwMsg.textContent="Switching. Building llama.cpp takes a while; the model servers restart at the end.";
 while(true){await new Promise(function(r){setTimeout(r,3000)});
  try{const p=await api("/api/hardware/progress");gpuLog.textContent=p.log.join("\n");gpuLog.scrollTop=gpuLog.scrollHeight;
   if(!p.running){const t=p.log.join("\n");hwMsg.textContent=/^done:/m.test(t)?"Done.":"It did not go through; nothing was changed. Its output is below.";await loadHw();window.refreshServers&&window.refreshServers();return}}catch(e){}}}
gpuBtn.onclick=async function(){const gpu=gpuTarget==="vulkan";
 if(!await window.warnPop(gpu?"Set up the GPU?":"Remove the GPU set-up?",
  ["llama.cpp is rebuilt for "+(gpu?"Vulkan":"the CPU")+" (this takes a while), then the model servers here restart: a call running on them is cut off and done again.",
   gpu?"After that each model can be set to the CPU or the GPU on its own card. A model already set up keeps running on the GPU unless you change it.":"Every model then runs on the CPU, using this container's memory and cores.",
   "If the build fails, nothing is changed."],"Go on","Not now"))return;
 const r=await postJ("/api/hardware/switch",{to:gpuTarget});if(!r.ok){hwMsg.textContent=r.detail||"Could not start it";return}followHw()};
// CPU speed of the host (through the helper on the Proxmox host), collapsed
const cs={rs:h("input",{type:"radio",name:"cpumode"}),rf:h("input",{type:"radio",name:"cpumode"}),sg:h("select",{}),sf:h("select",{}),go:h("button",{class:"pri"},"Apply CPU setting"),st:h("div",{class:"mut"}),msg:h("div",{class:"mut"})};
let built=false;
function csSync(){cs.sg.disabled=!cs.rs.checked;cs.sf.disabled=!cs.rf.checked}
cs.rs.onchange=csSync;cs.rf.onchange=csSync;
cs.go.onclick=async function(){cs.msg.textContent="";const mode=cs.rf.checked?"speed":"state";
 const r=await postJ("/api/cpu/set",{mode:mode,value:mode==="speed"?Number(cs.sf.value):cs.sg.value});
 cs.msg.textContent=r.ok?"Applied.":(r.detail||"Could not apply");loadCpu()};
async function loadCpu(){const d=await api("/api/cpu");
 if(!d.ok){built=false;cs.st.textContent=d.why||"Not available";[cs.rs,cs.rf,cs.sg,cs.sf,cs.go].forEach(function(e){e.disabled=true});return}
 cs.st.textContent="Now: "+(GL[d.gov]||d.gov)+", average "+ghz(d.avg_khz)+" across "+d.cores+" cores, allowed "+ghz(d.min_khz)+" to "+ghz(d.max_khz);
 if(built)return;built=true;cs.rs.disabled=false;cs.rf.disabled=false;cs.go.disabled=false;
 cs.sg.replaceChildren(...d.governors.map(function(g){return h("option",{value:g},GL[g]||g)}));
 const n=d.freqs.length,turbo=n>1&&d.freqs[n-1]-d.freqs[n-2]<20000;
 cs.sf.replaceChildren(...d.freqs.map(function(k,i){return h("option",{value:k},ghz(k)+(turbo&&i===n-1?" + Turbo":turbo&&i===n-2?" (no Turbo)":""))}));
 cs.sg.value=d.gov;if(d.mode==="speed")cs.sf.value=d.max_khz;cs.rs.checked=d.mode==="state";cs.rf.checked=d.mode==="speed";csSync()}
const cpuSpeed=h("details",{class:"sec"},h("summary",{},"CPU speed (host)"),cs.st,
 h("div",{class:"fld"},h("label",{},cs.rs,"Power state"),cs.sg),h("div",{class:"fld"},h("label",{},cs.rf,"Fixed speed"),cs.sf),cs.go,cs.msg);
// ---------- external servers ----------
let SERVERS=[];
function field(l,el){return h("div",{class:"fld"},h("label",{},l),el)}
function serverForm(p,isNew){
 const name=h("input",{type:"text",maxlength:"60",placeholder:"a name, e.g. Gaming PC"});name.value=p.name||"";
 const url=h("input",{type:"text",placeholder:"http://192.168.1.50:8080"});url.value=p.url||"";
 const api_=h("select",{},h("option",{value:"llamacpp"},"llama.cpp server"),h("option",{value:"openai"},"Other OpenAI-compatible"));api_.value=p.api||"llamacpp";
 const key=h("input",{type:"password",autocomplete:"off",placeholder:p.key_set?"saved, type to replace":"optional"});
 const kc=h("input",{type:"checkbox"}),ack=h("input",{type:"checkbox"});ack.checked=!!p.ack;
 const run=h("input",{type:"text",placeholder:"http://192.168.1.50:8090 (optional)"});run.value=p.runner||"";
 const rk=h("input",{type:"password",autocomplete:"off",placeholder:p.runner_key_set?"saved, type to replace":"printed when you set up the runner"});
 const rc=h("input",{type:"checkbox"});
 const lim=h("input",{type:"number",min:"1",style:"width:90px"});lim.value=p.limit||1;
 const msg=h("div",{class:"mut"});
 const test=h("button",{onclick:async function(){msg.textContent="Testing...";
  const r=await postJ("/api/remote/test",{id:p.id||"",url:url.value,api:api_.value,key:key.value});
  msg.textContent=r.ok?"Server says: "+r.data.state+(r.data.models&&r.data.models.length?". Models it lists: "+r.data.models.join(", "):""):(r.detail||"Could not test")}},"Test connection");
 const save=h("button",{class:"pri",onclick:async function(){
  if(!await limitOk(lim.value))return;
  const r=await postJ("/api/servers/save",{id:p.id||"",name:name.value,url:url.value,api:api_.value,key:key.value,clear_key:kc.checked,ack:ack.checked,
   runner:run.value,runner_key:rk.value,runner_clear:rc.checked,limit:lim.value});
  if(!r.ok){msg.textContent=r.detail;window.warnPop("Could not save the server",[r.detail]);return}
  msg.textContent="Saved. Models that use it were updated.";await loadServers();window.refreshServers&&window.refreshServers()}},isNew?"Add":"Save");
 const del=h("button",{onclick:async function(){
  if(!await window.warnPop("Remove the server "+p.name+"?",["Models that use it keep their settings and keys, but no longer follow this list."],"Remove","Keep it"))return;
  const r=await postJ("/api/servers/delete",{id:p.id});if(!r.ok){msg.textContent=r.detail;return}
  await loadServers();window.refreshServers&&window.refreshServers()}},"Remove");
 const d=h("details",{class:"sec"},h("summary",{},isNew?"New server":p.name+" · "+p.url+" · "+p.limit+" at a time"),
  field("Name",name),field("Address",url),field("Type",api_),field("API key",key),field("Clear the saved key",kc),
  field("Server is outside my network: send my notes to it",ack),
  h("div",{class:"mut",style:"margin-top:10px"},"Model runner (optional): run model_runner.py on that machine, and each model's card sets its model, context, slots and flags (see the README)."),
  field("Runner address",run),field("Runner token",rk),field("Clear the saved token",rc),
  field("Models that may work on it at the same time",lim),
  test,save,isNew?null:del,msg);
 d.open=isNew;return d}
async function loadServers(){try{const d=await api("/api/servers");SERVERS=d.servers;
 const add=h("button",{class:"pri",style:"margin-top:8px",onclick:function(){add.before(serverForm({},true))}},"Add server");
 notice.replaceChildren(...(d.notice&&d.notice.length?[h("div",{class:"mut",style:"border-left:3px solid var(--acc);padding-left:10px;margin:8px 0"},
  h("div",{style:"font-weight:600"},"The old presets were moved into this list:"),...d.notice.map(function(n){return h("div",{},n)}),
  h("button",{onclick:async function(){await postJ("/api/servers/notice/dismiss",{});loadServers()}},"OK"))]:[]));
 extBox.replaceChildren(h("div",{style:"font-weight:600;margin-top:16px"},"External servers"),
  h("div",{class:"mut"},"Another machine that runs a model. Pick it in Where it runs on a model's card. Add the machine here once, whichever models use it."),
  ...SERVERS.map(function(p){return serverForm(p,false)}),
  add)}catch(e){}}
root.append(notice,hostBox,extBox);$("#srv").replaceChildren(root);
loadHw();loadCpu();loadServers();setInterval(function(){if(!document.hidden)loadCpu().catch(function(){})},5000);
})();
</script>
"""
