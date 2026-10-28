"""Turn pending proposals into ONE branch + pull request on Gitea. Never touches main.

Usage:  GITEA_WRITE_TOKEN=... python commit.py [--dry]            proposals -> one PR
        GITEA_WRITE_TOKEN=... python commit.py --notes [--dry] [--with-raw]   generated notes -> one PR
  --dry   apply and show the diff locally, then undo; nothing is pushed.
"""
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import config

CFG = config.load()
VAULT = Path(CFG["paths"]["vault"])
STATE = Path(CFG["paths"]["state_dir"])
G = CFG["gitea"]
BASE = G.get("base_branch", "main")
IDENT = ["-c", f"user.name={G.get('bot_name', 'Memory-bot')}",
         "-c", f"user.email={G.get('bot_email', 'memory-bot@localhost')}"]


def git(*args, token=None, check=True, cwd=None):
    cmd = ["git", *IDENT]
    if token:
        # token travels in a header for this one command; it is never written to .git/config
        cmd += ["-c", f"http.extraHeader=Authorization: token {token}"]
    r = subprocess.run(cmd + list(args), cwd=cwd or VAULT, capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"git {args[0]} failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def load_pending():
    done_file = STATE / "committed.json"
    done = set(json.loads(done_file.read_text())) if done_file.exists() else set()
    files = sorted(p for p in (STATE / "proposals").glob("*.json") if p.name not in done)
    return files, done, done_file


def open_pr(token, branch, title, body):
    req = urllib.request.Request(
        f"{G['url'].rstrip('/')}/api/v1/repos/{G['vault_repo']}/pulls",
        data=json.dumps({"head": branch, "base": BASE, "title": title, "body": body}).encode(),
        headers={"Authorization": f"token {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("html_url", "")


NOTE_DIRS = ["summaries", "timelines", "entities", "tasks"]  # written by the agent; raw/ only with --with-raw


def is_agent_note(path):
    """Only notes the agent itself generated (confidence: low) may be deleted by a notes PR."""
    try:
        head = path.read_text(encoding="utf-8")[:400]
    except OSError:
        return False
    return head.startswith("---") and "confidence: low" in head


def notes_pr(dry, token, with_raw, only=None, note=""):
    """One PR that brings the agent's generated notes (summaries, timelines, entities) up to date.
    Works in a temporary git worktree, so the live vault folder is never switched or touched."""
    dirs = NOTE_DIRS + (["raw"] if with_raw else [])
    git("fetch", "origin", BASE, token=token or None)
    wt = STATE / "notes-worktree"
    git("worktree", "remove", "--force", str(wt), check=False)
    git("worktree", "prune", check=False)
    branch = f"{G.get('agent_branch', 'agent/proposals').rsplit('/', 1)[0]}/notes-{datetime.now():%Y%m%d-%H%M}"
    git("worktree", "add", "-B", branch, str(wt), f"origin/{BASE}")
    try:
        for d in dirs:
            src, dst = VAULT / d, wt / d
            if src.exists():
                for f in src.rglob("*"):
                    if f.is_file():
                        if only is not None and f.relative_to(VAULT).as_posix() not in only:
                            continue
                        out = dst / f.relative_to(src)
                        out.parent.mkdir(parents=True, exist_ok=True)
                        if not out.exists() or out.read_bytes() != f.read_bytes():
                            out.write_bytes(f.read_bytes())
            if dst.exists():
                for f in list(dst.rglob("*.md")):
                    if not (src / f.relative_to(dst)).exists() and (d == "raw" or is_agent_note(f)):
                        f.unlink()
        for rel in sorted(only or []):  # allowed top-level files (README, config)
            if "/" not in rel and (VAULT / rel).is_file():
                (wt / rel).write_bytes((VAULT / rel).read_bytes())
        git("add", "-A", cwd=wt)
        stat = git("diff", "--cached", "--stat", "--no-renames", cwd=wt)
        names = git("diff", "--cached", "--name-status", "--no-renames", cwd=wt).splitlines()
        if not names:
            print("notes are already up to date in", BASE)
            return
        new = sum(n.startswith("A") for n in names)
        gone = sum(n.startswith("D") for n in names)
        changed = len(names) - new - gone
        title = f"Agent notes {datetime.now():%Y-%m-%d %H:%M}: {new} new, {changed} updated, {gone} removed"
        body = ("Generated notes from the memory agent's overnight read of your chats. "
                "Everything here is low confidence until you review it. Facts quote the chat text they came from.\n\n"
                f"- new: {new}\n- updated: {changed}\n- removed: {gone}\n\n```\n{stat[-1500:]}\n```{note}")
        if dry:
            print(title)
            print(stat)
            return
        git("commit", "-m", title, "-m", body, cwd=wt)
        git("push", "origin", branch, token=token, cwd=wt)
        url = open_pr(token, branch, title, body)
        print("pull request:", url)
        return url
    finally:
        git("worktree", "remove", "--force", str(wt), check=False)
        if dry:
            git("branch", "-D", branch, check=False)


def main():
    dry = "--dry" in sys.argv
    token = os.environ.get("GITEA_WRITE_TOKEN", "")
    if not dry and not token:
        sys.exit("set GITEA_WRITE_TOKEN (or use --dry)")
    if "--notes" in sys.argv:
        return notes_pr(dry, token, "--with-raw" in sys.argv)
    files, done, done_file = load_pending()
    if not files:
        sys.exit("no pending proposals")
    if git("status", "--porcelain", "--untracked-files=no"):
        sys.exit("vault has uncommitted changes to tracked files; refusing to continue")

    git("fetch", "origin", BASE, token=token or None)
    branch = f"{G.get('agent_branch', 'agent/proposals')}-{datetime.now():%Y%m%d-%H%M}"
    git("checkout", "-B", branch, f"origin/{BASE}")
    applied, skipped, lines = [], [], []
    try:
        for pf in files:
            data = json.loads(pf.read_text())
            used = 0
            for pr in data["proposals"]:
                target = VAULT / pr["path"]
                text = target.read_text(encoding="utf-8") if target.exists() else ""
                if text.count(pr["old"]) != 1:  # file changed since the agent read it
                    skipped.append(f"{pr['path']} ({pf.name}): text no longer matches")
                    continue
                target.write_text(text.replace(pr["old"], pr["new"], 1), encoding="utf-8")
                used += 1
                lines.append(f"- **{pr['path']}** [{pr['confidence']}] {pr['reason']}\n"
                             f"  - evidence: `{pr['evidence']}`")
            if used:
                applied.append(pf.name)
            else:
                skipped.append(f"{pf.name}: nothing applicable")
        if not lines:
            sys.exit("nothing could be applied:\n  " + "\n  ".join(skipped))
        git("add", "-u")
        if dry:
            print(git("diff", "--cached", "--stat"))
            print(git("diff", "--cached"))
        else:
            body = ("Proposed by the memory agent. Review each change; merge to accept.\n\n"
                    + "\n".join(lines)
                    + ("\n\nSkipped:\n" + "\n".join(f"- {s}" for s in skipped) if skipped else ""))
            git("commit", "-m", f"agent: {len(lines)} proposed change(s)", "-m", body)
            git("push", "origin", branch, token=token)
            url = open_pr(token, branch, f"Agent proposals {datetime.now():%Y-%m-%d %H:%M}", body)
            done_file.write_text(json.dumps(sorted(done | set(applied))))
            print("pull request:", url)
        for s in skipped:
            print("skipped:", s)
    finally:
        git("reset", "--hard", check=False)
        git("checkout", BASE, check=False)
        if dry:
            git("branch", "-D", branch, check=False)


if __name__ == "__main__":
    main()
