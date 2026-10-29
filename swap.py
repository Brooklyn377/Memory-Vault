"""The Curator's side of taking turns (the rest is turns.py). The Curator asks for its turn only when the other local models
have nothing to do (others_idle), holds it while it works through notes (holding), and gives it back when another model
is waiting (wanted) or it has nothing left (restore). A Curator on an external server needs none of this."""
import time
from pathlib import Path

STATE = Path("/var/lib/memory-agent")
HOLD = STATE / "curator_holding"   # the Curator has asked for its turn and still holds it


def holding():
    return HOLD.exists()


def wanted():
    """True if a local model outside the Curator's group is waiting for its turn."""
    import turns
    return turns.others_waiting("cur")


def others_idle():
    """True only if no other local model has a call running or wanted."""
    import turns
    return turns.others_idle("cur")


def healthy(port):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def take(wait=600):
    """Ask for the Curator's turn and wait until its group is the loaded one. False if the others got busy meanwhile or
    the turn did not come within `wait` seconds."""
    import turns
    HOLD.write_text("1")
    if not turns.others_idle("cur"):
        HOLD.unlink(missing_ok=True)
        return False
    end = time.time() + wait
    while time.time() < end:
        turns.want("cur")
        if turns.holds("cur"):
            return True
        if not turns.alive():   # no manager: nothing will switch for it
            break
        time.sleep(2)
    HOLD.unlink(missing_ok=True)
    turns.clear_want("cur")
    return False


def restore():
    """Give the turn back: whichever model is waiting is loaded next."""
    import turns
    HOLD.unlink(missing_ok=True)
    turns.clear_want("cur")
