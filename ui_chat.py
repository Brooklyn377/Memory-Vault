"""Chat window. Messages are JSON files in <state>/chat/. The web page only queues and reads them;
`ui_chat.py --watch` (one slow worker) answers them.
  plain message   -> vault-only look-up: search note names and text, add linked notes, answer from those only.
                     The Assistant answers when its switch is on and its server answers; otherwise the Reviewer.
  /fix <text>     -> the Reviewer restates what it understood; only your "Yes" files it to the inbox
A failed Assistant answer is not retried by itself: the message is marked failed, an Ask card says why, and
"Send again" on the Chat page queues it again.
"""
import json
import re
import secrets
import sys
import time
import urllib.request
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import agent
import settings
import ui_extra
from graph import Vault, as_list
from scrub import scrub

STATE, VAULT = agent.STATE, agent.VAULT
CHAT = STATE / "chat"
MAX_TEXT = 4000
MAX_TRIES = 3
SKIP = ("raw/", "inbox/")
STOP = set("the and for are was were what when where which who how why does did this that with from have has "
           "about into your you our not can could would should there their them they then than been being "
           "tell show give lab homelab server".split())

LOOKUP_SYS = ("You answer one person's questions using ONLY the notes provided. "
              "If the notes do not cover the question, say the vault has nothing on it. "
              "After each fact, name the note it came from in square brackets, like [entities/foo.md]. "
              "Be short and plain. Never guess.")
FIX_SYS = ("The person is correcting something in their notes. Using the notes provided, restate in one or "
           "two plain sentences what you understood the correction to be and which note it affects. Then ask "
           "whether that is right. Do not say anything has been changed.")
ASSIST_SYS = (
    "You are the person's assistant for their notes vault and the files around it. Look things up with tools before "
    'answering. To use a tool, reply with ONLY JSON: {"tool":"<name>","args":{...}}. When you are ready, reply with '
    'ONLY JSON: {"answer":"<your reply>"}. Tools: '
    'search {"text":"<words>","where":"all|notes|raw|inbox|vault|state|code|logs|git|<Git server name>"} finds files; '
    'open {"path":"<path>","start":<line>} reads a file or lists a folder; '
    'links {"path":"<note path>"} lists linked notes; tasks {} lists open to-do tasks; '
    "questions {} lists open Ask cards; queue {} shows what the Sweeper, Reviewer and Curator are doing; "
    "repos {} lists the Git servers you can read and what each is for. "
    "Answer only from what the notes and tools show. After each fact, name its file in square brackets, like "
    "[vault/entities/foo.md]. If nothing covers it, say so. Be short and plain. Never guess, and never say you changed "
    "anything.")


# ---------- storage ----------
def _path(mid):
    return CHAT / f"{mid}.json"


def _save(m):
    CHAT.mkdir(parents=True, exist_ok=True)
    tmp = _path(m["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=1, ensure_ascii=False))
    tmp.replace(_path(m["id"]))


def _load(mid):
    p = _path(mid)
    return json.loads(p.read_text()) if p.exists() else None


def _all():
    if not CHAT.exists():
        return []
    return sorted((json.loads(p.read_text()) for p in CHAT.glob("*.json")), key=lambda m: m["id"])


def send(text):
    text = str(text).strip()
    kind = "correction" if re.match(r"(?i)/fix\b", text) else "lookup"
    if kind == "correction":
        text = re.sub(r"(?i)^/fix\s*", "", text)
    text = scrub(text)[0][:MAX_TEXT].strip()
    if not text:
        raise ValueError("empty message")
    m = {"id": f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}", "kind": kind, "text": text,
         "status": "queued", "reply": "", "sources": [], "confirm": "none", "tries": 0}
    _save(m)
    return m


def decide(mid, ok):
    m = _load(mid)
    if not m or m["kind"] != "correction" or m["confirm"] != "pending":
        raise ValueError("nothing to confirm")
    if ok:
        now = datetime.now()
        inbox = VAULT / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"{now:%Y-%m-%d-%H%M%S}-chat-{mid[-4:]}.md").write_text(
            f"---\ntype: user-statement\nconfidence: high\ncreated: {now:%Y-%m-%d}\nchat_id: {mid}\n---\n"
            f"Correction, confirmed by the person.\n\nWhat was understood: {scrub(m['reply'])[0]}\n\n"
            f"Their words: {m['text']}\n", encoding="utf-8")
        m["confirm"] = "filed"
    else:
        m["confirm"] = "discarded"
    _save(m)
    return m


# ---------- worker ----------
_vc = {"t": 0.0, "v": None}


def _vault():
    if _vc["v"] is None or time.time() - _vc["t"] > 120:
        _vc["v"], _vc["t"] = Vault(VAULT), time.time()
    return _vc["v"]


def retrieve(question):
    """-> (best matching notes, linked notes one hop out). Reads the vault only."""
    v = _vault()
    terms = [t for t in dict.fromkeys(re.findall(r"[a-z0-9][a-z0-9._-]{2,}", question.lower())) if t not in STOP][:8]
    scored = []
    for rel, body in v.body.items():
        if rel.startswith(SKIP):
            continue
        names = " ".join([rel.lower(), v.title(rel).lower()] + [a.lower() for a in as_list(v.meta[rel].get("aliases"))])
        low = body.lower()
        score = sum(3 for t in terms if t in names) + sum(min(low.count(t), 3) for t in terms)
        if score:
            scored.append((score, rel))
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = [r for _, r in scored[:__import__("tuning").get("chat.notes")]]
    extra = []
    for r in top[:__import__("tuning").get("chat.hop_notes")]:
        for n in sorted(v.out[r] | v.back[r]):
            if n not in top and n not in extra and not n.startswith(SKIP):
                extra.append(n)
    return top, extra[:__import__("tuning").get("chat.linked")]


def _ask(system, user):
    import reviewer
    body = {"model": reviewer.NAME, "temperature": settings.get("gpt")["temperature"], "max_tokens": settings.get("gpt")["max_tokens"]["chat"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "chat_template_kwargs": {"reasoning_effort": settings.get("gpt")["reasoning_effort"]}}
    import thinklog
    return (thinklog.chat("gpt", reviewer.ENDPOINT, body, "chat", slot=1).get("content") or "").strip()


def assistant_on():
    """True when the Assistant's switch is on and its server (local or external) answers."""
    try:
        import applier
        return bool(settings.get("pa").get("enabled")) and applier.health("pa") == "ready"
    except Exception:
        return False


def _ask_assistant(system, messages, label="chat"):
    import applier
    import thinklog
    import ui_settings
    s = settings.get("pa")
    body = {"model": s["model_file"] or "assistant", "temperature": s["temperature"], "max_tokens": s["max_tokens"]["chat"],
            "messages": [{"role": "system", "content": system}] + messages}
    caps = ui_settings.caps(ui_settings.capname("pa", s))   # the model decides which thinking switch applies
    if caps["thinking"]:
        body["chat_template_kwargs"] = {"enable_thinking": bool(s.get("thinking", True))}
    elif caps["reasoning_effort"]:
        body["chat_template_kwargs"] = {"reasoning_effort": s.get("reasoning_effort", "low")}
    return (thinklog.chat("pa", f"http://127.0.0.1:{applier.PORT['pa']}/v1", body, label).get("content") or "").strip()


def assistant_answer(m, context):
    """The tool loop: the Assistant looks things up until it answers, at most the look-up limit, then must answer.
    Returns (reply, files it opened)."""
    import assistant_tools
    budget = __import__("tuning").get("assistant.tool_calls")
    msgs = earlier_turns(m) + [{"role": "user", "content": f"{context}\n\nQUESTION: {m['text']}"}]
    opened, used = [], 0
    while True:
        last = used >= budget
        if last:
            msgs.append({"role": "user", "content": 'No more look-ups. Answer now with {"answer": "..."} from what you have.'})
        text = _ask_assistant(ASSIST_SYS, msgs, "chat" if not used else "assist")
        data = agent.parse_json(text) if text.lstrip().startswith(("{", "```")) else None
        if not isinstance(data, dict) or "tool" not in data or last:
            if isinstance(data, dict) and data.get("answer"):
                return str(data["answer"]).strip(), opened
            if isinstance(data, dict):   # JSON without an answer: nothing usable to show
                raise RuntimeError("the Assistant gave no answer")
            return text, opened          # plain text: the model answered without the JSON wrapper
        used += 1
        name, args = str(data.get("tool")), data.get("args") or {}
        if name == "open" and isinstance(args, dict) and args.get("path"):
            opened.append(str(args["path"]))
        result = assistant_tools.run(name, args)
        msgs += [{"role": "assistant", "content": text}, {"role": "user", "content": f"TOOL RESULT ({name}):\n{result}"}]


def earlier_turns(m):
    """Your earlier plain messages and their replies, oldest first, so the Assistant can follow up."""
    n = __import__("tuning").get("assistant.history")
    done = [x for x in _all() if x["id"] < m["id"] and x["kind"] == "lookup" and x["status"] == "replied" and x["reply"]]
    out = []
    for x in done[-n:] if n else []:
        out += [{"role": "user", "content": x["text"]}, {"role": "assistant", "content": x["reply"]}]
    return out


def handle(m, who="gpt"):
    v = _vault()
    top, extra = retrieve(m["text"])
    if not top and m["kind"] == "lookup" and who == "gpt":
        m["reply"], m["sources"] = "The vault has no notes that match that.", []
        return
    parts = [f"NOTE {r}:\n{v.body[r][:__import__('tuning').get('chat.note_chars')]}" for r in top]
    parts += [f"LINKED NOTE {r} (start only):\n{v.body[r][:__import__('tuning').get('chat.linked_chars')]}" for r in extra]
    context = "\n\n".join(parts) or "(no matching notes found)"
    if who == "pa":
        reply, opened = assistant_answer(m, context)
        extra = extra + [p for p in dict.fromkeys(opened) if p not in top + extra]
    else:
        system = LOOKUP_SYS if m["kind"] == "lookup" else FIX_SYS
        kind = "QUESTION" if m["kind"] == "lookup" else "CORRECTION"
        reply = _ask(system, f"{context}\n\n{kind}: {m['text']}")
    if not reply:
        raise RuntimeError("empty reply")
    m["reply"], m["sources"], m["by"] = reply, top + extra, "Assistant" if who == "pa" else "Reviewer"
    if m["kind"] == "correction":
        m["confirm"] = "pending"


def retry(mid):
    """Send a failed message again: your choice, never a timer."""
    m = _load(mid)
    if not m or m["status"] != "error":
        raise ValueError("only a failed message can be sent again")
    m.update(status="queued", tries=0, reply="")
    _save(m)
    return m


def watch():
    import reviewer
    agent.log("chat worker started")
    for m in _all():
        if m["status"] == "working":
            m["status"] = "queued"
            _save(m)
    while True:
        todo = [m for m in _all() if m["status"] == "queued"]
        if not todo:
            time.sleep(10)
            continue
        m = todo[0]
        who = "pa" if m["kind"] == "lookup" and assistant_on() else "gpt"
        if who == "gpt" and not reviewer.ready():
            time.sleep(30)
            continue
        m["status"] = "working"
        _save(m)
        try:
            handle(m, who)
            m["status"] = "replied"
            agent.log(f"chat: replied to {m['id']} ({m['by']})")
        except Exception as e:
            why = f"{type(e).__name__}: {str(e)[:120]}"
            if who == "pa":   # no retry timer: mark it failed, say why, and leave "Send again" to you
                m["status"], m["reply"] = "error", f"The Assistant could not answer ({type(e).__name__})."
                import questions
                questions.ask(f'The Assistant could not answer your chat message "{m["text"][:120]}": {why}',
                              why="Check the Assistant's server on the Settings page. On the Chat page, Send again "
                                  "queues the message again; with the Assistant's switch off, the Reviewer answers it.",
                              kind="assistant", options=[{"label": "OK", "value": "ok"}], payload={"chat": m["id"]})
                agent.log(f"chat: {m['id']} failed on the Assistant ({why})")
            else:
                m["tries"] += 1
                m["status"] = "error" if m["tries"] >= MAX_TRIES else "queued"
                if m["status"] == "error":
                    m["reply"] = f"Could not answer: {type(e).__name__}"
                agent.log(f"chat: {m['id']} failed ({why}), try {m['tries']}")
                time.sleep(60)
        _save(m)


# ---------- web ----------
def register(app, authed):
    router = APIRouter()

    def need(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @router.get("/api/chat")
    def chat_list(request: Request):
        need(request)
        return {"messages": _all()[-60:]}

    @router.post("/api/chat/send")
    async def chat_send(request: Request):
        need(request)
        data = await request.json()
        try:
            m = send(data.get("text", ""))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"id": m["id"]}

    @router.post("/api/chat/confirm")
    async def chat_confirm(request: Request):
        need(request)
        data = await request.json()
        try:
            decide(str(data.get("id", "")), bool(data.get("ok")))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @router.post("/api/chat/retry")
    async def chat_retry(request: Request):
        need(request)
        data = await request.json()
        try:
            retry(str(data.get("id", "")))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @router.get("/chat", response_class=HTMLResponse)
    def chat_page(request: Request):
        if not authed(request):
            return RedirectResponse("/")
        return ui_extra.PAGE.replace("__BODY__", CHAT_HTML).replace("__ACTIVE__", "chat")

    app.include_router(router)


CHAT_HTML = r"""
<style>
.me{margin-left:auto;width:fit-content;max-width:85%;background:var(--acc);color:#fff;border-radius:14px;padding:10px 12px;white-space:pre-wrap;word-break:break-word}
.st{font-size:12px;color:var(--mut);text-align:right;margin:2px 4px 10px}
.bot{width:fit-content;max-width:92%;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 12px;white-space:pre-wrap;word-break:break-word}
#cbox{position:sticky;bottom:0;background:var(--bg);padding:8px 0}
</style>
<div id="thread"></div>
<div id="cbox"><textarea id="ct" rows="3" placeholder="Ask about your notes. Start with /fix to correct a note."></textarea>
<button class="pri" id="cs">Send</button><span id="ce" class="mut"></span></div>
<script>
const LABEL={queued:"Queued",working:"Working (this can take a while)",replied:"Replied",error:"Failed"};
let seen=-1;
function bubble(m){
 const out=[h("div",{class:"me"},(m.kind==="correction"?"/fix ":"")+m.text),h("div",{class:"st"},(LABEL[m.status]||m.status)+(m.by&&m.status==="replied"?" · "+m.by:""))];
 if(m.status==="error")out.push(h("button",{onclick:()=>again(m.id)},"Send again"));
 if(m.reply){const box=h("div",{class:"msg"},h("div",{class:"bot"},m.reply));
  if(m.sources&&m.sources.length)box.append(h("div",{class:"mut"},"Notes used: "+m.sources.join(", ")));
  if(m.confirm==="pending")box.append(h("button",{class:"pri",onclick:()=>decide(m.id,true)},"Yes, file it"),h("button",{onclick:()=>decide(m.id,false)},"No, that is not right"));
  if(m.confirm==="filed")box.append(h("div",{class:"mut"},"Filed to the inbox. The agent will turn it into a proposal; the vault itself is not changed yet."));
  if(m.confirm==="discarded")box.append(h("div",{class:"mut"},"Discarded. Nothing was filed."));
  out.push(box)}
 return out}
async function load(){try{const d=await api("/api/chat");
 $("#thread").replaceChildren(...(d.messages.length?d.messages.flatMap(bubble):[h("div",{class:"card mut"},"Ask a question about your notes, or start with /fix to correct a note.")]));
 if(d.messages.length!==seen){seen=d.messages.length;window.scrollTo(0,document.body.scrollHeight)}}catch(e){}}
async function again(id){try{await api("/api/chat/retry",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:id})});load()}catch(e){$("#ce").textContent=e.message}}
async function decide(id,ok){try{await api("/api/chat/confirm",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:id,ok:ok})});load()}catch(e){$("#ce").textContent=e.message}}
$("#cs").onclick=async()=>{const t=$("#ct").value.trim();if(!t)return;$("#cs").disabled=true;$("#ce").textContent="";
 try{await api("/api/chat/send",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text:t})});$("#ct").value="";await load()}
 catch(e){$("#ce").textContent=e.message}$("#cs").disabled=false};
setInterval(()=>{if(!document.hidden)load()},10000);
load();
</script>
"""

if __name__ == "__main__":
    if "--watch" in sys.argv:
        watch()
    else:
        sys.exit("usage: ui_chat.py --watch")
