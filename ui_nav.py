"""Shared slide-out menu used by every page. DRAWER is plain HTML+CSS+JS and has no backslashes,
so it can be dropped into any page template."""

DRAWER = r"""
<style>
body{padding-top:52px}
#dbar{position:fixed;top:0;left:0;right:0;height:52px;display:flex;align-items:center;gap:6px;padding:0 12px;background:var(--head,var(--card,#1b2029));color:var(--headink,var(--ink,#e6e9f0));border-bottom:1px solid var(--line,#2b3240);z-index:30}
#dtheme{margin:0 0 0 auto;padding:4px 6px;font-size:13px;border-radius:8px;border:1px solid currentColor;background:transparent;color:inherit;width:auto}
#dtheme option{color:#111;background:#fff}
#dbar b{font-size:17px;white-space:nowrap;flex:none}
#dbtn{position:relative;display:block;width:auto;flex:none;background:none;border:0;color:inherit;padding:8px;margin:0;line-height:0;cursor:pointer}
#dbtn svg{background:none;border-radius:0}
#dscrim{position:fixed;top:52px;right:0;bottom:0;left:0;background:rgba(0,0,0,.55);z-index:34;display:none}
#dscrim.open{display:block}
#dwr{position:fixed;top:52px;bottom:0;left:0;width:min(80vw,300px);background:var(--card,#1b2029);color:var(--ink,#e6e9f0);border-right:1px solid var(--line,#2b3240);z-index:35;transform:translateX(-102%);transition:transform .2s ease;padding:12px 8px;overflow-y:auto}
#dwr.open{transform:none}
#dwr a{display:flex;align-items:center;justify-content:space-between;padding:14px 12px;border-radius:24px;color:inherit;text-decoration:none;font-weight:600;font-size:17px}
#dwr a.on{background:var(--acc,#6d9cff);color:#fff}
#dwr hr{border:0;border-top:1px solid var(--line,#2b3240);margin:8px 4px}
.dn{background:#fb923c;color:#fff;border-radius:10px;padding:0 8px;font-size:13px;font-weight:600}
.dn[hidden]{display:none}
#dwr .dn{background:none;color:inherit;font-size:15px;padding:0}
</style>
<div id="dbar">
 <b>Memory vault</b>
 <button id="dbtn" aria-label="Menu">
  <svg width="26" height="26" viewBox="0 0 24 24"><path d="M3 6h18M3 12h18M3 18h18" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg>
  <span id="dh" class="dn" hidden style="position:absolute;top:0;right:-2px;font-size:11px;padding:0 5px"></span>
 </button>
 <select id="dtheme" aria-label="Colour theme" title="Colour theme (this device only)">
  <option value="system">System</option><option value="white">White</option><option value="blue">Blue</option><option value="purple">Purple</option><option value="candy">Candy</option>
 </select>
</div>
<div id="dscrim"></div>
<div id="dwr">
 <a href="/">Home</a>
 <a href="/#up">Upload</a>
 <a href="/queue">Queue</a>
 <a href="/history">History</a>
 <a href="/chat">Chat</a>
 <a href="/map">Map</a>
 <a href="/graph">List</a>
 <a href="/ask">Ask<span id="qn" class="dn" hidden></span></a>
 <a href="/todo">To-do<span id="tn" class="dn" hidden></span></a>
 <a href="/think">Thinking</a>
 <a href="/settings">Settings<span id="sn" class="dn" hidden></span></a>
</div>
<script>
(function(){
 var dr=document.getElementById("dwr"),sc=document.getElementById("dscrim");
 function set(o){dr.classList.toggle("open",o);sc.classList.toggle("open",o)}
 document.getElementById("dbtn").addEventListener("click",function(){set(!dr.classList.contains("open"))});
 sc.addEventListener("click",function(){set(false)});
 dr.addEventListener("click",function(e){if(e.target.closest("a"))set(false)});
 function mark(){var here=location.pathname+location.hash;
  dr.querySelectorAll("a").forEach(function(a){a.classList.toggle("on",a.getAttribute("href")===here)})}
 var ts=document.getElementById("dtheme");
 try{ts.value=localStorage.getItem("theme")||"system"}catch(e){}
 ts.addEventListener("change",function(){var v=ts.value;   // kept in this browser only
  try{localStorage.setItem("theme",v)}catch(e){}
  if(v==="system")delete document.documentElement.dataset.theme;else document.documentElement.dataset.theme=v});
 window.addEventListener("hashchange",mark);mark();
 function show(id,n){var b=document.getElementById(id);if(b){b.hidden=!n;b.textContent=n}}
 async function counts(){
  var q=0,t=0,u=0;
  try{var r=await fetch("/api/questions/count");if(r.ok)q=(await r.json()).open||0}catch(e){}
  try{var r2=await fetch("/api/todo/count");if(r2.ok)t=(await r2.json()).open||0}catch(e){}
  try{var r3=await fetch("/api/update/status");if(r3.ok)u=(await r3.json()).available?1:0}catch(e){}   // 1: an update is ready
  show("qn",q);show("tn",t);show("sn",u);show("dh",q+t+u)}
 window.updBadge=counts;counts();setInterval(counts,30000);
})();
</script>
"""

# The colour themes, shared by every page template: the choice (System, White, Blue, Purple, Candy) is kept in this browser.
THEME_JS = r'''<script>(function(){try{var t=localStorage.getItem("theme");if(t&&t!=="system")document.documentElement.dataset.theme=t}catch(e){}})()</script>'''
THEME_CSS = r''':root{--bg:#f6f7f9;--card:#fff;--ink:#1c2230;--mut:#677084;--line:#dfe3ea;--acc:#2f6fed;--warn:#c2410c;--head:var(--card);--headink:var(--ink)}
@media (prefers-color-scheme:dark){:root{--bg:#131313;--card:#2b2b2b;--ink:#eaeaea;--mut:#a8a8a8;--line:rgba(255,255,255,.14);--acc:#8508BF;--warn:#fb923c;--head:#691B54;--headink:#fff}}
:root[data-theme=white]{color-scheme:light;--bg:#f6f7f9;--card:#fff;--ink:#1c2230;--mut:#677084;--line:#dfe3ea;--acc:#2f6fed;--warn:#c2410c;--head:#fff;--headink:#1c2230}
:root[data-theme=blue]{color-scheme:dark;--bg:#12151c;--card:#1b2029;--ink:#e6e9f0;--mut:#98a2b5;--line:#2b3240;--acc:#6d9cff;--warn:#fb923c;--head:#1b2029;--headink:#e6e9f0}
:root[data-theme=purple]{color-scheme:dark;--bg:#131313;--card:#2b2b2b;--ink:#eaeaea;--mut:#a8a8a8;--line:rgba(255,255,255,.14);--acc:#8508BF;--warn:#fb923c;--head:#691B54;--headink:#fff}
:root[data-theme=candy]{color-scheme:light;--bg:#CAFBAA;--card:#AAFBDB;--ink:#363636;--mut:#424242;--line:rgba(0,0,0,.14);--acc:#3273dc;--warn:#b93a0b;--head:#DBAAFB;--headink:#fff}'''
