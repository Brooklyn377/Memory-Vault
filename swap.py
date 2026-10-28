"""Swaps memory between the Sweeper+Reviewer pair and the curator model.
While curator_holding exists the pair is stopped. A call that needs the pair writes work_wanted and waits;
the curator finishes its note, then swaps the pair back and removes curator_holding."""
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

STATE = Path("/var/lib/memory-agent")
HOLD, WANTED = STATE / "curator_holding", STATE / "work_wanted"
PAIR = {8080: "llama-server", 8081: "memory-reviewer"}
KEY = {8080: "qwen", 8081: "gpt"}


def _ext(port):
    try:
        import power
        if power.off(KEY[port]):
            return True
        import remote
        return remote.enabled(KEY[port])
    except Exception:
        return False


def holding():
    return HOLD.exists()


def want():
    try:
        WANTED.write_text("1")
    except OSError:
        pass


def wanted():
    return WANTED.exists()


def clear_want():
    WANTED.unlink(missing_ok=True)


def wait_for(endpoint):
    """Block while the curator holds the memory. Only calls to the pair wait; they say they want it back."""
    m = re.search(r"//(?:127\.0\.0\.1|localhost):(\d+)", endpoint)
    if not m or int(m.group(1)) not in PAIR:
        return
    while holding():
        want()
        time.sleep(5)


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status, r.read()
    except Exception:
        return None, b""


def healthy(port):
    return _get(f"http://127.0.0.1:{port}/health")[0] == 200


def others_idle():
    """True only if both servers answer and no slot is running a call."""
    for port in PAIR:
        if _ext(port):
            continue
        code, raw = _get(f"http://127.0.0.1:{port}/slots")
        if code != 200:
            return False
        try:
            if any(x.get("is_processing") for x in json.loads(raw)):
                return False
        except ValueError:
            return False
    return True


def take():
    """Claim the memory and stop the pair. False if a call started in the meantime."""
    HOLD.write_text("1")
    clear_want()
    if not others_idle():
        HOLD.unlink(missing_ok=True)
        return False
    for unit in [u for p, u in PAIR.items() if not _ext(p)]:
        subprocess.run(["systemctl", "stop", unit], timeout=600)
    return True


def restore():
    """Start the pair again; callers keep waiting until both answer ready."""
    for unit in [u for p, u in PAIR.items() if not _ext(p)]:
        subprocess.run(["systemctl", "start", "--no-block", unit], timeout=30)
    end = time.time() + 900
    while time.time() < end and not all(healthy(p) for p in PAIR if not _ext(p)):
        time.sleep(5)
    HOLD.unlink(missing_ok=True)
    clear_want()
