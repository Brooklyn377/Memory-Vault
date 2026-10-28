"""Chunking with stable boundaries. A chat is a list of atoms (message pieces); a chunk is a run of atoms.
Chunks already read are found again by matching their stored hashes, so changing the chunk size only re-cuts what is unread."""
import hashlib


def _h(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def atoms(body, split_messages, split_big, max_single):
    out = []
    for n, (stamp, text) in enumerate(split_messages(body), start=1):
        parts = split_big(text, max_single) if len(text) > max_single else [text]
        for k, part in enumerate(parts):
            out.append((n, stamp[:10], f"(continued)\n{part}" if k else part))
    return out


def group(items, size):
    out, cur, used = [], [], 0
    for a in items:
        if cur and used + len(a[2]) > size:
            out.append(cur)
            cur, used = [], 0
        cur.append(a)
        used += len(a[2])
    if cur:
        out.append(cur)
    return out


def chunk(g):
    c = {"text": "\n\n".join(a[2] for a in g), "first": (g[0][0], g[0][1]), "last": g[-1][0], "n_atoms": len(g)}
    c["start_msg"], c["date"] = c["first"]
    c["hash"] = _h(c["text"])
    return c


def frozen(items, d):
    """Atom counts of the chunks already read: for each stored hash, find the run of atoms that produces it."""
    if not d or not d.get("hashes"):
        return []
    out, pos = [], 0
    for h in d["hashes"]:
        text, found = "", 0
        for n in range(1, len(items) - pos + 1):
            text += ("\n\n" if n > 1 else "") + items[pos + n - 1][2]
            if _h(text) == h:
                found = n
                break
        if not found:
            break
        out.append(found)
        pos += found
    return out


def make_chunks(body, d, size, split_messages, split_big, max_single):
    items = atoms(body, split_messages, split_big, max_single)
    out, pos = [], 0
    for n in frozen(items, d):
        out.append(items[pos:pos + n])
        pos += n
    out += group(items[pos:], size)
    return [chunk(g) for g in out]
