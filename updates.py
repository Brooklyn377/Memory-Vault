"""Updates from the Settings page: check for a newer version (when the Settings page is opened, and on Check now) and
apply it on request. Both use /usr/local/bin/memory-agent-update, the same as the command line.

Applying runs the updater as its own systemd job (update-memory-agent), outside the web service: the update
restarts the web service, and a child of it would be cut off halfway. Its output goes to LOG, which the page shows."""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import agent

UPDATER = "/usr/local/bin/memory-agent-update"
STATUS = agent.STATE / "update_check.json"
LOG = agent.STATE / "update.log"
UNIT = "update-memory-agent"   # not memory-*: the updater's rollback restarts every running memory-* unit
BACKUPS = Path("/var/backups/memory-agent")   # the updater's backups of the code from before each update
SOURCE = Path("/etc/memory-agent/source")   # where the code comes from: MA_REPO=owner/name and MA_REF=branch
VERSION = Path("/opt/memory-agent/VERSION")   # the code that is installed now (written by the updater)


def parse_notes(message):
    """A release's commit message -> [{"subject", "detail"}]: the list the release was made with ("- subject", then its
    detail on the next lines, indented). A message without such a list is one item, its first line."""
    lines = str(message or "").splitlines()
    items = []
    for line in lines[1:]:
        if line.startswith("- "):
            items.append({"subject": line[2:].strip(), "detail": ""})
        elif items and line.strip():
            items[-1]["detail"] = (items[-1]["detail"] + " " + line.strip()).strip()
    return items or ([{"subject": lines[0].strip(), "detail": ""}] if lines else [])


def _github(path):
    """One GitHub API answer for this project's repository. Raises on any failure."""
    repo = re.search(r"^MA_REPO=(\S+)", SOURCE.read_text(), re.M).group(1)
    req = urllib.request.Request("https://api.github.com/repos/" + repo + path,
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "memory-agent"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _release(c):
    return {"sha": c["sha"], "date": ((c.get("commit") or {}).get("committer") or {}).get("date", ""),
            "items": parse_notes((c.get("commit") or {}).get("message"))}


def changes(installed, latest):
    """The releases between the installed version and the newest, newest first, from GitHub: [{"sha", "date", "items"}].
    -> (list, note). If the installed version is not found there, the most recent ones are listed with a note."""
    note = ""
    try:
        if not installed:
            raise urllib.error.HTTPError("", 404, "", None, None)
        commits = list(reversed(_github(f"/compare/{installed}...{latest}").get("commits") or []))
    except urllib.error.HTTPError as e:
        if e.code not in (404, 422):
            return [], f"GitHub answered {e.code}"
        try:
            commits, note = _github(f"/commits?sha={latest}&per_page=10"), "The installed version was not found there, so these are the most recent releases."
        except (OSError, ValueError, AttributeError) as e2:
            return [], f"{type(e2).__name__}"
    except (OSError, ValueError, AttributeError) as e:
        return [], f"{type(e).__name__}"
    return [_release(c) for c in commits], note


def recent(latest):
    """The last 10 releases with their notes (all that is fetched), newest first; [] if GitHub cannot be asked."""
    try:
        return [_release(c) for c in _github(f"/commits?sha={latest}&per_page=10")]
    except (OSError, ValueError, AttributeError, KeyError):
        return []


def rollback_info():
    """The version the last update replaced, which Roll back puts back: {"available", "version", "at"}. The updater keeps
    a backup of the code from before each update."""
    import tarfile
    try:
        b = sorted(BACKUPS.glob("code-*.tar.gz"))[-1]
    except IndexError:
        return {"available": False, "reason": f"Nothing to roll back to: no backup of an earlier version was found in {BACKUPS} "
                                              "(the updater makes one before each update)."}
    version = ""
    try:
        with tarfile.open(b) as t:
            version = t.extractfile("opt/memory-agent/VERSION").read().decode().strip()
    except (OSError, KeyError, tarfile.TarError, AttributeError):
        pass
    m = re.match(r"code-(\d{8})-(\d{6})", b.name)
    at = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}T{m.group(2)[:2]}:{m.group(2)[2:4]}:{m.group(2)[4:]}" if m else ""
    same = bool(version) and version == status().get("installed")
    return {"available": not same, "version": version, "at": at,
            "reason": "Nothing to roll back to: the newest backup is the version you are running." if same else ""}


def installed_now():
    try:
        return VERSION.read_text().strip()
    except OSError:
        return ""


def status():
    """The last check, with the installed version read now: after an update or a rollback it is the new one at once, and
    whether an update is available follows from it. stale: the version changed since that check, so its lists are old."""
    try:
        st = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        st = {"available": False, "installed": "", "latest": "", "at": 0, "error": "", "changes": [], "changes_note": "", "recent": []}
    live = installed_now()
    if live:
        st["stale"] = bool(st.get("installed")) and st["installed"] != live
        st["installed"] = live
        if st.get("latest"):
            st["available"] = st["latest"] != live
    if not st.get("available"):
        st["changes"] = []
    return st


def check():
    """Ask GitHub, through the updater, whether there is a newer version. Saves and returns the answer."""
    prev = status()
    st = {"at": time.time(), "error": "", "installed": "", "latest": "", "available": False, "changes": [], "changes_note": "", "recent": []}
    try:
        r = subprocess.run([UPDATER, "--check"], capture_output=True, text=True, timeout=120)
        out = r.stdout + r.stderr
        m1 = re.search(r"installed:\s*(\S+)", out)
        m2 = re.search(r"available:\s*(\S+)", out)
        st["installed"] = m1.group(1) if m1 else ""
        st["latest"] = m2.group(1) if m2 else ""
        st["available"] = "an update is available" in out
        if r.returncode or not m2 or st["latest"] == "unknown":
            st["error"] = "could not ask GitHub for the newest version" if not st["error"] else st["error"]
        else:
            st["recent"] = recent(st["latest"])
            if st["available"]:
                st["changes"], st["changes_note"] = changes(st["installed"], st["latest"])
    except Exception as e:
        st["error"] = f"{type(e).__name__}: {str(e)[:150]}"
    if st["error"]:   # a failed check does not throw away what the last good one found
        st = dict(prev, at=st["at"], error=st["error"])
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, STATUS)
    return st


def rollback():
    """Put back the code from before the last update, as its own systemd job like an update (the services restart)."""
    if running():
        raise ValueError("an update is already running")
    if not rollback_info()["available"]:
        raise ValueError("there is no earlier version to put back")
    subprocess.run(["systemctl", "reset-failed", UNIT], capture_output=True, timeout=10)
    r = subprocess.run(["systemd-run", f"--unit={UNIT}", "--collect", "--quiet",
                        f"--property=StandardOutput=truncate:{LOG}", f"--property=StandardError=append:{LOG}",
                        UPDATER, "--rollback"], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise ValueError(f"could not start the rollback: {(r.stderr or r.stdout).strip()[:200]}")
    agent.log("update: rollback started from the Settings page")


def running():
    return subprocess.run(["systemctl", "is-active", "--quiet", UNIT], timeout=10).returncode == 0


def apply():
    """Start the update as its own systemd job. Its output goes to LOG; the web page restarts along the way."""
    if running():
        raise ValueError("an update is already running")
    subprocess.run(["systemctl", "reset-failed", UNIT], capture_output=True, timeout=10)
    r = subprocess.run(["systemd-run", f"--unit={UNIT}", "--collect", "--quiet",
                        f"--property=StandardOutput=truncate:{LOG}", f"--property=StandardError=append:{LOG}",
                        UPDATER], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise ValueError(f"could not start the update: {(r.stderr or r.stdout).strip()[:200]}")
    agent.log("update: started from the Settings page")


def progress():
    try:
        lines = LOG.read_text(errors="replace").splitlines()[-40:]
    except OSError:
        lines = []
    return {"running": running(), "log": lines}
