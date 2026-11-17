import os, secrets, time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

import config
from scrub import scrub

CFG = config.load()
STATE = Path(CFG["paths"]["state_dir"])
VAULT = Path(CFG["paths"]["vault"])
INBOX = STATE / "inbox"
UI_USER = os.environ["UI_USER"]
UI_PASSWORD = os.environ["UI_PASSWORD"]
ALLOWED = {".zip", ".json"}
SESSION_SECONDS = 30 * 24 * 3600
SESSIONS = {}

app = FastAPI()

def check(request: Request):
    tok = request.cookies.get("session")
    exp = SESSIONS.get(tok)
    if not exp or exp < time.time():
        raise HTTPException(401, "login required")

def log(msg):
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "activity.log").open("a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")

@app.post("/api/login")
def login(response: Response, username: str = Form(...), password: str = Form(...)):
    import accounts
    ok_user, ok_pass = accounts.verify(username, password, UI_USER, UI_PASSWORD)
    if not (ok_user and ok_pass):
        time.sleep(1)
        log("failed login attempt")
        raise HTTPException(401, "bad login")
    tok = secrets.token_urlsafe(32)
    SESSIONS[tok] = time.time() + SESSION_SECONDS
    response.set_cookie("session", tok, max_age=SESSION_SECONDS,
                        httponly=True, samesite="strict")
    log("login ok")
    return {"ok": True}

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,interactive-widget=resizes-content">
<title>Memory vault login</title>
<style>
body{margin:0;font-family:system-ui;background:#111;color:#eee;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:16px;box-sizing:border-box}
form{background:#1b1b1b;border-radius:14px;padding:20px;width:100%;max-width:340px}
label{display:block;margin:12px 0 4px;font-size:14px;color:#bbb}
input[type=text],input[type=password]{width:100%;box-sizing:border-box;background:#222;color:#eee;border:1px solid #444;border-radius:10px;padding:12px;font-size:16px}
button{background:#6d3fe0;color:#fff;border:0;border-radius:10px;padding:14px;font-size:16px;width:100%;margin-top:16px}
.err{color:#f66;min-height:1.2em;margin:10px 0 0}
</style></head><body>
<form method="post" action="/login" autocomplete="on">
<h3 style="margin-top:0">Memory vault login</h3>
<input type="hidden" name="next" value="__NEXT__">
<label for="username">Username</label>
<input type="text" id="username" name="username" autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false" required>
<label for="password">Password</label>
<input type="password" id="password" name="password" autocomplete="current-password" required>
<p class="err">__ERR__</p>
<button type="submit">Log in</button>
</form></body></html>"""


def _next(n):
    """Where to go after logging in: a path on this site, never another address."""
    n = str(n or "")
    return n if n.startswith("/") and not n.startswith("//") and "\\" not in n and "\n" not in n and "\r" not in n else "/"


def _login_page(next_url, err="", code=200):
    import html
    return HTMLResponse(LOGIN_PAGE.replace("__NEXT__", html.escape(_next(next_url), quote=True)).replace("__ERR__", html.escape(err)), status_code=code)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if _authed(request):
        return RedirectResponse(_next(next), status_code=303)
    return _login_page(next)


@app.post("/login")
def login_form(username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    """The login page's form: an ordinary form post and redirect, which is what password managers look for."""
    import accounts
    ok_user, ok_pass = accounts.verify(username, password, UI_USER, UI_PASSWORD)
    if not (ok_user and ok_pass):
        time.sleep(1)
        log("failed login attempt")
        return _login_page(next, "Wrong username or password", 401)
    tok = secrets.token_urlsafe(32)
    SESSIONS[tok] = time.time() + SESSION_SECONDS
    resp = RedirectResponse(_next(next), status_code=303)
    resp.set_cookie("session", tok, max_age=SESSION_SECONDS, httponly=True, samesite="strict")
    log("login ok")
    return resp

@app.get("/api/me")
def me(request: Request):
    check(request)
    return {"ok": True}

@app.post("/api/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    check(request)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED:
        raise HTTPException(400, "only .zip and .json files")
    INBOX.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}{suffix}"
    part = INBOX / (name + ".part")
    size, too_big = 0, False
    gb = __import__("tuning").get("import.max_upload_gb")   # set under Settings, Sweeper, Limits and guardrails
    max_bytes = gb * 1024 ** 3
    with part.open("wb") as out:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > max_bytes:
                too_big = True
                break
            out.write(chunk)
    if too_big:
        part.unlink(missing_ok=True)
        log(f"upload rejected: {file.filename} is over the {gb} GB import limit (Settings > Sweeper > Limits and guardrails > Largest file you can upload)")
        raise HTTPException(413, f"file too large: the limit is {gb} GB (Settings > Sweeper > Limits and guardrails)")
    part.rename(INBOX / name)
    log(f"upload saved {name} ({size} bytes)")
    return {"saved": name, "bytes": size}

@app.post("/api/note")
def note(request: Request, text: str = Form(...)):
    check(request)
    text = text.strip()
    if not text or len(text) > 20000:
        raise HTTPException(400, "empty or too long")
    clean, counts = scrub(text)
    now = datetime.now()
    folder = VAULT / "inbox"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{now:%Y-%m-%d-%H%M%S}-note.md"
    (folder / name).write_text(
        f"---\ntype: user-statement\nconfidence: high\ncreated: {now:%Y-%m-%d}\n---\n{clean}\n",
        encoding="utf-8")
    log(f"note saved {name} (redactions: {sum(counts.values())})")
    return {"saved": name}

@app.get("/api/activity")
def activity(request: Request):
    check(request)
    f = STATE / "activity.log"
    lines = f.read_text().splitlines()[-50:] if f.exists() else []
    pending = sorted(p.name for p in INBOX.glob("*")
                     if not p.name.endswith(".part")) if INBOX.exists() else []
    return {"log": lines[::-1], "pending": pending}

# Home: a dashboard. What each model is doing, what is waiting for you, upload, and the latest activity. The same
# sections on every screen: side by side across a wide window, one under the other on a phone.
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,interactive-widget=resizes-content">
<title>Memory vault</title>
__THEME_JS__
<style>
__THEME_CSS__
*{box-sizing:border-box}
body{margin:0;font:16px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--ink);padding:16px 16px 40px}
main{max-width:1500px;margin:0 auto}
h1{font-size:26px;margin:4px 0 16px}
h2{font-size:14px;margin:0 0 8px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
section{margin:0 0 26px;scroll-margin-top:68px;min-width:0}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
.tile{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;color:inherit;text-decoration:none;min-width:0;overflow-wrap:anywhere}
.tile b{font-size:17px}
.dot{display:inline-block;width:10px;height:10px;border-radius:5px;margin-right:6px}
.chip{font-size:12px;font-weight:600;padding:2px 10px;border-radius:10px;background:var(--line);color:var(--mut);margin-left:8px;white-space:nowrap}
.chip.on{background:var(--acc);color:#fff}
.mut{color:var(--mut);font-size:14px}
.now{margin-top:8px}
.big{font-size:22px;font-weight:700;margin-top:4px}
.cols{display:grid;gap:0 26px;grid-template-columns:minmax(0,1fr)}
@media (min-width:1000px){.cols{grid-template-columns:minmax(300px,1fr) minmax(0,2fr)}}
#drop{border:2px dashed var(--line);border-radius:12px;padding:20px;text-align:center}
#drop.over{border-color:var(--acc);background:var(--card)}
label.btn{display:inline-block;background:var(--acc);color:#fff;border-radius:10px;padding:10px 18px;margin:12px 0 4px;cursor:pointer}
#log{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;color:var(--mut);margin:0 0 8px}
.links a,a.more{color:var(--acc);text-decoration:none}
.links a{display:inline-block;margin-right:16px;padding:4px 0;white-space:nowrap}
a.more{font-size:14px}
</style></head><body>
__DRAWER__
<main>
<h1>Home</h1>
<section><h2>Models</h2><div class="grid" id="models"></div></section>
<section><h2>Waiting for you</h2><div class="grid">
<a class="tile" href="/ask"><b>Ask</b><div class="big" id="hq">...</div></a>
<a class="tile" href="/todo"><b>To-do</b><div class="big" id="ht">...</div></a>
<a class="tile" href="/queue"><b>Failed</b><div class="big" id="hf">...</div></a>
</div></section>
<div class="cols">
<section id="up"><h2>Upload</h2>
<div id="drop"><b>Add a chat export</b>
<div class="mut">Drop a .zip or .json file here, or choose one.</div>
<label class="btn" for="f">Choose file</label>
<input type="file" id="f" accept=".zip,.json" hidden>
<div id="upmsg" class="mut"></div><div id="pend" class="mut"></div></div></section>
<section><h2>Latest activity</h2><pre id="log"></pre><a class="more" href="/history">All activity in History</a></section>
</div>
<section><h2>Pages</h2><div class="links">
<a href="/queue">Queue</a><a href="/history">History</a><a href="/chat">Chat</a><a href="/map">Map</a><a href="/graph">List</a>
<a href="/ask">Ask</a><a href="/todo">To-do</a><a href="/think">Thinking</a><a href="/settings">Settings</a>
</div></section>
</main>
<script>
const $=i=>document.getElementById(i);
const NL=String.fromCharCode(10);
function showLogin(){location="/login?next="+encodeURIComponent(location.pathname+location.hash);}
async function api(url,opts){
 const r=await fetch(url,opts);
 if(r.status==401)showLogin();
 return r;}
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
// What each model is doing: the same words as the Queue page, which a tile opens
const M=[["sweeper","Sweeper","#4c9aff"],["reviewer","Reviewer","#f5a623"],["curator","Curator","#34d399"],["assistant","Assistant","#e879f9"]];
function dur(s){s=Math.max(0,Math.floor(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?h+"h "+m+"m":m?m+"m "+(s%60)+"s":s+"s"}
let Q=null;
function models(){
 if(!Q)return;
 $("models").replaceChildren(...M.map(function(a){
  const n=(Q.now||{})[a[0]]||{},x=(Q.next||{})[a[0]]||{},t=el("a","tile"),head=el("div"),dot=el("i","dot");
  t.href="/queue";dot.style.background=a[2];
  head.append(dot,el("b",null,a[1]),el("span","chip"+(n.state==="working"?" on":""),n.state||""));
  t.append(head);
  const calls=n.calls||[],now=el("div","now");
  if(!calls.length)now.append(el("div","mut",n.state==="idle"?"Nothing running right now.":"Not running."));
  calls.forEach(function(c){now.append(el("div",null,c.job+(c.what?": "+c.what:"")));
   if(c.since)now.append(el("div","mut","running for "+dur(Date.now()/1000-c.since)))});
  t.append(now,el("div","mut","Up next: "+(x.total!=null?x.total:(x.items||[]).length)));
  return t}))}
async function queue(){try{const r=await api("/api/queue");if(r.ok){Q=await r.json();models()}}catch(e){}}
async function counts(){
 for(const [u,i] of [["/api/questions/count","hq"],["/api/todo/count","ht"]]){
  try{const r=await fetch(u);if(r.ok){const n=(await r.json()).open;$(i).textContent=n?n+" open":"Nothing open"}}catch(e){}}
 try{const r=await fetch("/api/failed");if(r.ok){const n=((await r.json()).items||[]).length;$("hf").textContent=n?n+" waiting":"Nothing failed"}}catch(e){}}
async function activity(){
 try{const r=await api("/api/activity");
  if(!r.ok){$("log").textContent="Failed: "+r.status;return}
  const d=await r.json();
  $("log").textContent=d.log.slice(0,15).join(NL)||"Nothing yet.";
  $("pend").textContent=d.pending.length?"Waiting to be read: "+d.pending.join(", "):""}catch(e){}}
// Upload: choose a file, or drop one on the box
async function send(file){
 if(!file)return;
 const fd=new FormData();fd.append("file",file);
 $("upmsg").textContent="Uploading...";
 try{const r=await api("/api/upload",{method:"POST",body:fd});
  if(r.ok){$("upmsg").textContent="Saved: "+(await r.json()).saved}
  else{let why=String(r.status);try{why=(await r.json()).detail||why}catch(e){}$("upmsg").textContent="Failed: "+why}}
 catch(e){$("upmsg").textContent="Failed: the upload did not reach the server"}
 $("f").value="";activity()}
$("f").onchange=function(){send($("f").files[0])};
const drop=$("drop");
["dragenter","dragover"].forEach(function(n){drop.addEventListener(n,function(e){e.preventDefault();drop.classList.add("over")})});
["dragleave","drop"].forEach(function(n){drop.addEventListener(n,function(e){e.preventDefault();drop.classList.remove("over")})});
drop.addEventListener("drop",function(e){if(e.dataTransfer&&e.dataTransfer.files.length)send(e.dataTransfer.files[0])});
queue();counts();activity();
setInterval(function(){if(!document.hidden)queue()},5000);
setInterval(function(){if(!document.hidden)models()},2000);
setInterval(function(){if(!document.hidden){counts();activity()}},15000);
api("/api/me");
</script></body></html>"""

import ui_nav
PAGE = PAGE.replace("__DRAWER__", ui_nav.DRAWER).replace("__THEME_JS__", ui_nav.THEME_JS).replace("__THEME_CSS__", ui_nav.THEME_CSS)

import ui_extra

def _authed(request: Request):
    try:
        check(request)
        return True
    except HTTPException:
        return False

ui_extra.register(app, _authed)

@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE
