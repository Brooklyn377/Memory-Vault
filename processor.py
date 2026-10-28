import hashlib
import json
import re
import shutil
import sys
import time
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path

import yaml

import config
from scrub import scrub

CFG = config.load()
STATE = Path(CFG["paths"]["state_dir"])
VAULT = Path(CFG["paths"]["vault"])
INBOX = STATE / "inbox"
PROCESSED = STATE / "processed"
FAILED = STATE / "failed"
HASHES = STATE / "hashes.json"
OUT_DIR = VAULT / "raw" / "conversations"
MEM_DIR = VAULT / "raw" / "claude-memory"
KEEP_DAYS = 7
POLL_SECONDS = 10
MAX_UNZIPPED = 4 * 1024 ** 3


def log(msg):
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "activity.log").open("a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def load_hashes():
    if HASHES.exists():
        return json.loads(HASHES.read_text())
    return {}


def save_hashes(h):
    tmp = HASHES.with_suffix(".tmp")
    tmp.write_text(json.dumps(h, indent=1))
    tmp.replace(HASHES)


def slugify(text, limit=50):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].strip("-") or "untitled"


def message_text(m):
    parts = []
    content = m.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                parts.append(block["text"])
    if not parts and m.get("text"):
        parts.append(m["text"])
    for key, label in (("attachments", "attachment"), ("files", "file")):
        for a in m.get(key) or []:
            if isinstance(a, dict) and a.get("file_name"):
                parts.append(f"[{label}: {a['file_name']}]")
    return "\n\n".join(parts).strip()


def render(conv):
    """Return (conv_id, digest, markdown, redactions, created, title, updated_full) or None."""
    title = str(conv.get("name") or conv.get("title") or "Untitled").strip() or "Untitled"
    created = str(conv.get("created_at") or "")[:10] or "unknown"
    updated = str(conv.get("updated_at") or "")[:10] or created
    conv_id = str(conv.get("uuid") or conv.get("id") or "")
    if not conv_id:
        conv_id = hashlib.sha1((title + created).encode()).hexdigest()[:32]

    raw_msgs, sections, redactions = [], [], 0
    for m in conv.get("chat_messages") or conv.get("messages") or []:
        if not isinstance(m, dict):
            continue
        text = message_text(m)
        if not text:
            continue
        sender = str(m.get("sender") or m.get("role") or "").lower()
        who = "Human" if sender in ("human", "user") else "Assistant"
        stamp = str(m.get("created_at") or "")[:19].replace("T", " ")
        raw_msgs.append([who, stamp, text])
        clean, counts = scrub(text)
        redactions += sum(counts.values())
        clean = clean.replace("[[", "[ [")
        head = f"### {who}" + (f" ({stamp})" if stamp else "")
        sections.append(f"{head}\n\n{clean}\n")
    if not sections:
        return None

    digest = hashlib.sha256(
        json.dumps([title, raw_msgs], ensure_ascii=False).encode()).hexdigest()
    clean_title, tcounts = scrub(title)
    redactions += sum(tcounts.values())
    clean_title = clean_title.replace("[[", "[ [")
    fm = {
        "type": "conversation",
        "source_id": conv_id,
        "title": clean_title,
        "created": created,
        "updated": updated,
        "messages": len(sections),
        "imported": f"{datetime.now():%Y-%m-%d}",
    }
    md = ("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n"
          + f"# {clean_title}\n\n" + "\n".join(sections))
    updated_full = str(conv.get("updated_at") or conv.get("created_at") or "")
    return conv_id, digest, md, redactions, created, clean_title, updated_full


def process_conversations(convs):
    hashes = load_hashes()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stats = {"new": 0, "updated": 0, "unchanged": 0, "stale": 0,
             "skipped": 0, "redactions": 0}
    for conv in convs:
        r = render(conv) if isinstance(conv, dict) else None
        if r is None:
            stats["skipped"] += 1
            continue
        conv_id, digest, md, redactions, created, title, updated_full = r
        entry = hashes.get(conv_id)
        if entry and entry.get("hash") == digest:
            stats["unchanged"] += 1
            continue
        if entry and updated_full and entry.get("updated", "") > updated_full:
            stats["stale"] += 1
            continue
        if entry and Path(entry["file"]).name == entry["file"]:
            name, status = entry["file"], "updated"
        else:
            name, status = f"{created}-{slugify(title)}-{conv_id[:8]}.md", "new"
        (OUT_DIR / name).write_text(md, encoding="utf-8")
        hashes[conv_id] = {"hash": digest, "file": name, "updated": updated_full}
        stats[status] += 1
        stats["redactions"] += redactions
    save_hashes(hashes)
    return stats


def load_export(path):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith("conversations.json")]
            if not names:
                raise ValueError("no conversations.json in zip (contains: " + ", ".join(z.namelist()[:5]) + ")")
            if z.getinfo(names[0]).file_size > MAX_UNZIPPED:
                raise ValueError("conversations.json too large")
            with z.open(names[0]) as f:
                data = json.load(f)
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("conversations") or [data]
    if not isinstance(data, list):
        raise ValueError("unexpected JSON shape")
    return data

FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


def memory_members(path):
    if path.suffix.lower() != ".zip":
        return None
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if any(n.lower().endswith("conversations.json") for n in names):
            return None
        md = [n for n in names if n.lower().endswith(".md") and not n.endswith("/")]
        return md or None


def safe_rel(name):
    parts = [x for x in name.split("/") if x not in ("", ".")]
    if not parts or any(x == ".." for x in parts) or name.startswith("/"):
        return None
    if len(parts) > 1:
        parts = parts[1:]
    parts = [re.sub(r"[^\w .-]", "_", x) for x in parts]
    return "/".join(parts)


def process_memory_export(path):
    hashes = load_hashes()
    stats = {"new": 0, "updated": 0, "unchanged": 0, "skipped": 0, "redactions": 0}
    with zipfile.ZipFile(path) as z:
        for name in memory_members(path):
            rel = safe_rel(name)
            info = z.getinfo(name)
            if rel is None or info.file_size > 5 * 1024 * 1024:
                stats["skipped"] += 1
                continue
            raw = z.read(name).decode("utf-8", errors="replace")
            key = "mem:" + rel
            digest = hashlib.sha256(raw.encode()).hexdigest()
            entry = hashes.get(key)
            if entry and entry.get("hash") == digest:
                stats["unchanged"] += 1
                continue
            m = FM_RE.match(raw)
            orig = {}
            if m:
                try:
                    orig = yaml.safe_load(m.group(1)) or {}
                except yaml.YAMLError:
                    orig = {}
                if not isinstance(orig, dict):
                    orig = {}
            body = raw[m.end():] if m else raw
            clean, counts = scrub(body)
            clean = clean.replace("[[", "[ [")
            fm = {"type": "claude-memory", "source": "claude-memory-export",
                  "source_path": rel, "imported": f"{datetime.now():%Y-%m-%d}"}
            if orig.get("description"):
                d, dc = scrub(str(orig["description"]))
                fm["description"] = d.replace("[[", "[ [")
                stats["redactions"] += sum(dc.values())
            out = MEM_DIR / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
                           + "---\n\n" + clean, encoding="utf-8")
            hashes[key] = {"hash": digest, "file": rel}
            stats["updated" if entry else "new"] += 1
            stats["redactions"] += sum(counts.values())
    save_hashes(hashes)
    return stats


def handle_file(path):
    try:
        if memory_members(path):
            st = process_memory_export(path)
            log(f"import memory-export {path.name}: new {st['new']}, updated {st['updated']}, "
                f"unchanged {st['unchanged']}, skipped {st['skipped']}, "
                f"redactions {st['redactions']}")
            dest = PROCESSED
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest / path.name))
            return
        stats = process_conversations(load_export(path))
        log(f"import {path.name}: new {stats['new']}, updated {stats['updated']}, "
            f"unchanged {stats['unchanged']}, stale {stats['stale']}, "
            f"skipped {stats['skipped']}, redactions {stats['redactions']}")
        dest = PROCESSED
    except Exception as e:
        log(f"import FAILED {path.name}: {type(e).__name__}: {str(e)[:200]}")
        dest = FAILED
    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest / path.name))


def purge_old():
    if not PROCESSED.exists():
        return
    cutoff = time.time() - KEEP_DAYS * 86400
    for p in PROCESSED.iterdir():
        if p.is_file() and p.stat().st_mtime < cutoff:
            p.unlink()
            log(f"purged {p.name}")


def ready_files(sizes, wait_for_stable=True):
    out, now_sizes = [], {}
    if INBOX.exists():
        for p in sorted(INBOX.iterdir()):
            if not p.is_file() or p.name.endswith(".part"):
                continue
            size = p.stat().st_size
            now_sizes[p] = size
            if not wait_for_stable or sizes.get(p) == size:
                out.append(p)
    return out, now_sizes


def main():
    once = "--once" in sys.argv
    if once:
        files, _ = ready_files({}, wait_for_stable=False)
        for p in files:
            handle_file(p)
        purge_old()
        return
    log("importer started")
    sizes = {}
    while True:
        files, sizes = ready_files(sizes)
        for p in files:
            handle_file(p)
        purge_old()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
