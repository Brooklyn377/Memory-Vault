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

  A container that does not exist yet, which the runner creates when the first settings are applied:
      python3 model_runner.py --setup --create --engine podman --image IMAGE --models DIR [--device /dev/dri]

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
import shutil
import urllib.parse
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

state = {"proc": None, "lock": threading.Lock(), "help": None, "dl": {}, "stop": set()}
HF = ("huggingface.co", "hf.co")
CHUNK = 1024 * 1024


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
    return [c["llama"]] + _strip(c.get("base_args") or [], list(MANAGED) + list(flags)) + args + ["--host", c.get("llama_host", "0.0.0.0"), "--port", str(c["llama_port"])]


# ---------- model files: download, list, delete (the Settings page's Servers card asks for these) ----------
def _hf(host):
    return host in HF or host.endswith(".huggingface.co")


def _download(c, st):
    root, name = Path(c["models"]), st["name"]
    part = root / (name + ".part")
    try:
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(st["url"], headers={"Range": f"bytes={have}-"} if have else {})
        with urllib.request.urlopen(req, timeout=60) as r:
            if have and r.status != 206:
                have = 0
            total = have + int(r.headers.get("Content-Length") or 0)
            if shutil.disk_usage(root).free < total - have:
                raise OSError("not enough free disk space")
            st.update(status="running", done=have, total=total, error="")
            with open(part, "ab" if have else "wb") as f:
                while name not in state["stop"]:
                    buf = r.read(CHUNK)
                    if not buf:
                        break
                    f.write(buf)
                    st["done"] += len(buf)
        if name in state["stop"]:
            part.unlink(missing_ok=True)
            st["status"] = "cancelled"
        elif total and st["done"] < total:
            raise OSError("connection closed early")
        else:
            os.replace(part, root / name)
            st["status"] = "done"
    except Exception as e:
        st.update(status="failed", error=f"{type(e).__name__}: {str(e)[:120]}")
    finally:
        state["stop"].discard(name)


def start_download(c, url, confirmed):
    """Download a model file into the models folder. Hugging Face links start at once; any other host needs confirmed."""
    u = urllib.parse.urlparse(str(url).strip())
    if u.scheme != "https" or not u.hostname:
        raise ValueError("use a full https:// link")
    name = urllib.parse.unquote(Path(u.path).name)
    if not NAME.fullmatch(name):
        raise ValueError("the link must end in a .gguf file name")
    host = u.hostname.lower()
    if not _hf(host) and not confirmed:
        raise PermissionError(host)
    if (Path(c["models"]) / name).exists():
        raise ValueError(name + " already exists")
    old = state["dl"].get(name)
    if old and old["status"] in ("starting", "running"):
        raise ValueError("already downloading")
    path = u.path.replace("/blob/", "/resolve/", 1) if _hf(host) else u.path
    st = {"name": name, "url": urllib.parse.urlunparse(u._replace(path=path)), "host": host, "third_party": not _hf(host),
          "status": "starting", "done": 0, "total": 0, "error": ""}
    state["dl"][name] = st
    threading.Thread(target=_download, args=(c, st), daemon=True).start()


def downloads(c):
    out = [d for d in state["dl"].values() if d["status"] not in ("done", "cancelled")]   # a finished one is a model file now
    root = Path(c["models"])
    for p in sorted(root.glob("*.gguf.part")) if root.is_dir() else []:   # left by an earlier run
        if p.name[:-5] not in state["dl"]:
            out.append({"name": p.name[:-5], "status": "interrupted", "done": p.stat().st_size, "total": 0, "error": ""})
    return out


def delete_model(c, name, force):
    """Delete a model file. Refused while the running llama-server uses it, unless force."""
    root = Path(c["models"])
    if not NAME.fullmatch(name) or not (root / name).is_file():
        raise ValueError("no such model file")
    if not force and (c.get("settings") or {}).get("model_file") == name and running(c):
        raise ValueError(f"{name} is the model llama-server is running now")
    (root / name).unlink()


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
            "container": (c.get("container") or {}).get("name"), "log": _tail(c, 6), "downloads": downloads(c)}


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
            if self.path == "/download":
                try:
                    start_download(c, body.get("url", ""), bool(body.get("confirmed")))
                except PermissionError as e:
                    return self._send(409, {"error": f"third_party:{e}"})
                return self._send(200, {"ok": True})
            if self.path == "/download/cancel":
                name = str(body.get("name", ""))
                if not NAME.fullmatch(name):
                    raise ValueError("bad name")
                if name in state["dl"] and state["dl"][name]["status"] in ("starting", "running"):
                    state["stop"].add(name)
                else:
                    (Path(c["models"]) / (name + ".part")).unlink(missing_ok=True)
                    state["dl"].pop(name, None)
                return self._send(200, {"ok": True})
            if self.path == "/models/delete":
                delete_model(c, str(body.get("name", "")), bool(body.get("force")))
                return self._send(200, {"ok": True})
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
    ap.add_argument("--device", action="append", default=[], metavar="HOST[:CONTAINER]",
                    help="a device the container needs, e.g. --device /dev/dri for a GPU (Vulkan) or /dev/kfd for ROCm; "
                         "repeat for more. Needed when the engine does not report the device, as Podman does not for "
                         "Quadlet's AddDevice")
    ap.add_argument("--create", action="store_true", help="with --setup: manage a llama-server container that does not exist yet; "
                    "the runner creates it when the first settings are applied (needs --image and --models)")
    ap.add_argument("--image", help="with --create: the llama.cpp server image, e.g. ghcr.io/ggml-org/llama.cpp:server-vulkan")
    ap.add_argument("--name", default="llama-server", help="with --create: the container's name (default llama-server)")
    ap.add_argument("--group-add", action="append", default=[], metavar="GROUP",
                    help="with --create: a group the container joins (the GPU device's group id, or keep-groups with rootless Podman)")
    ap.add_argument("--api-key", help="with --create or --llama: the key llama-server asks for (not shown in the runner's log)")
    ap.add_argument("--jinja", action="store_true", help="with --llama: always start llama-server with --jinja (a container made by --create has it)")
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
            for dev in a.device:   # devices the engine did not report
                dev = dev if ":" in dev else f"{dev}:{dev}"
                if dev not in k["devices"]:
                    k["devices"].append(dev)
            c = dict(old, container=k, models=folder, llama_port=lport, settings=settings, stopped=False)
            c.pop("llama", None)
        elif a.create:
            if not a.image or not a.models:
                sys.exit("--create needs --image and --models")
            models = Path(a.models).resolve()
            models.mkdir(parents=True, exist_ok=True)
            dev = [d if ":" in d else f"{d}:{d}" for d in a.device]
            k = {"engine": a.engine, "name": a.name, "image": a.image, "binds": [f"{models}:/models:ro"],
                 "ports": [f"{a.llama_port}:8080"], "devices": dev, "security_opt": ["label=disable"], "group_add": a.group_add,
                 "restart": "unless-stopped", "models_in": "/models",
                 "base_args": ["--host", "0.0.0.0", "--port", "8080", "--jinja"] + (["--api-key", a.api_key] if a.api_key else [])}
            c = dict(old, container=k, models=str(models), llama_port=a.llama_port)
            c.pop("llama", None)
            c.pop("settings", None)
        elif a.llama and Path(a.llama).exists() and a.models and Path(a.models).is_dir():
            c = dict(old, llama=str(Path(a.llama).resolve()), models=str(Path(a.models).resolve()), llama_port=a.llama_port,
                     base_args=(["--jinja"] if a.jinja else []) + (["--api-key", a.api_key] if a.api_key else []))
            c.pop("container", None)
        else:
            sys.exit("--setup needs --adopt CONTAINER, or --llama (the llama-server program) and --models (the model folder)")
        c.update(listen=a.listen, port=a.port, token=secrets.token_urlsafe(24))
        save(c)
        print(f"Saved {CONFIG}.\nToken for the Settings page (shown once): {c['token']}\n"
              f"Runner address for the Settings page: http://<this machine's address>:{a.port}\n"
              f"Server address for the Settings page: http://<this machine's address>:{c['llama_port']}")
        if a.create:
            print(f"Managing the {a.engine} container {a.name} (image {a.image}, models in {c['models']}). It does not exist yet: "
                  "the runner creates it when you press Apply all on a model's card, after you have a model file here.")
        if a.adopt:
            print(f"Managing the {a.engine} container {a.adopt} (models in {c['models']}); it keeps running as it is.")
            devs = c["container"]["devices"]
            print("Devices it will get:", ", ".join(devs) if devs else
                  "none. If it uses a GPU, run setup again with --device /dev/dri (Vulkan) or --device /dev/kfd "
                  "--device /dev/dri (ROCm), or it will run on the CPU.")
            print("If a systemd service or Quadlet (~/.config/containers/systemd/) runs this container, turn it off: "
                  "it would replace the runner's container whenever the runner changes it.")
        elif not a.create:
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
