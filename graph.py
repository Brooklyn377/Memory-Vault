import argparse
import json
import re
import sys
from collections import deque
from pathlib import Path

import yaml

SKIP_DIRS = {".git", ".obsidian", "node_modules"}
LINK_RE = re.compile(r"\[\[([^\]\|#]+?)(?:#[^\]\|]*)?(?:\|[^\]]*)?\]\]")
FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
INLINE_RE = re.compile(r"`[^`\n]*`")
FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


def as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


class Vault:
    def __init__(self, root):
        self.root = Path(root)
        self.body = {}
        self.meta = {}
        self.by_name = {}
        self.stems = {}
        self.out = {}
        self.back = {}
        self.unresolved = {}
        self.files = set()       # the saved original files under raw/files: not notes, but links to them are followed
        self.file_out = {}       # note -> the files it links to
        self.file_back = {}      # file -> the notes that link to it
        self._load()

    def _load(self):
        fm_raw = {}
        for p in sorted(self.root.rglob("*.md")):
            rel = p.relative_to(self.root)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            rel = rel.as_posix()
            raw = p.read_text(encoding="utf-8", errors="replace")
            m = FM_RE.match(raw)
            meta = {}
            if m:
                try:
                    meta = yaml.safe_load(m.group(1)) or {}
                except yaml.YAMLError:
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
            self.body[rel] = raw[m.end():] if m else raw
            self.meta[rel] = meta
            fm_raw[rel] = m.group(1) if m else ""
            self.stems.setdefault(Path(rel).stem.lower(), []).append(rel)
            names = {Path(rel).stem.lower()}
            names.update(a.lower() for a in as_list(meta.get("aliases")))
            names.update(a.lower() for a in as_list(meta.get("alias")))
            for n in names:
                self.by_name.setdefault(n, [])
                if rel not in self.by_name[n]:
                    self.by_name[n].append(rel)
        raw_files = self.root / "raw" / "files"
        if raw_files.is_dir():
            self.files = {p.relative_to(self.root).as_posix() for p in raw_files.rglob("*") if p.is_file()}
        for rel in self.body:
            self.out[rel] = set()
            self.back.setdefault(rel, set())
            self.unresolved[rel] = set()
        for rel in self.body:
            text = INLINE_RE.sub("", FENCE_RE.sub("", self.body[rel]))
            targets = LINK_RE.findall(fm_raw[rel] + "\n" + text)
            for t in targets:
                dest = self.resolve(t)
                if dest is None and (fdest := self.resolve_file(t)):
                    self.file_out.setdefault(rel, set()).add(fdest)
                    self.file_back.setdefault(fdest, set()).add(rel)
                elif dest is None:
                    self.unresolved[rel].add(t.strip())
                elif dest != rel:
                    self.out[rel].add(dest)
                    self.back[dest].add(rel)

    def resolve(self, target):
        key = target.strip().lower()
        if key.endswith(".md"):
            key = key[:-3]
        for k in (key, key.replace(" ", "-")):
            if "/" in k:
                found = [r for r in self.body
                         if r[:-3].lower() == k or r[:-3].lower().endswith("/" + k)]
            else:
                found = self.by_name.get(k, [])
            if found:
                return sorted(found, key=lambda r: (len(r), r))[0]
        return None

    def resolve_file(self, target):
        """A link to a saved original, written as its path under raw/files (or the end of it)."""
        key = target.strip().lower().lstrip("/")
        for f in self.files:
            if f.lower() == key or f.lower().endswith("/" + key):
                return f
        return None

    def find(self, name):
        return name if name in self.body else self.resolve(name)

    def title(self, rel):
        return str(self.meta[rel].get("title") or Path(rel).stem)

    def names_for(self, rel):
        stem = Path(rel).stem
        names = {stem, stem.replace("-", " "), self.title(rel)}
        names.update(as_list(self.meta[rel].get("aliases")))
        return sorted({n for n in names if len(n) >= 3}, key=len, reverse=True)

    def snippet(self, text, match, width=70):
        a, b = max(0, match.start() - width), min(len(text), match.end() + width)
        return re.sub(r"\s+", " ", text[a:b]).strip()

    def find_mentions(self, terms, exclude=(), limit=50):
        if not terms:
            return []
        rx = re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(t) for t in terms)
                        + r")(?![\w-])", re.IGNORECASE)
        hits = []
        for rel, text in self.body.items():
            if rel in exclude:
                continue
            m = rx.search(text)
            if m:
                hits.append({"path": rel, "snippet": self.snippet(text, m)})
                if len(hits) >= limit:
                    break
        return hits

    def hop_distances(self, rel, hops):
        dist, queue = {rel: 0}, deque([rel])
        while queue:
            cur = queue.popleft()
            if dist[cur] >= hops:
                continue
            for nxt in self.out[cur] | self.back[cur]:
                if nxt not in dist:
                    dist[nxt] = dist[cur] + 1
                    queue.append(nxt)
        dist.pop(rel)
        return dist

    def neighborhood(self, rel, hops=1, mention_limit=50):
        linkers = self.back[rel] | {rel}
        return {
            "note": rel,
            "title": self.title(rel),
            "aliases": as_list(self.meta[rel].get("aliases")),
            "outgoing": sorted(self.out[rel]),
            "backlinks": sorted(self.back[rel]),
            "broken_links": sorted(self.unresolved[rel]),
            "unlinked_mentions": self.find_mentions(
                self.names_for(rel), exclude=linkers, limit=mention_limit),
            "within_hops": dict(sorted(self.hop_distances(rel, hops).items(),
                                       key=lambda kv: (kv[1], kv[0]))),
        }

    def duplicates(self):
        return {s: sorted(p) for s, p in self.stems.items() if len(p) > 1}

    def stats(self):
        links = sum(len(v) for v in self.out.values())
        broken = sum(len(v) for v in self.unresolved.values())
        orphans = [r for r in self.body if not self.out[r] and not self.back[r]]
        return {"notes": len(self.body), "links": links,
                "broken_links": broken, "orphans": len(orphans),
                "duplicate_names": len(self.duplicates())}


def show(obj, as_json):
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False))
        return
    if isinstance(obj, dict) and "note" in obj:
        print(f"{obj['note']}  ({obj['title']})")
        if obj["aliases"]:
            print("aliases:", ", ".join(obj["aliases"]))
        print("\nlinks out:")
        for p in obj["outgoing"] or ["-"]:
            print("  ", p)
        print("\nbacklinks:")
        for p in obj["backlinks"] or ["-"]:
            print("  ", p)
        print("\nbroken links:")
        for p in obj["broken_links"] or ["-"]:
            print("  ", p)
        print("\nunlinked mentions:")
        if not obj["unlinked_mentions"]:
            print("   -")
        for h in obj["unlinked_mentions"]:
            print(f"   {h['path']}: ...{h['snippet']}...")
        print("\nwithin hops:")
        for p, d in obj["within_hops"].items():
            print(f"   {d}  {p}")
    else:
        print(json.dumps(obj, indent=2, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description="Wikilink graph for a markdown vault")
    ap.add_argument("vault")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    sub.add_parser("broken")
    sub.add_parser("orphans")
    sub.add_parser("duplicates")
    n = sub.add_parser("neighbors")
    n.add_argument("name")
    n.add_argument("--hops", type=int, default=1)
    n.add_argument("--json", action="store_true")
    m = sub.add_parser("mentions")
    m.add_argument("term")
    m.add_argument("--json", action="store_true")
    args = ap.parse_args()

    v = Vault(args.vault)
    if args.cmd == "stats":
        show(v.stats(), True)
    elif args.cmd == "broken":
        show({r: sorted(t) for r, t in v.unresolved.items() if t}, True)
    elif args.cmd == "orphans":
        show([r for r in v.body if not v.out[r] and not v.back[r]], True)
    elif args.cmd == "duplicates":
        show(v.duplicates(), True)
    elif args.cmd == "neighbors":
        rel = v.find(args.name)
        if rel is None:
            print(f"no note matches '{args.name}'", file=sys.stderr)
            sys.exit(1)
        show(v.neighborhood(rel, args.hops), args.json)
    elif args.cmd == "mentions":
        hits = v.find_mentions([args.term])
        if args.json:
            show(hits, True)
        else:
            for h in hits or []:
                print(f"{h['path']}: ...{h['snippet']}...")
            if not hits:
                print("no mentions")


if __name__ == "__main__":
    main()
