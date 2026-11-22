"""The Assistant's memory of one chat. In a chat it sees that chat and no other: one summary of everything older (its
goal, what was decided, what was done, the files, what is still open, other facts), then the newest messages word for
word. With every earlier reply it also sees what it did for that reply: one line per tool call, written by the code.

When a chat passes the size you set, its oldest messages are written into the summary in ONE call to the Assistant, right
after a reply. A message you send meanwhile cancels that call and is answered first; the summary is written afterwards.
The summary is strict JSON, checked here: an answer that does not fit is sent back with the reason, as often as it
takes. A message of yours, or turning the Assistant off, ends that.

Every chat is measured and shrunk on its own. Nothing here ever stops a chat; the size only starts the shrinking.
Nothing is written to the vault: the whole chat is already in raw/conversations.

With the Helper (helper.py) working, the summary is kept current instead: after each of the Assistant's tool calls the
Helper writes a short progress entry, and after each answer it writes that message into the summary ("folded"). The
Assistant's view does not change: the newest messages still come word for word, and past the size the oldest stop being
sent (they are already in the summary) until "Chats kept word for word" are left. A message stops being sent only once
it is in the summary. When the Helper cannot work, the Assistant compresses as described above.

Before this, each older message had its own short summary, and the oldest of those were moved to raw/chat-summaries.
A chat that still has such summaries shows them as before until its next compression, which takes them in. Summaries
already moved to the vault stay there."""
import json
import re
import threading

import agent
import tuning
from scrub import chat_text

# <chat id>.json: {"memory": the six parts, "upto": the newest message no longer sent word for word, "folded": the newest
# message the summary covers (the Helper folds each answer in; without it, the same as upto), "fold_part": [message id,
# parts done] while a big message is folded in parts, "progress": {"msg": message id, "entries": [...]}, "helper_small": true
# while one Helper slot is too small for this chat's summary}
DIR = agent.STATE / "chat_memory"
_LOCK = threading.Lock()   # the Helper's threads and the chat watcher write these files at the same time
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
            "running": (chat in running or _folding(chat)) if chat else bool(running), "requested": bool(st.get("requested"))}


def _folding(cid):
    try:
        import helper
        return helper.folding(cid)
    except Exception:
        return False


def clear_failure():
    _flag(failed_at={}, helper_failed={})


def words(text):
    return len(str(text).split())


# ---------- one chat's summary ----------
def _file(cid):
    return DIR / (re.sub(r"[^A-Za-z0-9._-]", "_", str(cid)) + ".json")


def saved(cid):
    """{"memory": {...}, "upto": message id, ...} for a chat (see DIR), or {} if it has nothing yet."""
    try:
        d = json.loads(_file(cid).read_text())
        if not isinstance(d, dict):
            return {}
        if not isinstance(d.get("memory"), dict):
            d.pop("memory", None)
        return d
    except (OSError, ValueError):
        return {}


def _update(cid, fn):
    """Change one chat's file in place: fn(the saved dict) changes it. One writer at a time."""
    with _LOCK:
        d = saved(cid)
        fn(d)
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = _file(cid).with_name(_file(cid).name + ".tmp")
        tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False))
        tmp.replace(_file(cid))


def folded(cid, d=None):
    """The newest message this chat's summary covers."""
    d = saved(cid) if d is None else d
    return max(str(d.get("folded") or ""), str(d.get("upto") or ""))


# ---------- recent progress ----------
def progress_entries(cid, mid):
    p = saved(cid).get("progress") or {}
    return list(p.get("entries") or []) if p.get("msg") == mid else []


def add_progress(cid, mid, entry, n=None):
    """The Helper's entry for the Assistant's call number n. The first entry of a new reply replaces the last reply's.
    Entries are written side by side, so they are kept in call order, not in the order they were finished."""
    def num(e):
        m = re.match(r"#(\d+)\b", str(e))
        return int(m.group(1)) if m else 0

    def fn(d):
        p = d.get("progress") if isinstance(d.get("progress"), dict) else {}
        if p.get("msg") != mid:
            p = {"msg": mid, "entries": []}
        p["entries"] = sorted(list(p.get("entries") or []) + [entry], key=num)
        d["progress"] = p
    _update(cid, fn)


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
    d = saved(cid)
    out, mem = [], d.get("memory")
    text = render(mem) if mem else ""
    entries = list((d.get("progress") or {}).get("entries") or [])
    if entries:   # what the Helper wrote while the Assistant worked on its last reply
        text += ("\n\n" if text else "") + "Recent progress (your tool calls for your last reply, written as you worked):\n" + \
            "\n".join(f"- {x}" for x in entries)
    if text:
        covers = any(x["id"] <= folded(cid, d) for x in chats if not x.get("summary"))   # the Helper folded shown messages in
        out += [{"role": "user", "content": ("MEMORY OF THIS CHAT SO FAR (it also covers the newest messages, which follow word "
                 "for word; the whole chat is in raw/conversations):\n" if covers else "MEMORY OF THIS CHAT SO FAR (the messages it "
                 "covers are no longer shown; the whole chat is in raw/conversations):\n") + text},
                {"role": "assistant", "content": "Understood."}]
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
    done = folded(cid, had)
    new = [x for x in old if x["id"] > done]   # the ones the Helper already wrote into the summary need no call
    if not new:
        _update(cid, lambda d: d.update(upto=old[-1]["id"]))
        return True
    msgs = [{"role": "user", "content": _material(had.get("memory") or {}, new)}]
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
    _update(cid, lambda d: d.update(memory=mem, upto=old[-1]["id"], folded=max(done, new[-1]["id"])))
    count, limit = words(render(mem)), _g("assistant.summary_words")
    agent.log(f"chat memory: wrote {len(old)} message(s) of chat {cid} into its summary ({count:,} words"
              + (f", over the {limit:,} it was asked to keep to" if count > limit else "") + ")")
    return True


def run(busy):
    """Idle-time work for the chat watcher, right after a reply: compress each chat that passed its size, and carry on one
    that was interrupted. busy() says a chat message is waiting; then this stops and comes back when the chat is idle again."""
    import ui_chat
    if _with_helper(busy):
        return
    if not ui_chat.assistant_on():
        return
    st = _flag()
    running, failed, asked = _list(st.get("running")), _dict(st.get("failed_at")), bool(st.get("requested"))
    limit = _g("assistant.memory_words")
    todo = []
    for cid in _chat_ids():
        if _helper_chat(cid) or not _work(cid):
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


# ---------- with the Helper ----------
def _helper_chat(cid):
    """True when the Helper keeps this chat's summary: it can work, and one of its slots holds the summary."""
    try:
        import helper
        return helper.on() and not saved(cid).get("helper_small")
    except Exception:
        return False


def _with_helper(busy):
    """Idle-time work when the Helper keeps the summaries: fold in any answered message it has not written in yet (after
    a restart, or Try again), and past the size, stop sending the oldest messages word for word, which needs no call: they
    are in the summary already. -> True when the Helper covered every chat (the Assistant has nothing to do)."""
    import helper
    import ui_chat
    if not helper.on():
        return False
    st = _flag()
    failed, asked, limit = _dict(st.get("helper_failed")), bool(st.get("requested")), _g("assistant.memory_words")
    left = False
    for cid in _chat_ids():
        if not _helper_chat(cid):
            left = True   # its summary does not fit a Helper slot: the Assistant keeps it, below
            continue
        d = saved(cid)
        top = folded(cid, d)
        mine = [x for x in live() if ui_chat.chat_of(x) == cid]
        if any(x["id"] > top for x in mine) and failed.get(cid) is None and not helper.folding(cid):
            helper.fold_soon(cid)
        if asked or size(cid) > limit:
            old = [x for x in _work(cid) if x["id"] <= top]
            if old:
                _update(cid, lambda d, u=old[-1]["id"]: d.update(upto=u))
                agent.log(f"chat memory: the {len(old)} oldest message(s) of chat {cid} are no longer sent word for word; "
                          f"the summary covers them (chat memory now {size(cid):,} words)")
    if asked and not left:
        _flag(requested=False)
    return not left


def _for_helper(x, d):
    """One message as the Helper reads it, to write into the summary."""
    import chatfiles
    if x.get("summary"):
        return f"--- {_when(x)} (only a summary of this message was kept)\n{x['summary']}"
    did = "\n".join(str(w) for w in x.get("work") or []) or "(no tool calls)"
    p = d.get("progress") or {}
    notes = "\n".join(p.get("entries") or []) if p.get("msg") == x["id"] else ""
    return (f"--- {_when(x)}\nPERSON:\n{x['text']}{chatfiles.names(x)}\n\nWHAT THE ASSISTANT DID:\n{did}\n\n"
            f"PROGRESS ENTRIES:\n{notes or '(none)'}\n\nTHE ASSISTANT'S REPLY:\n{x['reply']}")


def _parts(text, size):
    """text cut at line breaks into pieces of at most size characters (a single longer line is cut where it must be)."""
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) > size:
            out.append(cur)
            cur = ""
        cur += line
    if cur or not out:
        out.append(cur)
    return out


def fold_with_helper(cid):
    """Write every answered message of this chat that the summary does not cover yet into it, oldest first, one call per
    message (or per part of a big one). Run by the Helper after each answer (helper.after_answer)."""
    import helper
    import questions
    import thinklog
    import ui_chat
    while True:
        if not helper.on():
            return
        d = saved(cid)
        top = folded(cid, d)
        todo = [x for x in live() if ui_chat.chat_of(x) == cid and x["id"] > top]
        if not todo:
            return
        x = todo[0]
        helper.progress_done(cid, x["id"])   # its own progress entries go into the summary with it
        d = saved(cid)
        mem = d.get("memory") or {}
        slot = helper.slot_tokens()
        room = slot - helper.reserve("memory") - helper.tokens(len(helper.MEMORY_SYS)) - 300   # for the summary and the message
        summ = max(helper.tokens(len(render(mem))), helper.summary_tokens())   # the summary as it may grow while folding
        if summ + 300 > room:   # not even the summary and a few lines of the message fit
            if not d.get("helper_small"):
                _update(cid, lambda d: d.update(helper_small=True))
                questions.ask(f"The Helper's context is too small for this chat's summary: one Helper slot holds {slot:,} tokens "
                              f"and the summary needs about {summ + 300 + slot - room:,}. Until that changes, the "
                              "Assistant keeps the chat memory as before. Raise Context on the Helper's card, or lower its Slots.",
                              kind="serverfail", options=[{"label": "OK", "value": "ok"}], payload={"chat": cid})
                agent.log(f"chat memory: one Helper slot ({slot:,} tokens) is too small for the summary of chat {cid}; the Assistant keeps it")
            return
        if d.get("helper_small"):
            _update(cid, lambda d: d.pop("helper_small", None))
        parts = _parts(_for_helper(x, d), max(400, int((room - summ) * 2.2)))
        fp = d.get("fold_part") or []
        start = int(fp[1]) if len(fp) == 2 and fp[0] == x["id"] else 0
        for k in range(start, len(parts)):
            head = ("NEWEST MESSAGE:" if len(parts) == 1 else
                    f"NEWEST MESSAGE, part 1 of {len(parts)} (the rest comes in the next calls):" if k == 0 else
                    f"NEWEST MESSAGE, part {k + 1} of {len(parts)} (the memory already holds parts 1 to {k}):")
            msgs = [{"role": "user", "content": f"THE MEMORY SO FAR:\n{render(mem) or '(empty)'}\n\n{head}\n{parts[k]}"}]
            while True:
                try:
                    text = helper.memory_call(helper.MEMORY_SYS, msgs, "memory", fill={"{SUMMARY_WORDS}": _g("assistant.summary_words")})
                except thinklog.Aborted:
                    return   # turned off or switched: the Assistant's compression covers it until the Helper is back
                except (helper.Full, helper.Limit) as e:
                    why = (f"the request was {e.args[0]:,} tokens and one Helper slot holds {e.args[1]:,}" if isinstance(e, helper.Full)
                           else f"its reply hit Memory max tokens ({e.args[1]:,}) on the Helper's card")
                    _helper_fail(cid, x["id"], why)
                    return
                except Exception as e:
                    _helper_fail(cid, x["id"], f"{type(e).__name__}: {str(e)[:300]}")
                    return
                new, wrong = check(text)
                if new is not None:
                    break
                agent.log(f"chat memory: sent the Helper's summary of chat {cid} back: {wrong[:160]}")
                msgs += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"That is not the memory format, so it was not saved. What is wrong: {wrong}. Reply with ONLY "
                                                     "the JSON object, with all six keys: goal, decisions, done, files, open, facts."}]
            mem = {kk: (chat_text(v) if isinstance(v, str) else [chat_text(y) for y in v]) for kk, v in new.items()}
            last = k == len(parts) - 1
            _update(cid, lambda d, mem=mem, k=k, last=last: (d.update(memory=mem), d.update(folded=x["id"]) if last else None,
                                                            d.pop("fold_part", None) if last else d.update(fold_part=[x["id"], k + 1])))
        agent.log(f"chat memory: the Helper wrote message {x['id']} of chat {cid} into its summary ({words(render(mem)):,} words"
                  + (f", in {len(parts)} parts)" if len(parts) > 1 else ")"))


def _helper_fail(cid, mid, why):
    st = _flag()
    _flag(helper_failed={**_dict(st.get("helper_failed")), cid: mid})
    import questions
    questions.ask(f"The Helper could not write a message of a chat into its summary: {why}",
                  why="Try again runs it at the next idle time. Until then that message is still sent word for word; no chat is stopped.",
                  kind="memory", options=[{"label": "Try again", "value": "retry"}], payload={"helper": cid, "msg": mid})
    agent.log(f"chat memory: the Helper could not write message {mid} of chat {cid} into its summary ({why})")
