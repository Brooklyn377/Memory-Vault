"""Files a chat says it updated, found in your Git servers.
For each chat not looked at yet: pick the file names that sit next to an "updated"-style word, find them in the Git
servers the Sweeper may read, and look at the commits that touched them after the chat. The Sweeper decides whether a
commit makes the update the chat describes; the Reviewer must agree. Only then is an updated file written:
  files/updated/<name>-<commit>.md    type: updated-file, the file as of that commit, the diff, links to the chat, the
                                      saved original if there is one, and the commit
Nothing is changed in Git: this only reads. The look-up for one chat stops after the time set under the Sweeper's
Limits and guardrails (File look-up time per chat)."""
import json
import re
import time
from datetime import datetime
from pathlib import Path

import agent
import gitrepos
import reviewer
import tuning
from scrub import scrub

VAULT = agent.VAULT
CONV = VAULT / "raw" / "conversations"
ORIG = VAULT / "raw" / "files"
UPDATED = VAULT / "files" / "updated"
SEEN = agent.STATE / "file_mentions.json"
# a file name: letters, digits, dots, dashes and underscores, ending in a short extension
NAME = re.compile(r"(?<![\w./-])([A-Za-z0-9_][\w.-]{0,60}\.[A-Za-z][A-Za-z0-9]{0,5})(?![\w-])")
# words that say a file was changed; a name within NEAR characters of one is looked up
VERBS = re.compile(r"\b(updat\w*|edit\w*|chang\w*|patch\w*|fix\w*|modif\w*|rewr\w*|replac\w*|creat\w*|add(?:ed)? to|wrote|writ\w*|commit\w*|push\w*)\b", re.I)
NEAR = 160

JUDGE_SYS = ("A chat says a file was updated. You get EXCERPTS from the chat and one COMMIT (its message and its diff) that "
             "changed that file after the chat. Decide whether this commit makes the update the chat describes. Reply as JSON "
             'only: {"match":true,"what":"<one sentence on what changed>","quote":"<exact words from the excerpts that '
             'describe this update>"}. If it does not, reply {"match":false}.')
CHECK_SYS = ("You check a claim that one commit makes an update a chat describes. You get the chat QUOTE, what the Sweeper says "
             "changed, and the commit's diff. Say ok true only if the diff really makes that update. Reply as JSON only: "
             '{"ok":true,"why":"<one short reason>"}')


def _seen():
    try:
        return json.loads(SEEN.read_text())
    except (OSError, ValueError):
        return {}


def mentioned(text):
    """File names that sit next to an update word, most mentioned first, each with the text around its mentions."""
    found = {}
    for m in NAME.finditer(text):
        a, b = max(0, m.start() - NEAR), min(len(text), m.end() + NEAR)
        if VERBS.search(text[a:b]):
            found.setdefault(m.group(1), []).append(text[a:b])
    return sorted(found.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def locate(name):
    """(server entry, path inside it) for each tracked file with this name, in the servers the Sweeper may read."""
    out = []
    for e in gitrepos.readable("qwen"):
        d = gitrepos.copy(e["id"], "qwen")
        if d is None:
            continue
        try:
            files = gitrepos._git(["ls-files"], "", cwd=d).splitlines()
        except Exception:
            continue
        out += [(e, f) for f in files if f.rsplit("/", 1)[-1] == name]
    return out


def commits_after(e, path, since, deadline):
    """The commits to one file made since a date, oldest first: (hash, author, date, subject, diff)."""
    d, tok = gitrepos.copy(e["id"], "qwen"), gitrepos.token(e["id"], "qwen")
    left = lambda: max(5, int(deadline - time.time()))
    for args in (["fetch", "-q", f"--shallow-since={since}", "origin", e["branch"]], ["fetch", "-q", "--deepen=1", "origin", e["branch"]]):
        try:
            gitrepos._git(args, tok, cwd=d, timeout=left())
        except Exception as ex:   # use the history there is
            agent.log(f"file look-up: could not read the history of {e['name']}: {str(ex)[:120]}")
            break
    out = gitrepos._git(["log", f"--since={since}", "--reverse", "--format=%x1e%H%x1f%an%x1f%aI%x1f%s", "-p", "--", path], tok, cwd=d, timeout=left())
    res = []
    for rec in out.split("\x1e")[1:]:
        head, _, diff = rec.partition("\n")
        h = head.split("\x1f")
        if len(h) == 4:
            res.append((h[0], h[1], h[2], h[3], diff.strip("\n")))
    return res


def contents_at(e, commit, path):
    return gitrepos._git(["show", f"{commit}:{path}"], gitrepos.token(e["id"], "qwen"), cwd=gitrepos.COPIES / e["id"])


def commit_url(e, commit):
    return re.sub(r"\.git$", "", e["url"].rstrip("/")) + "/commit/" + commit


def judge(excerpts, commit):
    h, who, when, subject, diff = commit
    limit = tuning.get("files.excerpt_chars")
    user = f"EXCERPTS:\n{chr(10).join(excerpts)[:limit]}\n\nCOMMIT {h[:10]} by {who} on {when[:10]}: {subject}\n{diff[:limit]}"
    budget = agent.Budget(2)
    for _ in range(2):
        v = agent.ask(JUDGE_SYS, user, budget)
        if isinstance(v, dict) and "match" in v:
            return v
    return None


def check(v, commit):
    user = f"QUOTE: {v.get('quote', '')}\nSWEEPER SAYS: {v.get('what', '')}\n\nDIFF:\n{commit[4][:tuning.get('files.excerpt_chars')]}"
    for _ in range(2):
        r = reviewer.rask(CHECK_SYS, user, tokens=2000)
        if isinstance(r, dict) and isinstance(r.get("ok"), bool):
            return r["ok"], str(r.get("why", ""))
    return None, "the Reviewer gave no usable answer"


def write_updated(name, e, path, commit, v, why, chat_stem):
    h, who, when, subject, diff = commit
    originals = sorted((ORIG / name).glob("*")) if (ORIG / name).is_dir() else []
    text = contents_at(e, h, path)
    ext = Path(name).suffix.lstrip(".")
    day = when[:10]
    body = (f"---\ntype: updated-file\ntitle: {json.dumps(f'Updated {name} ({h[:7]})')}\nfilename: {json.dumps(name)}\n"
            f"confidence: high\nrepository: {json.dumps(e['name'])}\ncommit: {h}\ncommitted: {day}\n---\n"
            f"# Updated {name}\n\n{v.get('what', '')}\n\n"
            f"- Chat: [[raw/conversations/{chat_stem}]]\n"
            f"- Change reference: commit {h[:10]} in {e['name']} ({path}): {commit_url(e, h)}\n"
            f"- Committed by {who} on {day}: {subject}\n")
    if originals:
        body += f"- Saved original: [[raw/files/{name}/{originals[-1].name}]]\n"
    body += f"- The Sweeper's quote from the chat: \"{v.get('quote', '')[:300]}\"\n- The Reviewer: {why}\n"
    fence = "````"
    body += f"\n## The file as of this commit\n{fence}{ext}\n{text.rstrip(chr(10))}\n{fence}\n\n## The change (git diff)\n{fence}diff\n{diff}\n{fence}\n"
    UPDATED.mkdir(parents=True, exist_ok=True)
    out = UPDATED / f"{name}-{h[:7]}.md"
    out.write_text(scrub(body)[0], encoding="utf-8")
    return out


def look(chat):
    """One chat: returns what happened, in a few words."""
    text = chat.read_text(encoding="utf-8", errors="replace")
    since = chat.name[:10]
    deadline = time.time() + tuning.get("files.lookup_seconds")
    made, tried = 0, 0
    for name, excerpts in mentioned(text):
        if time.time() > deadline:
            return f"{made} updated file(s); stopped at the time limit"
        for e, path in locate(name):
            if time.time() > deadline:
                break
            try:
                commits = commits_after(e, path, since, deadline)
            except Exception as ex:
                agent.log(f"file look-up: {name} in {e['name']}: {type(ex).__name__}: {str(ex)[:120]}")
                continue
            for c in commits:
                out = UPDATED / f"{name}-{c[0][:7]}.md"
                if out.exists():
                    continue
                if time.time() > deadline:
                    break
                tried += 1
                v = judge(excerpts, c)
                if not v or v.get("match") is not True or not reviewer.grounded(str(v.get("quote", "")), "\n".join(excerpts)):
                    continue
                ok, why = check(v, c)
                if ok:
                    write_updated(name, e, path, c, v, why, chat.stem)
                    made += 1
                    agent.log(f"file look-up: updated file for {name} ({c[0][:7]} in {e['name']}), from {chat.stem}")
                    break   # the first commit that makes the described update is the one; later ones are other work
    return f"{made} updated file(s) from {tried} commit(s) looked at"


def run():
    """Look at each chat that has not been looked at, newest first. Stops (to come back later) if a model is down."""
    seen = _seen()
    if not gitrepos.readable("qwen"):   # nothing to look in yet: the chats stay unlooked-at until a Git server is added
        return
    for p in sorted(CONV.glob("*.md"), reverse=True):
        if p.stem in seen:
            continue
        if not (agent.model_ready() and reviewer.ready()):
            return
        try:
            r = look(p)
        except Exception as e:
            r = f"error {type(e).__name__}: {str(e)[:120]}"
            agent.log(f"file look-up: {p.stem}: {r}")
        seen[p.stem] = {"at": datetime.now().isoformat(timespec="seconds"), "result": r}
        SEEN.write_text(json.dumps(seen))
