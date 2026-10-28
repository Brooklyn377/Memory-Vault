"""Publish gate: reviewer decides which notes go to the vault repository, then opens (and optionally merges) ONE PR.
  publisher.py [--dry]
"""
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

import agent
import commit
import hygiene
import reviewer
from scrub import scrub

VAULT, STATE, G = agent.VAULT, agent.STATE, commit.G
P = agent.CFG.get("publish", {})
DECISIONS = STATE / "publish_decisions.json"
MAX_BYTES = 200 * 1024
TOP_LEVEL_OK = (".yaml", ".yml", ".toml", ".conf", ".ini")

PUBLISH_SYS = (
    "You decide whether one note from a person's private notes vault belongs in the shared notes repository. "
    "Publish notes that record something specific and useful: a named device, service, project, decision or task with real "
    "content. Keep local (do not publish) notes that are trivial, generic, near-empty or only a stray word. "
    'Reply with ONLY JSON: {"decision":"publish|local","reason":"<one short sentence>"}.')


def classify(rel, text):
    top = rel.split("/")[0]
    if top in ("raw", "inbox", ".git") or top not in ("summaries", "timelines", "entities", "tasks") and "/" in rel:
        return "never", "not a generated-notes folder"
    if "/" not in rel:
        name = rel.lower()
        if name.startswith("readme") or name.endswith(TOP_LEVEL_OK) or name in (".gitignore",):
            return "always", "readme or config file"
        return "never", "unrecognized top-level file"
    if top in ("summaries", "timelines"):
        return "always", f"{top[:-1]} note"
    if top == "entities":
        name = Path(rel).stem
        docs = len(re.findall(r"^- \[\[", text, re.M))
        has_tl = (VAULT / "timelines" / f"{name}.md").exists()
        kept = hygiene.slug(name).lower() in hygiene._load(hygiene.KEEP)
        if not kept and (hygiene.reason(name, docs, has_tl) or (docs < 2 and not has_tl)):
            return "local", "one-off or noise entity"
        return "review", "entity worth a closer look"
    return "review", "task note"


def gates(rel, data):
    if len(data) > MAX_BYTES:
        return "file larger than 200 KB"
    text = data.decode("utf-8", errors="replace")
    if scrub(text)[0] != text:
        return "contains secret-looking text"
    return None


def verdict_from_reviewer(rel, text, cache):
    h = hashlib.sha1(text.encode()).hexdigest()
    data = reviewer.rask(PUBLISH_SYS, f"NOTE ({rel}):\n{text[:6000]}")
    if not data or data.get("decision") not in ("publish", "local"):
        return None, "reviewer gave no usable answer"
    cache[rel] = {"hash": h, "decision": data["decision"], "reason": str(data.get("reason", ""))[:200]}
    DECISIONS.write_text(json.dumps(cache, indent=1))
    return data["decision"], cache[rel]["reason"]


def collect():
    out = {}
    for p in VAULT.iterdir():
        if p.is_file():
            out[p.name] = p.read_bytes()
    for d in ("summaries", "timelines", "entities", "tasks"):
        for p in (VAULT / d).rglob("*.md") if (VAULT / d).exists() else []:
            out[p.relative_to(VAULT).as_posix()] = p.read_bytes()
    return out


def decide():
    cache = json.loads(DECISIONS.read_text()) if DECISIONS.exists() else {}
    allowed, held, local, undecided = set(), [], 0, 0
    reviewer_up = reviewer.ready()
    for rel, data in sorted(collect().items()):
        text = data.decode("utf-8", errors="replace")
        verdict, why = classify(rel, text)
        if verdict == "never":
            continue
        if verdict == "local":
            local += 1
            continue
        block = gates(rel, data)
        if block:
            held.append(f"- `{rel}`: {block}")
            continue
        if verdict == "review":
            hit = cache.get(rel)
            if hit and hit["hash"] == hashlib.sha1(text.encode()).hexdigest():
                d = hit["decision"]
            elif reviewer_up and "--fast" not in sys.argv:
                d, why = verdict_from_reviewer(rel, text, cache)
            else:
                d = None
            if d is None:
                undecided += 1
                continue
            if d == "local":
                local += 1
                continue
        allowed.add(rel)
    report = [f"publish gate: {len(allowed)} to publish, {local} kept in the vault only, "
              f"{len(held)} held back by hard gates, {undecided} waiting for the reviewer"]
    return allowed, report, held


def merge_pr(token, url):
    m = re.search(r"/pulls/(\d+)", url)
    if not m:
        raise RuntimeError("could not read the PR number from " + url)
    req = urllib.request.Request(
        f"{G['url'].rstrip('/')}/api/v1/repos/{G['vault_repo']}/pulls/{m.group(1)}/merge",
        data=json.dumps({"Do": "merge"}).encode(),
        headers={"Authorization": f"token {token}", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status


def main():
    dry = "--dry" in sys.argv
    token = __import__("os").environ.get("GITEA_WRITE_TOKEN", "")
    if not dry and not token:
        sys.exit("set GITEA_WRITE_TOKEN (or use --dry)")
    allowed, report, held = decide()
    for line in report + held:
        print(line)
    agent.log("publisher: " + report[0])
    if not allowed:
        sys.exit("nothing to publish")
    note = "\n\n" + "\n".join(report) + ("\n\nHeld back by hard gates:\n" + "\n".join(held) if held else "")
    url = commit.notes_pr(dry, token, False, only=allowed, note=note)
    if url and P.get("auto_merge", False):
        status = merge_pr(token, url)
        agent.log(f"publisher: merged {url} (HTTP {status})")
        print("merged:", url)
    elif url:
        agent.log(f"publisher: opened {url}; auto_merge is off, waiting for you")


def fingerprint():
    h = hashlib.sha1()
    for rel, data in sorted(collect().items()):
        h.update(rel.encode())
        h.update(data)
    return h.hexdigest()


def watch():
    import time
    marker = STATE / "publish_last.json"
    gap = int(P.get("min_minutes_between", 60)) * 60
    sys.argv = [a for a in sys.argv if a != "--watch"]
    agent.log("publisher watch started")
    while True:
        try:
            last = json.loads(marker.read_text()) if marker.exists() else {}
            fp = fingerprint()
            if fp != last.get("fp") and time.time() - last.get("t", 0) >= gap and reviewer.ready():
                try:
                    main()
                except SystemExit as e:
                    agent.log(f"publisher: {e}")
                marker.write_text(json.dumps({"fp": fp, "t": time.time()}))
        except Exception as e:
            agent.log(f"publisher watch error: {e}")
        time.sleep(600)


if __name__ == "__main__":
    watch() if "--watch" in sys.argv else main()
