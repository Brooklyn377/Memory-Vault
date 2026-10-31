"""Save files Claude created, exactly as written, plus one note per file.
  raw/files/<name>/<stamp>-<chat>.<ext>   original versions: written once, read-only, never scrubbed
  files/derived/<name>/                   copy + changes.json, made by filechanges.py
  files/<name>.md                         a note ABOUT the file, type: file-note, linking to the saved originals and the chats
Never saves .md files. Dry run unless --write."""
import glob
import hashlib
import json
import os
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import agent
from scrub import scrub

VAULT = agent.VAULT
FILES = VAULT / "files"
ORIG = VAULT / "raw" / "files"
DER = FILES / "derived"
MAX_BYTES = 2 * 1024 * 1024
HEREDOC = re.compile(r"cat\s+>\s*(\S+)\s*<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\2[ \t]*(?:\n|$)", re.S)
NAME = re.compile(r"[\w.-]{1,80}\.([A-Za-z0-9]{1,6})$")


def good_name(path):
    base = str(path).strip().strip("'\"").replace("\\", "/").split("/")[-1]
    m = NAME.match(base)
    return base if m and m.group(1).lower() != "md" else None


def from_block(b):
    """Yield (filename, text) for each file a tool_use block wrote."""
    if b.get("type") != "tool_use":
        return
    i = b.get("input") or {}
    if isinstance(i.get("command"), str):
        for path, _, body in HEREDOC.findall(i["command"]):
            n = good_name(path)
            if n:
                yield n, body + "\n"
    path = i.get("file_path") or i.get("path")
    text = i.get("content") if isinstance(i.get("content"), str) else i.get("file_text")
    if path and isinstance(text, str):
        n = good_name(path)
        if n:
            yield n, text


def collect():
    found = {}
    for z in sorted(glob.glob(str(agent.STATE / "*" / "*.zip"))):
        try:
            zf = zipfile.ZipFile(z)
            names = [n for n in zf.namelist() if n.endswith("conversations.json")]
            if not names:
                continue
            convs = json.loads(zf.read(names[0]))
        except (OSError, ValueError, zipfile.BadZipFile):
            continue
        for c in convs:
            for m in c.get("chat_messages", []):
                when = str(m.get("created_at") or c.get("created_at") or "")
                for b in m.get("content") or []:
                    if isinstance(b, dict):
                        for name, text in from_block(b):
                            found.setdefault(name, {})[(when, text)] = str(c.get("uuid", ""))[:8]
    return found


def raw_chats():
    return {p.stem: p.read_text(encoding="utf-8", errors="replace").lower()
            for p in (VAULT / "raw" / "conversations").glob("*.md")}


def stem_for(short, raws):
    return next((s for s in raws if short and s.endswith(short)), None)


def _date(p):
    return f"{p.name[:4]}-{p.name[4:6]}-{p.name[6:8]}"


def write_note(name, raws=None):
    d = ORIG / name
    vers = sorted(d.glob("*")) if d.is_dir() else []
    if not vers:
        return
    raws = raws if raws is not None else raw_chats()
    made, links = set(), []
    for p in vers:
        st = stem_for(p.stem.split("-", 1)[1], raws)
        if st:
            made.add(st)
            links.append(f"- [[raw/conversations/{st}]] ({_date(p)})")
    low = name.lower()
    said = [s for s, t in raws.items() if low in t and s not in made][:20]
    cj = DER / name / "changes.json"
    ch = json.loads(cj.read_text()) if cj.exists() else {}
    applied, unapplied = ch.get("applied", []), ch.get("unapplied", [])
    text = vers[-1].read_text(encoding="utf-8", errors="replace")
    first = next((l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#!")), "")
    body = (f"---\ntype: file-note\ntitle: {json.dumps('Notes about ' + name)}\nconfidence: high\nfilename: {json.dumps(name)}\n"
            f"versions: {len(vers)}\nupdated: {datetime.now():%Y-%m-%d}\n---\n# Notes about {name}\n\n"
            f"The file itself (read-only, {len(text)} characters, latest of {len(vers)} version(s)): "
            f"[[raw/files/{name}/{vers[-1].name}]]\nStarts with: `{first[:100]}`\n")
    if len(vers) > 1:
        body += "\nEarlier versions: " + ", ".join(f"[[raw/files/{name}/{p.name}]]" for p in vers[:-1]) + "\n"
    if (DER / name / name).exists():
        body += f"Latest derived copy: `files/derived/{name}/{name}` ({len(applied)} change(s) applied).\n"
    if applied:
        body += "\n## Changes made to the derived copy\n" + "\n".join(
            f"- [{c['confidence']} confidence] {c['what']} - \"{c['quote'][:120]}\" ({c['at'][:10]})" for c in applied) + "\n"
    if unapplied:
        body += "\n## Changes found but not applied (need a manual look)\n" + "\n".join(
            f"- {c['what']} - {c['why']}" for c in unapplied) + "\n"
    if ch.get("skipped"):
        body += "\n## Skipped by you\n" + "\n".join(f"- {c['what']} ({c['at'][:10]})" for c in ch["skipped"]) + "\n"
    body += "\n## Created or changed in\n" + ("\n".join(links) or "- (chat note not found)") + "\n"
    if said:
        body += "\n## Mentioned in\n" + "\n".join(f"- [[raw/conversations/{s}]]" for s in said) + "\n"
    FILES.mkdir(parents=True, exist_ok=True)
    (FILES / f"{name}.md").write_text(scrub(body)[0], encoding="utf-8")


def main(write):
    found, raws = collect(), raw_chats()
    for name in sorted(found):
        d = ORIG / name
        have = {hashlib.sha1(p.read_bytes()).hexdigest() for p in d.glob("*")} if d.is_dir() else set()
        added = 0
        for (when, text), short in sorted(found[name].items()):
            if len(text.encode()) > MAX_BYTES:
                continue
            h = hashlib.sha1(text.encode()).hexdigest()
            if h in have:
                continue
            have.add(h)
            added += 1
            if not write:
                continue
            d.mkdir(parents=True, exist_ok=True)
            stamp = re.sub(r"\D", "", when)[:14] or "00000000000000"
            path = d / f"{stamp}-{short}{Path(name).suffix}"
            if not path.exists():
                path.write_text(text, encoding="utf-8")
                os.chmod(path, 0o444)
            cj = DER / name / "changes.json"
            if cj.exists():  # a newer original replaces the old change log
                cj.rename(cj.with_name(f"changes.{datetime.now():%Y%m%d%H%M%S}.json"))
                (DER / name / name).unlink(missing_ok=True)
        print(f"{name}: {added} new version(s)")
        if write:
            write_note(name, raws)
    print("done" if write else "dry run, nothing written")


def migrate():
    """Notes written before files were told apart from notes about them were labelled type: file; relabel them."""
    for d in sorted(ORIG.iterdir()) if ORIG.is_dir() else []:
        note = FILES / f"{d.name}.md"
        if d.is_dir() and note.exists() and "\ntype: file\n" in note.read_text(encoding="utf-8", errors="replace")[:300]:
            write_note(d.name)


def auto():
    """Run main(write=True) only when the set of export zips has changed since last time."""
    migrate()
    sig = sorted(f"{z}:{os.path.getsize(z)}" for z in glob.glob(str(agent.STATE / "*" / "*.zip")))
    mark = agent.STATE / "filestore_seen.json"
    try:
        if json.loads(mark.read_text()) == sig:
            return
    except (OSError, ValueError):
        pass
    main(True)
    mark.write_text(json.dumps(sig))


if __name__ == "__main__":
    main("--write" in sys.argv)
