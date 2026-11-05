"""The Assistant's memory of one chat. In a chat it sees that chat's earlier messages and no other chat's: the newest word
for word, the older ones as one short summary each. When one chat's memory passes the size you set, the Assistant is asked
in its idle time to summarize that chat's oldest messages down to the number kept word for word. If that did not shrink it
by the percentage you set, that chat's oldest summaries are moved to the vault (raw/chat-summaries, linked to their chats)
until it did. Every chat is measured and shrunk on its own. Nothing here ever stops a chat; the size only starts the
shrinking."""
import json
import re
from datetime import datetime

import agent
import tuning
from scrub import scrub

SUMMARIES = agent.VAULT / "raw" / "chat-summaries"
FLAG = agent.STATE / "chat-memory.json"   # {"running": [chat ids being compressed], "requested": true, "failed_at": {chat id: size}}


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
    """The memory bar: with a chat, how much of the memory is that chat's own (all that the Assistant sees in it)."""
    st = _flag()
    running = _list(st.get("running"))
    return {"tokens": size(chat), "limit": _g("assistant.memory_tokens"),
            "running": (chat in running) if chat else bool(running), "requested": bool(st.get("requested"))}


def clear_failure():
    _flag(failed_at={})


def tokens(text):
    return int(len(text) / max(1, _g("assistant.chars_per_token")))


def cost(m):
    return tokens(m["summary"]) if m.get("summary") else tokens(m["text"] + m["reply"])


def live(before=None):
    """The chats in the Assistant's memory, oldest first: answered, and not moved to the vault."""
    import ui_chat
    return [m for m in ui_chat._all() if m["kind"] == "lookup" and m["status"] == "replied" and m.get("reply")
            and not m.get("vaulted") and (before is None or m["id"] < before)]


def size(chat=""):
    import ui_chat
    return sum(cost(m) for m in live() if not chat or ui_chat.chat_of(m) == chat)


def _when(m):
    i = m["id"]
    return f"{i[:4]}-{i[4:6]}-{i[6:8]} {i[9:11]}:{i[11:13]}"


def turns(m):
    """The earlier messages of THIS chat as messages for the model: the summaries together first, then word for word.
    Other chats are not shown; a new chat starts empty."""
    import ui_chat
    chats = [x for x in live(m["id"]) if ui_chat.chat_of(x) == ui_chat.chat_of(m)]
    sums = [x for x in chats if x.get("summary")]
    out = []
    if sums:
        out += [{"role": "user", "content": "SUMMARIES OF YOUR EARLIER CHATS WITH ME, oldest first (the full chats are in raw/conversations):\n"
                 + "\n".join(f"- {_when(x)}: {x['summary']}" for x in sums)},
                {"role": "assistant", "content": "Understood."}]
    for x in chats:
        if not x.get("summary"):
            out += [{"role": "user", "content": x["text"] + __import__("chatfiles").names(x)}, {"role": "assistant", "content": x["reply"]}]
    return out


def _chat_ids():
    import ui_chat
    return list(dict.fromkeys(ui_chat.chat_of(m) for m in live()))


def _work(chat=""):
    """(messages to summarize, summaries) of one chat in order: everything but its newest messages kept word for word."""
    import ui_chat
    chats = [x for x in live() if not chat or ui_chat.chat_of(x) == chat]
    plain, sums = [x for x in chats if not x.get("summary")], [x for x in chats if x.get("summary")]
    keep = _g("assistant.history")
    return (plain[:-keep] if keep else plain), sums


def _summarize(x):
    import ui_chat
    system = (f"Write a summary of this exchange between a person and their assistant, for the assistant's own memory, in at most "
              f"{_g('assistant.summary_words')} words. Keep names, file paths, addresses, decisions, corrections and anything left "
              "open. Leave out filler and the way it was said. Reply with the summary only.")
    text = ui_chat._ask_assistant(system, [{"role": "user", "content": f"PERSON:\n{x['text']}\n\nASSISTANT:\n{x['reply']}"}], "memory")
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _to_vault(x):
    """Move one summary out of working memory into raw/chat-summaries: one note per day, each summary linked to its chat."""
    import ui_chat
    day = f"{x['id'][:4]}-{x['id'][4:6]}-{x['id'][6:8]}"
    link = f"[[raw/conversations/{ui_chat.conv_name(day)[:-3]}]]"
    summary = scrub(x["summary"])[0].replace("[[", "[ [")
    section = f"### {_when(x)} (summary of {link})\n\n{summary}\n"
    path = SUMMARIES / f"{day}-chat-summaries.md"
    SUMMARIES.mkdir(parents=True, exist_ok=True)
    if path.exists():
        text = path.read_text(encoding="utf-8").rstrip("\n") + "\n\n" + section
    else:
        title = f"Chat summaries, {day}"
        text = f"---\ntype: chat-summary\ntitle: {title}\ncreated: {day}\n---\n\n# {title}\n\n{section}"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    x["vaulted"] = True
    ui_chat._save(x)


def _compress(cid, busy):
    """Shrink one chat. False when it stopped before finishing (a message is waiting, the server was switched, or it failed)."""
    import ui_chat
    for x in _work(cid)[0]:
        if busy():
            return False
        try:
            text = _summarize(x)
            if not text:
                raise RuntimeError("the Assistant wrote an empty summary")
        except Exception as e:   # no retry timer: say why and wait for you
            import thinklog
            if isinstance(e, thinklog.Aborted):
                return False   # the server was switched; carry on next idle time
            why = f"{type(e).__name__}: {str(e)[:400]}"
            _flag(running=[], failed_at={**_dict(_flag().get("failed_at")), cid: size(cid)})
            import questions
            questions.ask(f"The Assistant could not compress the memory of a chat: {why}",
                          why="Try again runs it at the next idle time. Until then the memory just keeps growing; no chat is stopped.",
                          kind="memory", options=[{"label": "Try again", "value": "retry"}], payload={})
            agent.log(f"chat memory: could not summarize {x['id']} in chat {cid} ({why})")
            return False
        x["summary"] = text
        ui_chat._save(x)
    target = _g("assistant.memory_tokens") * (1 - _g("assistant.memory_shrink") / 100)
    for x in _work(cid)[1]:   # not shrunk enough: this chat's oldest summaries go to the vault
        if size(cid) <= target:
            break
        _to_vault(x)
    return True


def run(busy):
    """Idle-time work for the chat watcher: start a compression for each chat that passed its size, and carry on one that
    was interrupted. busy() says a chat message is waiting; then this stops and comes back when the chat is idle again."""
    import ui_chat
    if not ui_chat.assistant_on():
        return
    st = _flag()
    running, failed, asked = _list(st.get("running")), _dict(st.get("failed_at")), bool(st.get("requested"))
    limit = _g("assistant.memory_tokens")
    todo = []
    for cid in _chat_ids():
        old, sums = _work(cid)
        now = size(cid)
        if cid in running or (asked and (old or sums)) or (now > limit and (old or sums) and failed.get(cid) != now):
            todo.append(cid)
    if not todo:
        if asked or running:
            _flag(running=[], requested=False)
        return
    if todo != running or asked:
        agent.log(f"chat memory: compressing {len(todo)} chat(s) in idle time" + (" (you asked for it)" if asked else f", over {limit:,}"))
    _flag(running=todo, requested=False)
    for cid in todo:
        if not _compress(cid, busy):
            return
        todo = [c for c in todo if c != cid]
        _flag(running=todo)
        agent.log(f"chat memory: chat {cid} compressed to {size(cid):,} tokens")
