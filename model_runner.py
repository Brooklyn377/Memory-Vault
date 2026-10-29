#!/usr/bin/env python3
"""Model runner: lets the memory agent manage a llama-server on another machine (a GPU box, a PC).

It runs on that machine and owns its llama-server: starts it, restarts it with new settings from the agent's Settings
page, stops it, and reports its model files and options. Standard library only (Python 3.8+), on Linux, Windows and
macOS. Two ways to run llama-server:

  A container (Podman or Docker), e.g. the ghcr.io/ggml-org/llama.cpp server images. Adopt the one you already run:
      python3 model_runner.py --setup --adopt llama [--engine podman|docker]
  The runner reads that container's image, folders, port, GPU devices, security options, restart policy and command,
  and later recreates it with the same, replacing only the model, context, threads, slots and the flags you set on
  the card. Everything else in its command (for example --jinja and --api-key) is kept.

  A llama-server program on this machine:
      python3 model_runner.py --setup --llama /path/to/llama-server --models /path/to/models

Setup writes model_runner.json next to this file and prints a token once, for the Settings page. Then run
`python3 model_runner.py` (as a service, or in a terminal).

Safety: every request needs the token. Only model files inside the models folder can be used, numbers must be numbers,
and a flag is passed only if that llama-server's own --help lists it, with a plain value. Commands are run as lists,
never through a shell, and only the configured program or container engine with fixed subcommands. A change that does
not come up is rolled back to the previous settings, and the reply carries llama-server's last lines."""
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
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "model_runner.json"
LOG = HERE / "model_runner-llama.log"
NAME = re.compile(r"[\w.\-]+\.gguf")
VALUE = re.compile(r"[\w.\-:/]{1,200}")
CNAME = re.compile(r"[\w][\w.\-]{0,127}")
WAIT = 900   # seconds a restart may take before it is rolled back
# Options the card manages, with every spelling llama-server accepts: they are taken out of an adopted command
# before the card's values are added, so no option is given twice.
SAME = {"--model": ["-m"], "--ctx-size": ["-c"], "--threads": ["-t"], "--parallel": ["-np"],
        "--n-gpu-layers": ["-ngl", "--gpu-layers"], "--batch-size": ["-b"], "--ubatch-size": ["-ub"],
        "--flash-attn": ["-fa"], "--cache-type-k": ["-ctk"], "--cache-type-v": ["-ctv"], "--threads-batch": ["-tb"],
        "--model-draft": ["-md"], "--draft-max": ["--draft", "--draft-n"], "--draft-min": ["--draft-n-min"]}
MANAGED = ("--model", "--ctx-size", "--threads", "--parallel")

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


def _run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def flags_supported(c):
    if state["help"] is None:
        k = c.get("container")
        cmd = [k["engine"], "run", "--rm", k["image"], "--help"] if k else [c["llama"], "--help"]
        try:
            r = _run(cmd, 300)
            state["help"] = sorted(set(re.findall(r"--[a-z0-9][a-z0-9-]*", r.stdout + r.stderr)))
        except Exception:
            state["help"] = []
    return state["help"]


def models(c):
    root = Path(c["models"])
    return [{"name": p.name, "size": p.stat().st_size} for p in sorted(root.glob("*.gguf"))] if root.is_dir() else []


def _spellings(flag):
    return [flag] + SAME.get(flag, [])


def _strip(args, flags):
    """args without any of these options (in any spelling) and their values."""
    drop = {s for f in flags for s in _spellings(f)}
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a in drop:
            i += 1
            if i < len(args) and (not args[i].startswith("-") or re.fullmatch(r"-\d+(\.\d+)?", args[i])):
                i += 1   # its value
            continue
        out.append(a)
        i += 1
    return out


def build(c, s):
    """Settings from the agent -> the llama-server arguments. Raises ValueError for anything not allowed."""
    root = Path(c["models"])
    k = c.get("container")
    model = str(s.get("model_file") or "")
    if not NAME.fullmatch(model) or not (root / model).is_file():
        raise ValueError(f"{model or 'no model'} is not a model file in {root}")
    inside = (lambda f: str(PurePosixPath(k["models_in"]) / f)) if k else (lambda f: str(root / f))
    args = ["--model", inside(model)]
    for key, flag in (("context", "--ctx-size"), ("threads", "--threads"), ("parallel", "--parallel")):
        if s.get(key) not in (None, ""):
            args += [flag, str(int(float(s[key])))]
    sup = set(flags_supported(c))
    flags = s.get("flags") or {}
    for flag, v in flags.items():
        if not isinstance(flag, str) or not flag.startswith("--") or (sup and flag not in sup):
            raise ValueError(f"{flag} is not an option of this llama-server")
        if v is True:
            args.append(flag)
        elif v not in (None, False, ""):
            v = str(v)
            if v.endswith(".gguf"):
                if not NAME.fullmatch(v) or not (root / v).is_file():
                    raise ValueError(f"{v} is not a model file in {root}")
                v = inside(v)
            elif not VALUE.fullmatch(v):
                raise ValueError(f"{flag}: the value has odd characters")
            args += [flag, v]
    if k:   # the adopted command, minus what the card sets, then the card's values
        return _strip(k["base_args"], list(MANAGED) + list(flags)) + args
    return [c["llama"]] + args + ["--host", c.get("llama_host", "0.0.0.0"), "--port", str(c["llama_port"])]


def health(c):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{c['llama_port']}/health", timeout=3) as r:
            return "ready" if r.status == 200 else "loading"
    except urllib.error.HTTPError as e:
        return "loading" if e.code == 503 else "down"
    except Exception:
        return "down"


# ---------- running it: a program this runner starts, or a container it recreates ----------
def running(c):
    k = c.get("container")
    if k:
        r = _run([k["engine"], "inspect", "--format", "{{.State.Running}}", k["name"]], 30)
        return r.returncode == 0 and r.stdout.strip() == "true"
    p = state["proc"]
    return bool(p and p.poll() is None)


def _stop(c):
    k = c.get("container")
    if k:
        _run([k["engine"], "rm", "--force", k["name"]], 120)
        return
    p = state["proc"]
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(10)
    state["proc"] = None


def _start(c, args):
    k = c.get("container")
    if k:
        cmd = [k["engine"], "run", "--detach", "--name", k["name"]]
        for v in k.get("binds", []):
            cmd += ["--volume", v]
        for p in k.get("ports", []):
            cmd += ["--publish", p]
        for d in k.get("devices", []):
            cmd += ["--device", d]
        for o in k.get("security_opt", []):
            cmd += ["--security-opt", o]
        for g in k.get("group_add", []):
            cmd += ["--group-add", g]
        if k.get("restart") and k["restart"] != "no":
            cmd += ["--restart", k["restart"]]
        r = _run(cmd + [k["image"]] + args, 600)
        with open(LOG, "a") as f:
            f.write(f"\n=== {time.ctime()} {' '.join(cmd + [k['image']] + _hide(args))}\n{r.stdout}{r.stderr}")
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or "the container did not start")
        return
    log = open(LOG, "ab")
    log.write(f"\n=== {time.ctime()} {' '.join(_hide(args))}\n".encode())
    state["proc"] = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)


def _hide(args):
    """For the log: the value after --api-key is not written down."""
    return [("[hidden]" if i and args[i - 1] in ("--api-key", "--api-key-file") else a) for i, a in enumerate(args)]


def _tail(c, n=12):
    k = c.get("container")
    if k:
        r = _run([k["engine"], "logs", "--tail", str(n), k["name"]], 30)
        lines = (r.stdout + r.stderr).splitlines()
        if lines:
            return lines[-n:]
    try:
        return LOG.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _wait_ready(c):
    end = time.time() + WAIT
    time.sleep(2)
    while time.time() < end:
        if not running(c):
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
        _stop(c)
        try:
            _start(c, args)
            ok = _wait_ready(c)
        except RuntimeError as e:
            ok, early = False, str(e)
        else:
            early = ""
        if ok:
            c["settings"], c["stopped"] = s, False
            save(c)
            return {"ok": True}
        why = _tail(c) + ([early] if early else [])
        _stop(c)
        if old:
            try:
                _start(c, build(c, old))
                _wait_ready(c)
            except (ValueError, RuntimeError):
                pass
        return {"ok": False, "error": "llama-server did not come up with these settings; the previous ones were put back",
                "log": why}


def status():
    c = load()
    return {"running": running(c), "health": health(c), "settings": c.get("settings"), "stopped": bool(c.get("stopped")),
            "models": models(c), "flags": flags_supported(c), "llama_port": c["llama_port"],
            "container": (c.get("container") or {}).get("name"), "log": _tail(c, 6)}


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
            c = load()
            if self.path == "/stop":
                with state["lock"]:
                    _stop(c)
                    c["stopped"] = True
                    save(c)
                return self._send(200, {"ok": True})
            if self.path == "/start":
                if not c.get("settings"):
                    raise ValueError("no settings yet: apply some from the Settings page first")
                with state["lock"]:
                    if not running(c):
                        _start(c, build(c, c["settings"]))
                    c["stopped"] = False
                    save(c)
                return self._send(200, {"ok": True})
        except (ValueError, TypeError, RuntimeError) as e:
            return self._send(400, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


# ---------- setup ----------
def _flag_value(args, flag):
    for i, a in enumerate(args[:-1]):
        if a in _spellings(flag):
            return args[i + 1]
    return None


def adopt(engine, name):
    """Read an existing llama-server container. -> (container settings, models folder here, llama port, settings)."""
    if not CNAME.fullmatch(name):
        raise SystemExit("that container name has odd characters")
    r = _run([engine, "inspect", name], 60)
    if r.returncode:
        raise SystemExit(f"{engine} inspect {name} failed: {(r.stderr or r.stdout).strip()[:300]}")
    info = json.loads(r.stdout)[0]
    cfg, host = info.get("Config") or {}, info.get("HostConfig") or {}
    args = list(cfg.get("Cmd") or [])
    model = _flag_value(args, "--model")
    if not model:
        raise SystemExit("its command has no -m (model file), so the models folder cannot be found")
    models_in = str(PurePosixPath(model).parent)
    binds = list(host.get("Binds") or [])
    folder = next((b.split(":")[0] for b in binds if b.split(":")[1:2] == [models_in]), None)
    if not folder:
        raise SystemExit(f"no folder of this machine is mounted at {models_in} in that container")
    ports = []
    for cport, hs in (host.get("PortBindings") or {}).items():
        for hb in hs or []:
            ip = hb.get("HostIp") or ""
            ports.append(f"{ip + ':' if ip else ''}{hb.get('HostPort')}:{cport.split('/')[0]}")
    devices = [d.get("PathOnHost") + ":" + d.get("PathInContainer") for d in host.get("Devices") or [] if d.get("PathOnHost")]
    lport = _flag_value(args, "--port") or "8080"
    hport = next((p.split(":")[-2] for p in ports if p.endswith(":" + lport)), lport)
    k = {"engine": engine, "name": name, "image": cfg.get("Image") or info.get("ImageName"), "binds": binds,
         "ports": ports, "devices": devices, "security_opt": list(host.get("SecurityOpt") or []),
         "group_add": list(host.get("GroupAdd") or []), "restart": (host.get("RestartPolicy") or {}).get("Name") or "no",
         "models_in": models_in, "base_args": args}
    settings = {"model_file": PurePosixPath(model).name, "context": _flag_value(args, "--ctx-size"),
                "threads": _flag_value(args, "--threads"), "parallel": _flag_value(args, "--parallel"), "flags": {}}
    return k, folder, int(hport), {x: v for x, v in settings.items() if v is not None}


def main():
    ap = argparse.ArgumentParser(description="Let the memory agent manage this machine's llama-server.")
    ap.add_argument("--setup", action="store_true", help="write the settings file and print a new token")
    ap.add_argument("--adopt", metavar="CONTAINER", help="manage this existing llama-server container")
    ap.add_argument("--engine", default="podman", choices=["podman", "docker"], help="container engine (default podman)")
    ap.add_argument("--llama", help="path to llama-server (without a container)")
    ap.add_argument("--models", help="folder with the .gguf model files (without a container)")
    ap.add_argument("--listen", default="0.0.0.0", help="address the runner listens on (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8090, help="the runner's port (default 8090)")
    ap.add_argument("--llama-port", type=int, default=8080, help="llama-server's port, without a container (default 8080)")
    a = ap.parse_args()
    if a.setup:
        old = load() if CONFIG.exists() else {}
        if a.adopt:
            k, folder, lport, settings = adopt(a.engine, a.adopt)
            c = dict(old, container=k, models=folder, llama_port=lport, settings=settings, stopped=False)
            c.pop("llama", None)
        elif a.llama and Path(a.llama).exists() and a.models and Path(a.models).is_dir():
            c = dict(old, llama=str(Path(a.llama).resolve()), models=str(Path(a.models).resolve()), llama_port=a.llama_port)
            c.pop("container", None)
        else:
            sys.exit("--setup needs --adopt CONTAINER, or --llama (the llama-server program) and --models (the model folder)")
        c.update(listen=a.listen, port=a.port, token=secrets.token_urlsafe(24))
        save(c)
        print(f"Saved {CONFIG}.\nToken for the Settings page (shown once): {c['token']}\n"
              f"Runner address for the Settings page: http://<this machine's address>:{a.port}\n"
              f"Server address for the Settings page: http://<this machine's address>:{c['llama_port']}")
        if a.adopt:
            print(f"Managing the {a.engine} container {a.adopt} (models in {c['models']}); it keeps running as it is.")
        else:
            print(f"Stop any llama-server you started yourself on port {a.llama_port}; the runner starts its own.")
        return
    if not CONFIG.exists():
        sys.exit("run once with --setup first (see --help)")
    c = load()
    if c.get("settings") and not c.get("stopped") and not running(c):
        try:
            _start(c, build(c, c["settings"]))
        except (ValueError, RuntimeError) as e:
            print("not starting llama-server:", e)
    srv = ThreadingHTTPServer((c.get("listen", "0.0.0.0"), int(c.get("port", 8090))), H)
    print(f"model runner listening on {c.get('listen')}:{c.get('port')}, llama-server on port {c['llama_port']}")
    # Stopping the runner (Ctrl-C, or a service manager's TERM) also stops a llama-server program it started, so none
    # is left behind holding the port. A container keeps running: it does not depend on the runner.
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    try:
        srv.serve_forever()
    finally:
        if not c.get("container"):
            _stop(c)


if __name__ == "__main__":
    main()
