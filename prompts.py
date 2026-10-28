"""Editable system prompts. Defaults live in the code; an edit is a text file in /var/lib/memory-agent/prompts/.
thinklog.chat swaps an edited prompt in when the system message matches a known default exactly."""
import hashlib
import importlib
import json
import os
import re
import time
from pathlib import Path

import agent

STATE = agent.STATE
DIR, HIST, META = STATE / "prompts", STATE / "prompt_history", STATE / "prompt_meta.json"
KEEP, MAX_CHARS = 20, 30000
KEYS = re.compile(r'"([A-Za-z_]\w*)"\s*:')

LOCAL = {
    "curator_intro": "You are the curator of one person's notes vault. Take your time and be thorough; nothing else needs you. You get ONE entity note, numbered fact lines, the notes it links to, the SOURCE CHATS and CANDIDATE notes.",
    "curator_verify": "VERIFY: for each fact line say supported, unsupported or contradicted by the SOURCE CHATS. For contradicted, quote the chat text that shows it.",
    "curator_connect": "CONNECT: smaller models wrote these notes from a few messages at a time and missed links across chats. Read the source chats for things the notes missed, and propose a link to a CANDIDATE only if the two notes are clearly about related things; quote exact words from this note or the source chats (quote_a) and exact words from the candidate (quote_b). Also report a CONFLICT if a candidate or a linked note states something that disagrees with this note.",
    "curator_prune": "PRUNE: decide keep, prune or unsure. Prune only if the note records nothing useful: a stray word, a generic term, a file name, or something mentioned in passing with no lasting information. Set certain true only if you are sure.",
    "curator_format": ("Quote exactly; never invent; when unsure say so. Reply with ONLY JSON: "
                       '{"facts":[{"line":1,"verdict":"supported|unsupported|contradicted","quote":"","note":""}],'
                       '"links":[{"to":"<candidate path>","why":"","quote_a":"","quote_b":""}],'
                       '"conflicts":[{"with":"<candidate or linked path>","why":"","quote_a":"","quote_b":""}],'
                       '"prune":{"decision":"keep|prune|unsure","certain":false,"reason":""}}'),
}
# (id, model, title, what it is used for, module, constant)
REG = [
    ("sweep_chunk", "qwen", "Chunk reader", "Reads each part of a chat and proposes facts, each with a quote.", "sweep", "SYS"),
    ("inbox_extract", "qwen", "Inbox note: claims", "Pulls claims out of a note you sent (inbox notes and confirmed chat corrections).", "agent", "EXTRACT_SYS"),
    ("inbox_judge", "qwen", "Inbox note: does a note change", "Decides whether an existing note should change because of a claim.", "agent", "JUDGE_SYS"),
    ("bootstrap_summary", "qwen", "Document summary (first import only)", "Summarises a document during the one-time bootstrap.", "bootstrap", "SYS"),
    ("review_gate", "gpt", "Chunk gate", "Judges the Sweeper's facts for one chat part before anything is written.", "reviewgate", "GATE_SYS"),
    ("task_scan", "gpt", "Task scan", "Reads a summary and lists unfinished tasks for the To-do page.", "reviewer", "SCAN_SYS"),
    ("task_update", "gpt", "To-do update reader", "Reads an update you typed on a task.", "reviewer", "UPDATE_SYS"),
    ("task_check", "gpt", "Task check", "After each chunk, closes or updates open tasks that the new facts answer.", "taskcheck", "SYS"),
    ("file_changes", "gpt", "File change tracker", "Reads chats about a saved file and records edits to its changed copy.", "filechanges", "SYS"),
    ("chat_lookup", "gpt", "Chat: answer a question", "Answers your chat questions from the notes only.", "ui_chat", "LOOKUP_SYS"),
    ("chat_fix", "gpt", "Chat: restate a correction", "Restates a /fix correction before it is filed.", "ui_chat", "FIX_SYS"),
    ("publish_judge", "gpt", "Publish judge", "Decides whether a note is published to the shared repository.", "publisher", "PUBLISH_SYS"),
    ("curator_intro", "cur", "Note review: intro", "Opens every note-review prompt.", None, None),
    ("curator_verify", "cur", "Note review: verify facts", "Added when 'Verify facts' is on.", None, None),
    ("curator_connect", "cur", "Note review: find connections", "Added when 'Find new connections' is on.", None, None),
    ("curator_prune", "cur", "Note review: prune", "Added when 'Prune' is on.", None, None),
    ("curator_format", "cur", "Note review: answer format", "Closes every note-review prompt. Keep the JSON format.", None, None),
    ("map_pass", "cur", "Map pass", "Reads the whole vault and proposes links and hub notes.", "mapper", "SYS"),
]
BY = {r[0]: r for r in REG}
_defaults = {}


def sha(t):
    return hashlib.sha1(t.encode()).hexdigest()[:16]


def default_of(pid):
    if pid in _defaults:
        return _defaults[pid]
    r = BY[pid]
    try:
        text = LOCAL[pid] if r[4] is None else getattr(importlib.import_module(r[4]), r[5])
        _defaults[pid] = text if isinstance(text, str) else None
    except Exception:
        _defaults[pid] = None
    return _defaults[pid]


def overrides():
    out = {}
    for p in DIR.glob("*.txt") if DIR.is_dir() else []:
        if p.stem in BY:
            try:
                t = p.read_text(encoding="utf-8")
            except OSError:
                continue
            if t.strip():
                out[p.stem] = t
    return out


def current(pid):
    return overrides().get(pid) or default_of(pid) or ""


def versions(pid):
    d = HIST / pid
    return sorted(d.glob("*.txt")) if d.is_dir() else []


def version(pid):
    v = versions(pid)
    return int(v[-1].name.split("-")[0]) if v else 0


def required(pid):
    d = default_of(pid)
    return sorted(set(KEYS.findall(d))) if d else []


def apply(body, label=""):
    """Swap in an edited system prompt. Returns (body, a note for the call log)."""
    ov = overrides()
    if not ov:
        return body, ""
    msgs = body.get("messages") or []
    if not msgs or msgs[0].get("role") != "system" or not isinstance(msgs[0].get("content"), str):
        return body, ""
    h = sha(msgs[0]["content"])
    for pid, text in ov.items():
        d = default_of(pid)
        if d is not None and sha(d) == h:
            new = dict(body, messages=[dict(msgs[0], content=text)] + list(msgs[1:]))
            return new, f" [edited prompt: {BY[pid][2]} v{version(pid)}]"
    if label == "curate":
        parts = [BY[p][2] for p in ov if p.startswith("curator_")]
        if parts:
            return body, " [edited note-review prompt: " + ", ".join(parts) + "]"
    return body, ""


def curator_system(cfg, fallback):
    try:
        jobs = [current(k).strip() for k, on in (("curator_verify", cfg["verify"]), ("curator_connect", cfg["connect"]),
                                                 ("curator_prune", cfg["prune"])) if on]
        return current("curator_intro").strip() + " " + " ".join(jobs) + " " + current("curator_format").strip()
    except Exception:
        return fallback(cfg)


def _meta():
    try:
        return json.loads(META.read_text())
    except (OSError, ValueError):
        return {}


def save(pid, text, skip_check=False):
    if pid not in BY:
        raise ValueError("unknown prompt")
    text = text.strip()
    if not text:
        raise ValueError("a prompt cannot be empty; use Reset to go back to the default")
    if len(text) > MAX_CHARS:
        raise ValueError(f"too long ({len(text):,} characters, limit {MAX_CHARS:,})")
    d = default_of(pid)
    if d is not None and text == d.strip():
        return reset(pid)
    if not skip_check:
        miss = [k for k in required(pid) if f'"{k}"' not in text]
        if miss:
            raise ValueError("the answer format needs these keys, still missing: " + ", ".join(miss) +
                             ". Add them back, or tick 'Skip the format check' to save anyway.")
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / f".{pid}.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, DIR / f"{pid}.txt")
    (HIST / pid).mkdir(parents=True, exist_ok=True)
    n = version(pid) + 1
    (HIST / pid / f"{n:04d}-{int(time.time())}.txt").write_text(text, encoding="utf-8")
    for old in versions(pid)[:-KEEP]:
        old.unlink(missing_ok=True)
    m = _meta()
    m[pid] = sha(d) if d else ""
    META.write_text(json.dumps(m))


def reset(pid):
    if pid not in BY:
        raise ValueError("unknown prompt")
    (DIR / f"{pid}.txt").unlink(missing_ok=True)
    m = _meta()
    m.pop(pid, None)
    META.write_text(json.dumps(m))


def listing():
    ov, m, out = overrides(), _meta(), []
    for pid, model, title, when, _, _ in REG:
        d = default_of(pid)
        hist = []
        for p in reversed(versions(pid)):
            try:
                hist.append({"n": int(p.name.split("-")[0]), "at": int(p.stem.split("-")[1]), "text": p.read_text(encoding="utf-8")})
            except (OSError, ValueError, IndexError):
                pass
        out.append({"id": pid, "model": model, "title": title, "when": when, "default": d or "", "missing": d is None,
                    "text": ov.get(pid) or d or "", "edited": pid in ov, "version": version(pid),
                    "stale": bool(pid in ov and d and m.get(pid) and m[pid] != sha(d)),
                    "required": required(pid), "history": hist})
    return out
