#!/usr/bin/env python3
"""Model runner: lets the memory agent manage a llama-server on another machine (a GPU box, a PC).

It runs next to llama-server, owns it (starts it, restarts it with new settings, stops it) and answers the agent's
Settings page over HTTP with a token. Standard library only, so it runs on Linux, Windows and macOS with nothing
installed but Python 3.8+.

First run (writes model_runner.json next to this file and prints the token once; keep it for the Settings page):
    python3 model_runner.py --setup --llama /path/to/llama-server --models /path/to/models
        [--listen 0.0.0.0] [--port 8090] [--llama-port 8080]
Then run it (as a service, or in a terminal):
    python3 model_runner.py

Safety: every request needs the token. Only model files inside the models folder can be used, numbers must be numbers,
and a server flag is passed only if this llama-server's own --help lists it, with a plain value. Everything is passed
as separate arguments, never through a shell. A change that does not come up within the wait time is rolled back to
the previous settings, and the reply carries llama-server's last lines."""
import argparse
import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "model_runner.json"
LOG = HERE / "model_runner-llama.log"
NAME = re.compile(r"[\w.\-]+\.gguf")
VALUE = re.compile(r"[\w.\-:/]{1,200}")
WAIT = 900   # seconds a restart may take before it is rolled back

state = {"proc": None, "lock": threading.Lock(), "help": None}


def load():
    return json.loads(CONFIG.read_text())


def save(c):
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, indent=1))
    os.replace(tmp, CONFIG)
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass


def flags_supported(c):
    if state["help"] is None:
        try:
            r = subprocess.run([c["llama"], "--help"], capture_output=True, text=True, timeout=60)
            state["help"] = sorted(set(re.findall(r"--[a-z0-9][a-z0-9-]*", r.stdout + r.stderr)))
        except Exception:
            state["help"] = []
    return state["help"]


def models(c):
    root = Path(c["models"])
    return [{"name": p.name, "size": p.stat().st_size} for p in sorted(root.glob("*.gguf"))] if root.is_dir() else []


def build(c, s):
    """Settings from the agent -> llama-server arguments. Raises ValueError for anything not allowed."""
    root = Path(c["models"])
    model = str(s.get("model_file") or "")
    if not NAME.fullmatch(model) or not (root / model).is_file():
        raise ValueError(f"{model or 'no model'} is not a model file in {root}")
    args = [c["llama"], "-m", str(root / model)]
    for key, flag in (("context", "-c"), ("threads", "-t"), ("parallel", "-np")):
        if s.get(key) not in (None, ""):
            args += [flag, str(int(float(s[key])))]
    sup = set(flags_supported(c))
    for flag, v in (s.get("flags") or {}).items():
        if not isinstance(flag, str) or not flag.startswith("--") or flag not in sup:
            raise ValueError(f"{flag} is not an option of this llama-server")
        if v is True:
            args.append(flag)
        elif v not in (None, False, ""):
            v = str(v)
            if v.endswith(".gguf"):
                if not NAME.fullmatch(v) or not (root / v).is_file():
                    raise ValueError(f"{v} is not a model file in {root}")
                v = str(root / v)
            elif not VALUE.fullmatch(v):
                raise ValueError(f"{flag}: the value has odd characters")
            args += [flag, v]
    return args + ["--host", c.get("llama_host", "0.0.0.0"), "--port", str(c["llama_port"])]


def health(c):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{c['llama_port']}/health", timeout=3) as r:
            return "ready" if r.status == 200 else "loading"
    except urllib.error.HTTPError as e:
        return "loading" if e.code == 503 else "down"
    except Exception:
        return "down"


def _stop():
    p = state["proc"]
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(10)
    state["proc"] = None


def _start(args):
    log = open(LOG, "ab")
    log.write(f"\n=== {time.ctime()} {' '.join(args)}\n".encode())
    state["proc"] = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)


def _tail(n=12):
    try:
        return LOG.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _wait_ready(c):
    end = time.time() + WAIT
    while time.time() < end:
        if state["proc"] is None or state["proc"].poll() is not None:
            return False
        if health(c) == "ready":
            return True
        time.sleep(2)
    return False


def apply(s):
    """Restart llama-server with these settings; roll back to the previous ones if it does not come up."""
    c = load()
    args = build(c, s)
    with state["lock"]:
        old = c.get("settings")
        _stop()
        _start(args)
        if _wait_ready(c):
            c["settings"], c["stopped"] = s, False
            save(c)
            return {"ok": True}
        why = _tail()
        _stop()
        if old:
            try:
                _start(build(c, old))
                _wait_ready(c)
            except ValueError:
                pass
        return {"ok": False, "error": "llama-server did not come up with these settings; the previous ones were put back",
                "log": why}


def status():
    c = load()
    p = state["proc"]
    return {"running": bool(p and p.poll() is None), "health": health(c), "settings": c.get("settings"),
            "stopped": bool(c.get("stopped")), "models": models(c), "flags": flags_supported(c),
            "llama_port": c["llama_port"], "log": _tail(6)}


class H(BaseHTTPRequestHandler):
    def _auth(self):
        got = self.headers.get("Authorization", "")
        return hmac.compare_digest(got.encode(), ("Bearer " + load()["token"]).encode())

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not self._auth():
            return self._send(401, {"error": "token"})
        if self.path == "/status":
            return self._send(200, status())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return self._send(401, {"error": "token"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/apply":
                return self._send(200, apply(body))
            if self.path == "/stop":
                with state["lock"]:
                    _stop()
                    c = load()
                    c["stopped"] = True
                    save(c)
                return self._send(200, {"ok": True})
            if self.path == "/start":
                c = load()
                if not c.get("settings"):
                    raise ValueError("no settings yet: apply some from the Settings page first")
                with state["lock"]:
                    if not (state["proc"] and state["proc"].poll() is None):
                        _start(build(c, c["settings"]))
                    c["stopped"] = False
                    save(c)
                return self._send(200, {"ok": True})
        except (ValueError, TypeError) as e:
            return self._send(400, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description="Let the memory agent manage this machine's llama-server.")
    ap.add_argument("--setup", action="store_true", help="write the settings file and print a new token")
    ap.add_argument("--llama", help="path to llama-server")
    ap.add_argument("--models", help="folder with the .gguf model files")
    ap.add_argument("--listen", default="0.0.0.0", help="address the runner listens on (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8090, help="the runner's port (default 8090)")
    ap.add_argument("--llama-port", type=int, default=8080, help="llama-server's port (default 8080)")
    a = ap.parse_args()
    if a.setup:
        if not a.llama or not Path(a.llama).exists() or not a.models or not Path(a.models).is_dir():
            sys.exit("--setup needs --llama (the llama-server program) and --models (the model folder)")
        old = load() if CONFIG.exists() else {}
        c = dict(old, llama=str(Path(a.llama).resolve()), models=str(Path(a.models).resolve()), listen=a.listen,
                 port=a.port, llama_port=a.llama_port, token=secrets.token_urlsafe(24))
        save(c)
        print(f"Saved {CONFIG}.\nToken for the Settings page (shown once): {c['token']}\n"
              f"Helper address for the Settings page: http://<this machine's address>:{a.port}\n"
              f"Stop any llama-server you started yourself on port {a.llama_port}; the runner starts its own.")
        return
    if not CONFIG.exists():
        sys.exit("run once with --setup first (see --help)")
    c = load()
    if c.get("settings") and not c.get("stopped"):
        try:
            _start(build(c, c["settings"]))
        except ValueError as e:
            print("not starting llama-server:", e)
    srv = ThreadingHTTPServer((c.get("listen", "0.0.0.0"), int(c.get("port", 8090))), H)
    print(f"model runner listening on {c.get('listen')}:{c.get('port')}, llama-server on port {c['llama_port']}")
    # Stopping the runner (Ctrl-C, or a service manager's TERM) also stops its llama-server, so none is left behind
    # holding the port.
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    try:
        srv.serve_forever()
    finally:
        _stop()


if __name__ == "__main__":
    main()
