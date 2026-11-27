"""The to-do list of one chat, shared by the Assistant and you.

One JSON file per chat in <state>/chat_todo/<chat id>.json: {"next": the next step number, "steps": [{"n", "text", "by"
("assistant" or "person"), "required", "done"}]}. Numbers are never reused, so "step 4" always means the same step.

The Assistant adds and ticks off steps with its todo_* tools; the steps not done yet follow every message it is sent
(block()). You add, tick off, mark required and delete steps from the To-do panel on the Chat page. While a required
step is not done, the Assistant's answer is sent back (required_open()). Nothing stops the Assistant ticking a step off:
the block is a reminder, not an enforcer. No model calls here."""
import json
import os
import re
import threading

import agent

DIR = agent.STATE / "chat_todo"
_LOCK = threading.Lock()   # the chat worker and the web page both change lists


class Refused(ValueError):
    """A tool call that was not carried out: the Assistant is told why in these words."""
    plain = True


def _path(cid):
    cid = str(cid or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", cid):
        raise ValueError("that is not a chat")
    return DIR / f"{cid}.json"


def load(cid):
    try:
        d = json.loads(_path(cid).read_text())
        if isinstance(d, dict) and isinstance(d.get("steps"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"next": 1, "steps": []}


def _store(cid, d):
    DIR.mkdir(parents=True, exist_ok=True)
    p = _path(cid)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False))
    os.replace(tmp, p)


def forget(cid):
    """The chat was deleted: its list goes with it."""
    try:
        _path(cid).unlink(missing_ok=True)
    except ValueError:
        pass


def _label(s):
    tags = (["required"] if s.get("required") else []) + (["added by the person"] if s.get("by") == "person" else [])
    return f"{s['n']}. {s['text']}" + (f" ({', '.join(tags)})" if tags else "")


def _find(d, n, empty_word):
    try:
        k = int(str(n).strip())
    except (TypeError, ValueError):
        raise Refused(f'n is the number of a step, like 2, as todo_list shows it: "{n}" is not one. {empty_word}') from None
    for s in d["steps"]:
        if s["n"] == k:
            return s
    nums = [s["n"] for s in d["steps"]]
    have = f"the steps are {min(nums)} to {max(nums)}" if nums else "it is empty"
    raise Refused(f"There is no step {k} in this chat's to-do list; {have}. {empty_word}")


def add(cid, text, by="assistant", required=False):
    """-> the new step. Refused: no text, more than one line, or the same text as a step not done yet."""
    text = str(text or "").strip()
    if not text:
        raise Refused("Give the step in text. Nothing was added.")
    if "\n" in text or "\r" in text:
        raise Refused("A step is one line: text has a line break. Nothing was added.")
    with _LOCK:
        d = load(cid)
        for s in d["steps"]:
            if not s["done"] and s["text"] == text:
                raise Refused(f"Step {s['n']} already says exactly that, so nothing was added.")
        s = {"n": d["next"], "text": text, "by": "person" if by == "person" else "assistant",
             "required": bool(required) and by == "person", "done": False}
        d["steps"].append(s)
        d["next"] += 1
        _store(cid, d)
    agent.log(f"chat todo: {'you' if by == 'person' else 'the Assistant'} added step {s['n']}"
              f"{' (required)' if s['required'] else ''}: {text[:120]}")
    return s


def done(cid, n, by="assistant"):
    with _LOCK:
        d = load(cid)
        s = _find(d, n, "Nothing was ticked off.")
        if s["done"]:
            raise Refused(f"Step {s['n']} is already done, so nothing changed.")
        s["done"] = True
        _store(cid, d)
    agent.log(f"chat todo: {'you' if by == 'person' else 'the Assistant'} ticked off step {s['n']}: {s['text'][:120]}")
    return s


def person(cid, what, n=None, text=None, required=False):
    """Your change from the To-do panel: add, done, undone, required, optional, delete or clear (the done steps)."""
    if what == "add":
        return add(cid, text, "person", required)
    if what == "done":
        return done(cid, n, "person")
    with _LOCK:
        d = load(cid)
        if what == "clear":
            d["steps"] = [s for s in d["steps"] if not s["done"]]
        elif what in ("undone", "required", "optional", "delete"):
            s = _find(d, n, "")
            if what == "undone":
                s["done"] = False
            elif what == "delete":
                d["steps"].remove(s)
            else:
                s["required"] = what == "required"
        else:
            raise ValueError("add, done, undone, required, optional, delete or clear")
        _store(cid, d)
    agent.log(f"chat todo: you changed the list ({what}{'' if n is None else ' step ' + str(n)})")


def required_open(cid):
    return [s for s in load(cid)["steps"] if s["required"] and not s["done"]]


def block(cid):
    """The steps not done yet, put after every message the Assistant is sent; "" when there are none."""
    left = [s for s in load(cid)["steps"] if not s["done"]] if cid else []
    if not left:
        return ""
    return ("\n\nTODO (the steps of this chat's to-do list not done yet; tick one off with todo_done when it is done):\n"
            + "\n".join(_label(s) for s in left))


# ---------- the Assistant's tools ----------
def tool_add(cid, text):
    s = add(cid, text)
    return f"Added step {s['n']}: {s['text']}."


def tool_done(cid, n):
    s = done(cid, n)
    return f"Step {s['n']} is done: {s['text']}."


def tool_list(cid):
    steps = load(cid)["steps"]
    if not steps:
        return "The to-do list of this chat is empty."
    return "\n".join(f"{s['n']}. [{'done' if s['done'] else ' '}] " + _label(s).split(". ", 1)[1] for s in steps)
