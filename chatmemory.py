"""The Assistant's memory of your earlier chats. It sees them with every new message: the newest word for word, the older
ones as one short summary each. When that memory passes the size you set, the Assistant is asked in its idle time to
summarize the oldest chats down to the number kept word for word. If that did not shrink the memory by the percentage you
set, the oldest summaries are moved to the vault (raw/chat-summaries, linked to their chats) until it did. Nothing here
ever stops a chat; the size only starts the shrinking."""
import json
import re
from datetime import datetime

import agent
import tuning
from scrub import scrub

SUMMARIES = agent.VAULT / "raw" / "chat-summaries"
FLAG = agent.STATE / "chat-memory.json"   # {"running": true} while a compression is under way; {"failed_at": size} after one failed


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


def request():
    """Compress now, as if the memory had passed its size: the chat watcher starts it in its next idle time."""
    _flag(requested=True, failed_at=None)


def status():
    st = _flag()
    return {"tokens": size(), "limit": _g("assistant.memory_tokens"), "running": bool(st.get("running")), "requested": bool(st.get("requested"))}


def clear_failure():
    _flag(failed_at=None)


def tokens(text):
    return int(len(text) / max(1, _g("assistant.chars_per_token")))


def cost(m):
    return tokens(m["summary"]) if m.get("summary") else tokens(m["text"] + m["reply"])


def live(before=None):
    """The chats in the Assistant's memory, oldest first: answered, and not moved to the vault."""
    import ui_chat
    return [m for m in ui_chat._all() if m["kind"] == "lookup" and m["status"] == "replied" and m.get("reply")
            and not m.get("vaulted") and (before is None or m["id"] < before)]


def size():
    return sum(cost(m) for m in live())


def _when(m):
    i = m["id"]
    return f"{i[:4]}-{i[4:6]}-{i[6:8]} {i[9:11]}:{i[11:13]}"


def turns(m):
    """The earlier chats as messages for the model: the summaries together first, then the chats word for word."""
    chats = live(m["id"])
    sums = [x for x in chats if x.get("summary")]
    out = []
    if sums:
        out += [{"role": "user", "content": "SUMMARIES OF YOUR EARLIER CHATS WITH ME, oldest first (the full chats are in raw/conversations):\n"
                 + "\n".join(f"- {_when(x)}: {x['summary']}" for x in sums)},
                {"role": "assistant", "content": "Understood."}]
    for x in chats:
        if not x.get("summary"):
            out += [{"role": "user", "content": x["text"]}, {"role": "assistant", "content": x["reply"]}]
    return out


def _work():
    """(chats to summarize, summaries) in order: everything but the newest chats kept word for word."""
    chats = live()
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


def run(busy):
    """Idle-time work for the chat watcher: start a compression when the memory passed its size, and carry one on.
    busy() says a chat message is waiting; then this stops and comes back when the chat is idle again."""
    import ui_chat
    if not ui_chat.assistant_on():
        return
    st = _flag()
    old, sums = _work()
    if not st.get("running"):
        now = size()
        asked = bool(st.get("requested"))
        if not asked and (now <= _g("assistant.memory_tokens") or not (old or sums) or st.get("failed_at") == now):
            return
        _flag(running=True, requested=False)
        agent.log(f"chat memory: {now:,} tokens; compressing in idle time" + (" (you asked for it)" if asked else f", over {_g('assistant.memory_tokens'):,}"))
    for x in old:
        if busy():
            return
        try:
            text = _summarize(x)
            if not text:
                raise RuntimeError("the Assistant wrote an empty summary")
        except Exception as e:   # no retry timer: say why and wait for you
            import thinklog
            if isinstance(e, thinklog.Aborted):
                return   # the server was switched; carry on next idle time
            why = f"{type(e).__name__}: {str(e)[:400]}"
            _flag(running=False, failed_at=size())
            import questions
            questions.ask(f"The Assistant could not compress its chat memory: {why}",
                          why="Try again runs it at the next idle time. Until then the memory just keeps growing; no chat is stopped.",
                          kind="memory", options=[{"label": "Try again", "value": "retry"}], payload={})
            agent.log(f"chat memory: could not summarize {x['id']} ({why})")
            return
        x["summary"] = text
        ui_chat._save(x)
    target = _g("assistant.memory_tokens") * (1 - _g("assistant.memory_shrink") / 100)
    for x in _work()[1]:   # not shrunk enough: the oldest summaries go to the vault
        if size() <= target:
            break
        _to_vault(x)
    _flag(running=False)
    agent.log(f"chat memory: compressed to {size():,} tokens")
