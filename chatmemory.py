"""The Assistant's memory of one chat. In a chat it sees that chat and no other: one summary of everything older (its
goal, what was decided, what was done, the files, what is still open, other facts), then the newest messages word for
word. With every earlier reply it also sees what it did for that reply: one line per tool call, written by the code.

When a chat passes the size you set, its oldest messages are written into the summary in ONE call to the Assistant, right
after a reply. A message you send meanwhile cancels that call and is answered first; the summary is written afterwards.
The summary is strict JSON, checked here: an answer that does not fit is sent back with the reason, as often as it
takes. A message of yours, or turning the Assistant off, ends that.

Every chat is measured and shrunk on its own. Nothing here ever stops a chat; the size only starts the shrinking.
Nothing is written to the vault: the whole chat is already in raw/conversations.

Before this, each older message had its own short summary, and the oldest of those were moved to raw/chat-summaries.
A chat that still has such summaries shows them as before until its next compression, which takes them in. Summaries
already moved to the vault stay there."""
import json
import re

import agent
import tuning
from scrub import chat_text

DIR = agent.STATE / "chat_memory"   # <chat id>.json: {"memory": the six parts, "upto": the id of the newest message it covers}
FLAG = agent.STATE / "chat-memory.json"   # {"running": [chat ids being compressed], "requested": true, "failed_at": {chat id: size}}
PARTS = (("goal", "Goal"), ("decisions", "Decided"), ("done", "Done"), ("files", "Files"), ("open", "Still open"), ("facts", "Also"))


def _g(key):
    return tuning.get(key)


def _flag(**kv):
    try:
        d = json.loads(FLAG.read_text())
    except (OSError, ValueError):
        d = {}
    if kv:
        d.update(kv)
        FLAG.write_text(json.dumps(d))
    return d


def _list(v):
    return list(v) if isinstance(v, list) else []


def _dict(v):
    return dict(v) if isinstance(v, dict) else {}


def request():
    """Compress now, as if every chat had passed its size: the chat watcher starts it in its next idle time."""
    _flag(requested=True, failed_at={})


def status(chat=""):
    """The line above the message box, in words: the chat's summary, and all the Assistant sees of the chat (the summary
    and the messages still kept word for word) against the size that starts a compression."""
    st = _flag()
    running = _list(st.get("running"))
    return {"summary": words(render(saved(chat).get("memory") or {})) if chat else 0, "words": size(chat),
            "limit": _g("assistant.memory_words"),
            "running": (chat in running) if chat else bool(running), "requested": bool(st.get("requested"))}


def clear_failure():
    _flag(failed_at={})


def words(text):
    return len(str(text).split())


# ---------- one chat's summary ----------
def _file(cid):
    return DIR / (re.sub(r"[^A-Za-z0-9._-]", "_", str(cid)) + ".json")


def saved(cid):
    """{"memory": {...}, "upto": message id} for a chat, or {} if it has no summary yet."""
    try:
        d = json.loads(_file(cid).read_text())
        return d if isinstance(d, dict) and isinstance(d.get("memory"), dict) else {}
    except (OSError, ValueError):
        return {}


def forget(cid):
    """The chat was deleted: its summary goes with it."""
    _file(cid).unlink(missing_ok=True)


def render(mem):
    """The summary as the Assistant reads it. A part with nothing in it is left out."""
    out = []
    for key, title in PARTS:
        v = mem.get(key)
        if key == "goal":
            if str(v or "").strip():
                out.append(f"Goal: {str(v).strip()}")
        elif v:
            out.append(f"{title}:\n" + "\n".join(f"- {x}" for x in v))
    return "\n".join(out)


def check(text):
    """An answer to the summary prompt -> (the six parts, "") or (None, what is wrong with it, for the model)."""
    s = re.sub(r"<think>.*?</think>", "", str(text), flags=re.DOTALL).strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    try:
        d = json.loads(s)
    except ValueError as e:
        return None, f"it is not valid JSON ({e})"
    if not isinstance(d, dict):
        return None, "it is not a JSON object"
    keys = [k for k, _ in PARTS]
    for k in keys:
        if k not in d:
            return None, f'the key "{k}" is missing'
    extra = [k for k in d if k not in keys]
    if extra:
        return None, f'the key "{extra[0]}" is not one of the six'
    if not isinstance(d["goal"], str):
        return None, '"goal" must be text'
    for k in keys[1:]:
        if not isinstance(d[k], list) or not all(isinstance(x, str) for x in d[k]):
            return None, f'"{k}" must be a list of text'
    return {"goal": d["goal"].strip(), **{k: [x.strip() for x in d[k] if x.strip()] for k in keys[1:]}}, ""


# ---------- what the Assistant sees ----------
def _reply(m):
    """A reply exactly as the Assistant wrote it. What it did for it (m["work"]) goes only into the chat's summary."""
    return m["reply"]


def cost(m):
    return words(m["summary"]) if m.get("summary") else words(m["text"] + _reply(m))


def live(before=None):
    """The messages in the Assistant's memory, oldest first: answered, not moved to the vault, and not yet written
    into their chat's summary."""
    import ui_chat
    upto = {}
    out = []
    for m in ui_chat._all():
        if m["kind"] != "lookup" or m["status"] != "replied" or not m.get("reply") or m.get("vaulted"):
            continue
        cid = ui_chat.chat_of(m)
        if cid not in upto:
            upto[cid] = saved(cid).get("upto") or ""
        if m["id"] > upto[cid] and (before is None or m["id"] < before):
            out.append(m)
    return out


def _chat_ids():
    import ui_chat
    return list(dict.fromkeys(ui_chat.chat_of(m) for m in live()))


def size(chat=""):
    """Words in what the Assistant sees of a chat: its summary and its messages not yet in the summary."""
    import ui_chat
    msgs = sum(cost(m) for m in live() if not chat or ui_chat.chat_of(m) == chat)
    files = [_file(chat)] if chat else (sorted(DIR.glob("*.json")) if DIR.is_dir() else [])
    for f in files:
        try:
            msgs += words(render(json.loads(f.read_text()).get("memory") or {}))
        except (OSError, ValueError, AttributeError):
            pass
    return msgs


def _when(m):
    i = m["id"]
    return f"{i[:4]}-{i[4:6]}-{i[6:8]} {i[9:11]}:{i[11:13]}"


def turns(m):
    """The earlier part of THIS chat as messages for the model: its summary, then the messages word for word. Other chats are not shown; a new chat starts empty."""
    import chatfiles
    import ui_chat
    cid = ui_chat.chat_of(m)
    chats = [x for x in live(m["id"]) if ui_chat.chat_of(x) == cid]
    out, mem = [], saved(cid).get("memory")
    if mem and render(mem):
        out += [{"role": "user", "content": "MEMORY OF THIS CHAT SO FAR (the messages it covers are no longer shown; the whole chat "
                 "is in raw/conversations):\n" + render(mem)}, {"role": "assistant", "content": "Understood."}]
    sums = [x for x in chats if x.get("summary")]   # from before a chat had one summary: shown until the next compression
    if sums:
        out += [{"role": "user", "content": "SUMMARIES OF YOUR EARLIER CHATS WITH ME, oldest first (the full chats are in raw/conversations):\n"
                 + "\n".join(f"- {_when(x)}: {x['summary']}" for x in sums)},
                {"role": "assistant", "content": "Understood."}]
    for x in chats:
        if not x.get("summary"):
            out += [{"role": "user", "content": x["text"] + chatfiles.names(x)}, {"role": "assistant", "content": _reply(x)}]
    return out


# ---------- compression ----------
def _work(chat):
    """The messages of one chat to write into its summary, oldest first: everything not yet in it but the newest
    messages kept word for word. Older per-message summaries always go."""
    import ui_chat
    chats = [x for x in live() if ui_chat.chat_of(x) == chat]
    plain = [x for x in chats if not x.get("summary")]
    keep = _g("assistant.history")
    kept = {x["id"] for x in plain[-keep:]} if keep else set()
    return [x for x in chats if x["id"] not in kept]


def system():
    return ("You keep the memory of one chat between a person and you, their assistant. You get THE MEMORY SO FAR (it may be "
            "empty) and the OLDEST MESSAGES of the chat. Once you have written them into the memory you will no longer see those "
            "messages. Each message shows what the person wrote, what you did (your tool calls) and your reply. Write the new "
            "memory: everything in the memory so far that still matters, plus what these messages add. Keep exact names, file "
            "paths, commands, addresses, numbers, decisions and corrections. Where the person corrected something, keep only the "
            f"corrected version. Leave out filler and the way things were said. Use at most {_g('assistant.summary_words')} words in "
            'all. Reply with ONLY JSON: {"goal":"<what the person is trying to get done in this chat>","decisions":["<something '
            'decided, or a preference the person stated>"],"done":["<something finished, with the file or place>"],"files":["<path>: '
            '<what it is, or what was changed in it>"],"open":["<something still to do, waiting for an answer, or '
            'unanswered>"],"facts":["<anything else worth keeping>"]}. Use an empty list where there is nothing.')


def _material(mem, old):
    import chatfiles
    parts = []
    for x in old:
        if x.get("summary"):
            parts.append(f"--- {_when(x)} (only a summary of this message was kept)\n{x['summary']}")
        else:
            did = "\n".join(str(w) for w in x.get("work") or []) or "(no tool calls)"
            parts.append(f"--- {_when(x)}\nPERSON:\n{x['text']}{chatfiles.names(x)}\n\nWHAT YOU DID:\n{did}\n\nYOUR REPLY:\n{x['reply']}")
    return f"THE MEMORY SO FAR:\n{render(mem) or '(empty)'}\n\nOLDEST MESSAGES:\n" + "\n\n".join(parts)


def _fail(cid, why):
    _flag(running=[], failed_at={**_dict(_flag().get("failed_at")), cid: size(cid)})
    import questions
    questions.ask(f"The Assistant could not compress the memory of a chat: {why}",
                  why="Try again runs it at the next idle time. Until then the memory just keeps growing; no chat is stopped.",
                  kind="memory", options=[{"label": "Try again", "value": "retry"}], payload={})
    agent.log(f"chat memory: could not write the summary of chat {cid} ({why})")


def _compress(cid, busy):
    """Write one chat's oldest messages into its summary, in one call. False when it stopped before finishing (a
    message is waiting, the server was switched, or it failed)."""
    import thinklog
    import ui_chat
    old = _work(cid)
    if not old:
        return True
    had = saved(cid)
    msgs = [{"role": "user", "content": _material(had.get("memory") or {}, old)}]
    while True:   # no limit on the tries: a message of yours, or turning the Assistant off, ends it
        if busy():
            return False
        try:
            text = ui_chat._ask_assistant(system(), msgs, "memory", cancel=busy)
        except thinklog.Aborted:
            return False   # a message of yours came in, or the server was switched: carry on at the next idle time
        except Exception as e:   # no retry timer: say why and wait for you
            _fail(cid, f"{type(e).__name__}: {str(e)[:400]}")
            return False
        mem, wrong = check(text)
        if mem is not None:
            break
        agent.log(f"chat memory: sent the summary of chat {cid} back: {wrong[:160]}")
        msgs += [{"role": "assistant", "content": text},
                 {"role": "user", "content": f"That is not the memory format, so it was not saved. What is wrong: {wrong}. Reply with ONLY "
                                             "the JSON object, with all six keys: goal, decisions, done, files, open, facts."}]
    mem = {k: (chat_text(v) if isinstance(v, str) else [chat_text(x) for x in v]) for k, v in mem.items()}
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = _file(cid).with_suffix(".tmp")
    tmp.write_text(json.dumps({"memory": mem, "upto": old[-1]["id"]}, indent=1, ensure_ascii=False))
    tmp.replace(_file(cid))
    count, limit = words(render(mem)), _g("assistant.summary_words")
    agent.log(f"chat memory: wrote {len(old)} message(s) of chat {cid} into its summary ({count:,} words"
              + (f", over the {limit:,} it was asked to keep to" if count > limit else "") + ")")
    return True


def run(busy):
    """Idle-time work for the chat watcher, right after a reply: compress each chat that passed its size, and carry on one
    that was interrupted. busy() says a chat message is waiting; then this stops and comes back when the chat is idle again."""
    import ui_chat
    if not ui_chat.assistant_on():
        return
    st = _flag()
    running, failed, asked = _list(st.get("running")), _dict(st.get("failed_at")), bool(st.get("requested"))
    limit = _g("assistant.memory_words")
    todo = []
    for cid in _chat_ids():
        if not _work(cid):
            continue
        now = size(cid)
        if cid in running or asked or (now > limit and failed.get(cid) != now):
            todo.append(cid)
    if not todo:
        if asked or running:
            _flag(running=[], requested=False)
        return
    if todo != running or asked:
        agent.log(f"chat memory: compressing {len(todo)} chat(s)" + (" (you asked for it)" if asked else f", over {limit:,} words"))
    _flag(running=todo, requested=False)
    for cid in todo:
        if not _compress(cid, busy):
            return
        todo = [c for c in todo if c != cid]
        _flag(running=todo)
        agent.log(f"chat memory: chat {cid} compressed to {size(cid):,} words")
