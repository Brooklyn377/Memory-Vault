"""Pipeline limits: the numbers that cap what each job reads and keeps. Defaults equal the old hardcoded values.
tuning.get('key') reads the saved value at call time and falls back to the default.
The range of each limit is the usual one: a value outside it is saved with a warning. -1 means no limit (OFF_AS
says what that is where it is not "unlimited"). Any other value below the lowest that can work at all (floor) is
refused."""
import json
import os
from pathlib import Path

FILE = Path("/var/lib/memory-agent/tuning.json")
# key, model, label, what it does, default, usual lowest, usual highest, services to restart (blank = live)
REG = [
    ("sweep.min_quote_chars", "qwen", "Shortest quote accepted for a fact", "A fact is kept only if its quote is at least this long and appears word for word in the chat part. Lower lets weaker quotes through. The Sweeper is told this number with each chunk.", 15, 6, 200, ""),
    ("questions.max_open", "qwen", "Ask queue size", "Most open questions at once. The Sweeper stops queuing hygiene cards when it is full.", 25, 5, 200, "memory-web, memory-sweep"),
    ("review.retries", "gpt", "Retries on a chunk the Reviewer rejects", "How often the Sweeper is asked to redo a chunk the Reviewer disliked before the facts are dropped.", 1, 0, 5, "memory-sweep"),
    ("review.min_quote_chars", "gpt", "Shortest quote the Reviewer's checks accept", "Used by the task check, file tracker, curator and map pass to confirm a quote is really in the text.", 12, 6, 200, ""),
    ("review.recap_chars", "gpt", "Recap length the gate sees", "The Sweeper's recap is cut to this many characters before the Reviewer reads it.", 600, 200, 5000, "memory-sweep"),
    ("review.quote_chars", "gpt", "Quote length the gate sees", "Each fact's quote is cut to this many characters before the Reviewer reads it.", 200, 100, 2000, "memory-sweep"),
    ("reviewer.note_chars", "gpt", "Summary text read per task scan", "How much of a summary the Reviewer reads when looking for unfinished tasks.", 6000, 1000, 40000, "memory-review-worker"),
    ("taskcheck.max_tasks", "gpt", "Open tasks checked per chunk", "Open tasks compared against each chunk's new facts.", 30, 5, 200, ""),
    ("files.window", "gpt", "File check: characters around each mention", "Text kept on each side of a file name when the Reviewer reads chats about a file.", 1500, 300, 8000, "memory-filechanges"),
    ("files.excerpt_chars", "gpt", "File check: chat text per call", "Most chat text sent to the Reviewer in one file check.", 12000, 3000, 60000, "memory-filechanges"),
    ("chat.notes", "gpt", "Chat: notes read in full", "Best-matching notes the chat reads for a question.", 4, 1, 12, ""),
    ("chat.hop_notes", "gpt", "Chat: notes whose links are followed", "How many of those notes have their linked notes pulled in.", 2, 0, 8, ""),
    ("chat.linked", "gpt", "Chat: linked notes added", "Most linked notes added to the chat's context.", 4, 0, 20, ""),
    ("chat.note_chars", "gpt", "Chat: characters read per note", "Text taken from each full note.", 1500, 300, 8000, ""),
    ("chat.linked_chars", "gpt", "Chat: characters read per linked note", "Text taken from each linked note.", 300, 50, 3000, ""),
    ("curator.note_chars", "cur", "Note text shown", "Characters of the note under review.", 8000, 1000, 40000, ""),
    ("curator.candidates", "cur", "Unlinked candidate notes shown", "Notes it could link to, shown with each note.", 60, 0, 300, ""),
    ("curator.linked_shown", "cur", "Linked notes shown", "Notes already linked to this one that are shown for context.", 40, 0, 200, ""),
    ("curator.source_chats", "cur", "Source chats shown", "Raw chats a note was written from that are included.", 8, 1, 30, ""),
    ("curator.facts_checked", "cur", "Facts checked per note", "Most fact lines it will judge in one note.", 25, 1, 100, ""),
    ("curator.questions_per_note", "cur", "Questions asked per note", "Most Ask cards one note can raise about its facts.", 3, 0, 20, ""),
    ("curator.links_per_note", "cur", "Links added per note", "Most links it can add to one note in one review.", 6, 0, 50, ""),
    ("curator.conflicts_per_note", "cur", "Conflicts raised per note", "Most conflicts between notes it can ask about per review.", 2, 0, 20, ""),
    ("curator.idle_seconds", "cur", "Idle time before it swaps in (seconds)", "How long the Sweeper and Reviewer must both be idle before the curator starts.", 60, 0, 3600, ""),
    ("map.links_per_call", "cur", "Map pass: links per call", "Most links accepted from one map-pass call.", 25, 0, 200, ""),
    ("map.hubs_per_call", "cur", "Map pass: hubs per call", "Most hub notes accepted from one call.", 5, 0, 30, ""),
    ("map.hub_members", "cur", "Map pass: notes per hub", "Most notes listed in one hub.", 30, 3, 100, ""),
    ("map.hub_min_members", "cur", "Map pass: smallest hub", "A group needs at least this many notes to become a hub. The map pass is told this number with each call.", 3, 2, 20, ""),
    ("map.gist_chars", "cur", "Map pass: gist length", "Characters of each note shown in the whole-vault index.", 200, 50, 1000, ""),
]
REG += [
    ("sweep.chunk_chars", "qwen", "Chunk size (characters of chat per read)", "How much of a chat the Sweeper reads at once. Parts already read keep their size; unread parts, new imports and the Queue use the new size. Bigger means fewer, larger calls and more text for the model to hold. One message up to 12,000 characters is never split.", 8000, 2000, 60000, ""),
    ("sweep.max_facts", "qwen", "Facts kept per chunk", "The most facts the code keeps from one reply. The Sweeper is told this number with each chunk.", 6, 1, 30, ""),
    ("sweep.attempts", "qwen", "Attempts per chunk before it is skipped", "How many times the Sweeper retries a reply it could not use.", 3, 1, 10, ""),
    ("agent.hop_depth", "qwen", "Inbox agent: link hops", "How many links away from a note the inbox agent looks for notes that may need to change. Replaces the value in config.yaml.", 1, 1, 3, ""),
    ("agent.max_candidates", "qwen", "Inbox agent: candidate notes per claim", "Notes the inbox agent considers for each claim. Replaces the value in config.yaml.", 4, 1, 30, ""),
    ("agent.max_tool_calls", "qwen", "Inbox agent: model calls per note", "Hard cap on model calls while handling one inbox note. Replaces the value in config.yaml.", 8, 1, 60, ""),
]
REG += [
    ("sweep.recap_chars", "qwen", "Recap length kept (characters)", "The code cuts each chunk's recap to this many characters. The Sweeper is told the matching word count with each chunk (about 7.5 characters per word, so 900 is 120 words). Raise the Reviewer's 'Recap length the gate sees' to at least this.", 900, 300, 12000, ""),
    ("sweep.overlap_chars", "qwen", "End of the previous chunk shown (characters)", "Gives the model the last part of the previous chunk, word for word, as background. 0 turns it off. Quotes must still come from the new chunk.", 0, 0, 20000, ""),
    ("sweep.facts_context", "qwen", "Earlier facts shown", "Shows the most recent facts already recorded from earlier in this chat, so the model can spot contradictions and avoid repeating. 0 turns it off.", 0, 0, 200, ""),
]
BY = {r[0]: r for r in REG}
_cache = {"m": None, "d": {}}


def _saved():
    try:
        m = FILE.stat().st_mtime_ns
    except OSError:
        _cache.update(m=None, d={})
        return {}
    if m != _cache["m"]:
        try:
            d = json.loads(FILE.read_text())
        except (OSError, ValueError):
            d = {}
        _cache.update(m=m, d=d if isinstance(d, dict) else {})
    return _cache["d"]


OFF = -1                  # typed in a box: no limit
NO_LIMIT = 10 ** 9        # what get() returns for a cap with no limit; slices and range() take it as "all"
# Limits where "no limit" is not a huge number: what get() returns instead, and what -1 means on the page.
OFF_AS = {
    "sweep.min_quote_chars": (1, "no length minimum; a quote is still required"),
    "review.min_quote_chars": (1, "no length minimum; a quote is still required"),
    "map.hub_min_members": (1, "no minimum; a hub still needs one note"),
    "curator.idle_seconds": (0, "no wait"),
}
# Limits that stop a retry loop: with no limit, something the model cannot handle is retried without end.
LOOPS = ("sweep.attempts", "review.retries", "agent.max_tool_calls")


def floor(r):
    """The lowest value that can work at all: 0 where the usual range starts at 0, otherwise 1."""
    return 0 if r[5] <= 0 else 1


def value(key, saved):
    """The value code uses for what is saved: the default if nothing valid is saved, and for -1 no limit."""
    r = BY[key]
    try:
        x = int(saved.get(key))
    except (TypeError, ValueError):
        return r[4]
    if x == OFF:
        return OFF_AS.get(key, (NO_LIMIT,))[0]
    return x if x >= floor(r) else r[4]


def get(key):
    return value(key, _saved())


def off(key):
    """True when the limit is set to -1 (no limit)."""
    return _saved().get(key) == OFF


def listing():
    s = _saved()
    return [{"key": r[0], "model": r[1], "label": r[2], "help": r[3], "default": r[4], "lo": r[5], "hi": r[6],
             "min": floor(r), "off": OFF_AS.get(r[0], (0, "no limit"))[1], "restart": r[7], "saved": s.get(r[0])}
            for r in REG]


def token_warnings(cur=None, s=None):
    """Calls that may not fit in a model's context, for these limits and model settings (default: the saved ones).
    Only warnings: a call that does not fit fails or is cut short, and the person decides what to change."""
    out = []
    try:
        import settings
        s = s or settings.load()
    except Exception:
        return out
    cur = _saved() if cur is None else cur

    def g(k):
        return value(k, cur)

    def amount(n):   # a limit set to -1 makes the estimate meaningless; say so instead of printing it
        return "any number of tokens (a limit is set to -1)" if n >= NO_LIMIT / 100 else f"about {int(n):,} tokens"

    r, est = s["gpt"], (lambda chars: chars / 2.2)
    ctx = int(r["context"])
    need = est(g("chat.notes") * g("chat.note_chars") + g("chat.linked") * g("chat.linked_chars")) + r["max_tokens"]["chat"] + 1500
    if need > ctx:
        out.append(f"Chat could send {amount(need)} against the Reviewer's {ctx:,} context. Lower the chat note sizes or raise the context.")
    need = est(g("reviewer.note_chars")) + r["max_tokens"]["review"] + 1500
    if need > ctx:
        out.append(f"A task scan could need {amount(need)} against the Reviewer's {ctx:,} context. Lower the summary size.")
    need = est(9000 + g("files.excerpt_chars")) + r["max_tokens"]["review"] + 1500
    if need > ctx:
        out.append(f"A file check could need {amount(need)} against the Reviewer's {ctx:,} context. Lower the chat text per call.")
    q = s["qwen"]
    one = (g("sweep.chunk_chars") + g("sweep.recap_chars") + g("sweep.overlap_chars")) / 2.2 + g("sweep.facts_context") * 70 + q["max_tokens"]["sweep"] + 1500
    if one > int(q["context"]):
        out.append(f"One Sweeper call could need {amount(one)} (chunk text, the answer limit and the prompt) against the Sweeper's {int(q['context']):,} context. Lower the chunk size or the sweep token limit, or raise the context.")
    elif one * int(q["parallel"]) > int(q["context"]):
        out.append(f"The Sweeper has {q['parallel']} slots sharing {int(q['context']):,} tokens, and {q['parallel']} full-size calls at once could need about {int(one * q['parallel']):,}. That's only if they all run to their limits.")
    return out


def save(values):
    """Save the changed values. A blank or default value is removed. Returns (services that must restart, warnings):
    a changed value outside its usual range, and calls that this save makes too big for a model's context (an
    older one does not come back every time; token_warnings lists them all)."""
    cur, changed, warn = dict(_saved()), [], []
    before = set(token_warnings(cur))
    for k, v in (values or {}).items():
        r = BY.get(k)
        if r is None:
            raise ValueError("unknown setting " + str(k))
        new = None
        if v is not None and str(v).strip() != "":
            x = int(float(v))
            if x == OFF:
                if k in LOOPS and x != cur.get(k):
                    warn.append(f"{r[2]}: with no limit, something the model cannot handle is retried without end.")
            elif x < floor(r):
                raise ValueError(f"{r[2]}: must be at least {floor(r)}, or -1 for no limit")
            elif not r[5] <= x <= r[6] and x != cur.get(k):
                warn.append(f"{r[2]}: {x:,} is outside the usual {r[5]:,} to {r[6]:,}.")
            new = None if x == r[4] else x
        old = cur.get(k)
        if new is None:
            cur.pop(k, None)
        else:
            cur[k] = new
        if old != new:
            changed.append(k)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=1, sort_keys=True))
    os.replace(tmp, FILE)
    return (sorted({u.strip() for k in changed for u in BY[k][7].split(",") if u.strip()}),
            warn + [x for x in token_warnings(cur) if x not in before])
