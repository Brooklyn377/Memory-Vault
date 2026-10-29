"""Updates from the Settings page: check for a newer version (also every CHECK_EVERY seconds in the background, for the
menu's badge) and apply it on request. Both use /usr/local/bin/memory-agent-update, the same as the command line.

Applying runs the updater as its own systemd job (update-memory-agent), outside the web service: the update
restarts the web service, and a child of it would be cut off halfway. Its output goes to LOG, which the page shows."""
import json
import os
import re
import subprocess
import threading
import time

import agent

UPDATER = "/usr/local/bin/memory-agent-update"
STATUS = agent.STATE / "update_check.json"
LOG = agent.STATE / "update.log"
UNIT = "update-memory-agent"   # not memory-*: the updater's rollback restarts every running memory-* unit
CHECK_EVERY = 6 * 3600
_started = False


def status():
    try:
        return json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return {"available": False, "installed": "", "latest": "", "at": 0, "error": ""}


def check():
    """Ask GitHub, through the updater, whether there is a newer version. Saves and returns the answer."""
    st = {"at": time.time(), "error": "", "installed": "", "latest": "", "available": False}
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
    except Exception as e:
        st["error"] = f"{type(e).__name__}: {str(e)[:150]}"
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, STATUS)
    return st


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


def start():
    """Check in the background: a minute after the web service starts, then every CHECK_EVERY seconds."""
    global _started
    if _started:
        return
    _started = True

    def loop():
        time.sleep(60)
        while True:
            try:
                check()
            except Exception:
                pass
            time.sleep(CHECK_EVERY)
    threading.Thread(target=loop, daemon=True).start()
