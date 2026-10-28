import os, secrets, time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse

import config
from scrub import scrub

CFG = config.load()
STATE = Path(CFG["paths"]["state_dir"])
VAULT = Path(CFG["paths"]["vault"])
INBOX = STATE / "inbox"
UI_USER = os.environ["UI_USER"]
UI_PASSWORD = os.environ["UI_PASSWORD"]
MAX_BYTES = int(CFG.get("upload", {}).get("max_mb", 200)) * 1024 * 1024
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
    with part.open("wb") as out:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_BYTES:
                too_big = True
                break
            out.write(chunk)
    if too_big:
        part.unlink(missing_ok=True)
        raise HTTPException(413, "file too large")
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

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Memory Agent</title>
<style>
body{margin:0;font-family:system-ui;background:#111;color:#eee;padding:16px 16px 80px}
button,label.btn{background:#6d3fe0;color:#fff;border:0;border-radius:10px;padding:14px;
 font-size:16px;display:block;text-align:center;width:100%;box-sizing:border-box}
textarea,input[type=password],input[type=text]{width:100%;box-sizing:border-box;background:#222;
 color:#eee;border:1px solid #444;border-radius:8px;padding:10px;font-size:16px;margin-bottom:10px}
nav{position:fixed;bottom:0;left:0;right:0;display:flex;background:#1b1b1b}
nav button{border-radius:0;background:#1b1b1b;flex:1}nav button.on{background:#6d3fe0}
.tab{display:none}.tab.on{display:block}
.idx a{display:block;background:#1b1b1b;border:1px solid #333;border-radius:12px;padding:14px;margin-bottom:10px;color:inherit;text-decoration:none}
.idx b{display:block;font-size:17px}.idx span{color:#aaa;font-size:14px}
pre{white-space:pre-wrap;font-size:13px;color:#bbb}
#login{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.88);
 display:none;align-items:center;justify-content:center;z-index:50;padding:16px}
#login form{background:#1b1b1b;border-radius:14px;padding:20px;width:100%;max-width:340px}
#lmsg{color:#f66;min-height:1.2em}
</style></head><body>
<div id="login"><form id="lf" autocomplete="on">
<h3>Memory Agent login</h3>
<input type="text" id="u" name="username" placeholder="Username" autocomplete="username">
<input type="password" id="p" name="password" placeholder="Password" autocomplete="current-password">
<p id="lmsg"></p>
<button type="submit">Log in</button>
</form></div>
<div id="hm" class="tab on"><h3>Memory vault</h3>
<div class="idx">
<a href="#up"><b>Upload</b><span>Add a chat export</span></a>
<a href="/queue"><b>Queue</b><span>What each model is doing and what is next</span></a>
<a href="/history"><b>History</b><span>Everything the models have done</span></a>
<a href="/chat"><b>Chat</b><span>Ask about the lab or correct a note</span></a>
<a href="/map"><b>Map</b><span>Every note as a dot</span></a>
<a href="/graph"><b>List</b><span>Browse and search notes</span></a>
<a href="/ask"><b>Ask</b><span id="hq">Questions for you</span></a>
<a href="/todo"><b>To-do</b><span id="ht">Unfinished tasks</span></a>
<a href="/think"><b>Thinking</b><span>Live model thinking and speed</span></a>
</div></div>
<div id="up" class="tab"><h3>Upload export</h3>
<label class="btn" for="f">Choose file</label>
<input type="file" id="f" accept=".zip,.json" hidden>
<p id="upmsg"></p></div>
<div id="ac" class="tab"><h3>Activity</h3><pre id="log"></pre></div>
__DRAWER__
<script>
const $=i=>document.getElementById(i);
function showLogin(){$("login").style.display="flex";}
async function api(url,opts){
 const r=await fetch(url,opts);
 if(r.status==401)showLogin();
 return r;}
$("lf").onsubmit=async e=>{
 e.preventDefault();
 const fd=new FormData();
 fd.append("username",$("u").value);fd.append("password",$("p").value);
 const r=await fetch("/api/login",{method:"POST",body:fd});
 if(r.ok){$("login").style.display="none";$("p").value="";$("lmsg").textContent="";}
 else{$("lmsg").textContent="Wrong username or password";}};
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{
 document.querySelectorAll(".tab,nav button").forEach(e=>e.classList.remove("on"));
 b.classList.add("on");$(b.dataset.t).classList.add("on");
 if(b.dataset.t=="ac")load();});
$("f").onchange=async()=>{
 const fd=new FormData();fd.append("file",$("f").files[0]);
 $("upmsg").textContent="Uploading...";
 const r=await api("/api/upload",{method:"POST",body:fd});
 $("upmsg").textContent=r.ok?"Saved: "+(await r.json()).saved:"Failed: "+r.status;};
async function load(){
 const r=await api("/api/activity");
 if(!r.ok){$("log").textContent="Failed: "+r.status;return;}
 const d=await r.json();
 $("log").textContent="Pending: "+(d.pending.join(", ")||"none")+"\\n\\n"+d.log.join("\\n");}
function tab(id){if(!["hm","up","ac"].includes(id))id="hm";
 document.querySelectorAll(".tab").forEach(e=>e.classList.remove("on"));
 $(id).classList.add("on");if(id=="ac")load();}
addEventListener("hashchange",()=>tab(location.hash.slice(1)));
tab(location.hash.slice(1));
async function idx(){
 for(const [u,i] of [["/api/questions/count","hq"],["/api/todo/count","ht"]]){
  try{const r=await fetch(u);if(r.ok){const n=(await r.json()).open;$(i).textContent=n?n+" open":"Nothing open"}}catch(e){}}}
idx();
api("/api/me");
</script></body></html>"""

import ui_nav
PAGE = PAGE.replace("__DRAWER__", ui_nav.DRAWER)

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
