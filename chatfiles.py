"""Files and images attached to a chat message.
  state/chat_uploads/<id>/<name>        where an upload waits, and where it stays with its message (shown in the chat)
  raw/files/<name>/<message id>.<ext>   the original, kept in the vault like the files saved from chats (not for images)
The Assistant gets a text file's whole text in the message; an image only when its server
can look at images (the server says so: /props, modalities, vision)."""
import base64
import json
import mimetypes
import re
import secrets
import shutil
import time
import urllib.request
from pathlib import Path

import agent

UP = agent.STATE / "chat_uploads"
ORIG = agent.VAULT / "raw" / "files"
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_VISION = {"t": 0.0, "v": False}
_SWEPT = [0.0]
ORPHAN_SECONDS = 24 * 3600   # an upload that was never sent is removed after this long


def clean_name(name):
    base = str(name or "").replace("\\", "/").split("/")[-1]
    base = re.sub(r"[^\w.\- ]", "_", base).strip(" .")
    if len(base) > 120:
        stem, dot, ext = base.rpartition(".")
        base = (stem[: 115 - len(ext)] + dot + ext) if dot and len(ext) <= 8 else base[:120]
    return base or "file"


def is_image(name):
    return Path(name).suffix.lower() in IMAGES


def new_upload(name):
    """-> (id, the path to write the file to)."""
    uid = secrets.token_hex(6)
    d = UP / uid
    d.mkdir(parents=True, exist_ok=True)
    return uid, d / clean_name(name)


def info(uid):
    """The upload with this id: {"id", "name", "size", "image"}, or None."""
    if not re.fullmatch(r"[0-9a-f]{12}", str(uid)):
        return None
    d = UP / uid
    files = [p for p in d.iterdir() if p.is_file()] if d.is_dir() else []
    if len(files) != 1:
        return None
    return {"id": uid, "name": files[0].name, "size": files[0].stat().st_size, "image": is_image(files[0].name)}


def path_of(uid, name):
    i = info(uid)
    return UP / uid / i["name"] if i and i["name"] == name else None


def discard(uid):
    if re.fullmatch(r"[0-9a-f]{12}", str(uid)):
        shutil.rmtree(UP / uid, ignore_errors=True)


def attach(ids, message_id):
    """The files for a message that is being sent: -> [{"id", "name", "size", "image", "vault"}]. Each non-image file is also
    copied to the vault's originals (read-only, never changed)."""
    out = []
    for uid in ids or []:
        i = info(uid)
        if not i:
            raise ValueError("an attached file is no longer there; attach it again")
        i["vault"] = ""
        if not i["image"]:
            ext = Path(i["name"]).suffix
            dest = ORIG / i["name"] / (message_id + (ext + ".txt" if ext.lower() == ".md" else ext))   # a .md would be read as a vault note
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(UP / uid / i["name"], dest)
            dest.chmod(0o444)
            i["vault"] = str(dest.relative_to(agent.VAULT))
        out.append(i)
    return out


def remove(files):
    for f in files or []:
        discard(f.get("id"))


def sweep(referenced):
    """Delete uploads that were never sent. referenced(): the ids of uploads that belong to a message. Looks every 10 minutes."""
    if time.time() - _SWEPT[0] < 600 or not UP.is_dir():
        return
    _SWEPT[0] = time.time()
    keep = referenced()
    for d in UP.iterdir():
        if d.is_dir() and d.name not in keep and time.time() - d.stat().st_mtime > ORPHAN_SECONDS:
            shutil.rmtree(d, ignore_errors=True)


def read_text(p):
    """The file's text, or None if it is not text."""
    try:
        raw = p.read_bytes()
        if b"\0" in raw[:8192]:
            return None
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def vision():
    """True when the Assistant's server says it can look at images. Asked again after 20 seconds."""
    if time.time() - _VISION["t"] < 20:
        return _VISION["v"]
    ok = False
    try:
        import applier
        import remote
        ext = remote.enabled("pa")
        base = remote.base("pa") if ext else f"http://127.0.0.1:{applier.PORT['pa']}"
        req = urllib.request.Request(base.rstrip("/") + "/props")
        key = remote.saved_key("pa") if ext else ""
        if key:
            req.add_header("Authorization", "Bearer " + key)
        with urllib.request.urlopen(req, timeout=3) as r:
            ok = bool((json.load(r).get("modalities") or {}).get("vision"))
    except Exception:
        ok = False
    _VISION.update(t=time.time(), v=ok)
    return ok


def context(files, can_see):
    """What the Assistant is told about the attachments: -> (text to add to the message, [data URIs of the images])."""
    parts, images = [], []
    for f in files or []:
        p = UP / f["id"] / f["name"]
        saved = f" The original is saved at {f['vault']}." if f.get("vault") else ""
        if f.get("image"):
            if can_see and p.is_file():
                mime = mimetypes.guess_type(f["name"])[0] or "image/png"
                images.append(f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode())
                parts.append(f'ATTACHED IMAGE "{f["name"]}" (shown to you with this message).')
            else:
                parts.append(f'ATTACHED IMAGE "{f["name"]}": you cannot see images.')
            continue
        text = read_text(p)
        if text is None:
            parts.append(f'ATTACHED FILE "{f["name"]}", {f["size"]:,} bytes, is not text, so you cannot read it.{saved}')
        else:
            parts.append(f'ATTACHED FILE "{f["name"]}":{saved}\n{text}')
    return "\n\n".join(parts), images


def names(m):
    """For the saved conversation and the Assistant's memory of this message."""
    n = [f["name"] for f in m.get("files") or []]
    return ("\n[attached: " + ", ".join(n) + "]") if n else ""
