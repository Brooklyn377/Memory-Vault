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

  If llama-server dies by itself, the runner starts it again (and waits longer after each quick death). Stopping it
  from the card keeps it stopped.

  A llama-server program on this machine:
      python3 model_runner.py --setup --llama /path/to/llama-server --models /path/to/models

Setup writes model_runner.json next to this file and prints a token once, for the Settings page. Then run
`python3 model_runner.py` (as a service, or in a terminal).

Safety: every request needs the token. Only model files inside the models folder can be used, numbers must be numbers,
and a flag is passed only if that llama-server's own --help lists it, with a plain value. Commands are run as lists,
never through a shell, and only the configured program or container engine with fixed subcommands. A change that does
not come up is rolled back to the previous settings, and the reply carries llama-server's last lines.

Updates: the agent asks for one with a commit and the sha256 it expects; the runner downloads model_runner.py from GitHub
at that commit (from REPO, or "repo" in model_runner.json, never from the request), installs it only if the sha256
matches and it is valid Python, and restarts."""
import argparse
import hashlib
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

state = {"proc": None, "lock": threading.Lock(), "help": None, "dl": {}, "stop": set(),
         "split": None}   # where llama-server put the model at its last start (see split), read once it is ready
HF = ("huggingface.co", "hf.co")
REPO = "Brooklyn377/Memory-Vault"   # where /update downloads this file from; "repo" in model_runner.json overrides it
SELF = Path(__file__).resolve()
RUNNING = hashlib.sha256(SELF.read_bytes()).hexdigest()   # the file this process runs: a later swap does not change it
STARTED = time.time()
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


def _gpu_args(k):
    """The GPU part of a container's command: its devices, and the extra arguments a GPU needs (for example --gpus all)."""
    out = []
    for d in k.get("devices", []):
        out += ["--device", d]
    return out + list(k.get("run_args", []))


def flags_supported(c):
    if state["help"] is None:
        k = c.get("container")
        cmd = [k["engine"], "run", "--rm", *_gpu_args(k), k["image"], "--help"] if k else [c["llama"], "--help"]
        try:
            r = _run(cmd, 300)
            state["help"] = sorted(set(re.findall(r"--[a-z0-9][a-z0-9-]*", r.stdout + r.stderr)))
        except Exception:
            state["help"] = []
    return state["help"]


def models(c):
    root = Path(c["models"])
    return [{"name": p.name, "size": p.stat().st_size} for p in sorted(root.glob("*.gguf"))] if root.is_dir() else []


QUANT = re.compile(r"[-_.](?:UD-)?(?:I?Q\d\w*|F16|BF16|F32|MXFP4)(?:-\d{5}-of-\d{5})?$", re.I)


def _gguf_names(path):
    """The names inside a GGUF file ({"name": ..., "basename": ...}), from the first entries of its header; {} if unreadable."""
    import struct
    size, out = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}, {}
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return out
            f.read(12)   # version, tensor count

            def rd(fmt):
                return struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]

            def s():
                return f.read(rd("<Q")).decode("utf-8", "replace")

            def skip(t):
                if t == 8:
                    s()
                elif t == 9:
                    it, n = rd("<I"), rd("<Q")
                    for _ in range(n):
                        skip(it)
                else:
                    f.read(size[t])
            for _ in range(rd("<Q")):
                k, t = s(), rd("<I")
                if not k.startswith("general."):
                    break   # the general.* entries come first
                if t == 8 and k in ("general.name", "general.basename"):
                    out[k.split(".", 1)[1]] = s()
                else:
                    skip(t)
    except (OSError, struct.error, KeyError):
        pass
    return out


def find_mmproj(root, model):
    """The vision file (mmproj) of this model file, in the same folder, or None. A file with the model's name in its own name
    counts first; then one whose name inside the file is the model's. Several: F16, then BF16, then Q8_0."""
    root, norm = Path(root), (lambda x: re.sub(r"[^a-z0-9]", "", str(x).lower()))
    cands = [p for p in sorted(root.glob("*.gguf")) if "mmproj" in p.name.lower()]
    if not cands or not model:
        return None
    base = norm(QUANT.sub("", str(model)[:-5] if str(model).endswith(".gguf") else str(model)))
    hit = [p for p in cands if len(base) >= 6 and base in norm(p.name)]
    if not hit:
        mine = {norm(v) for v in _gguf_names(root / model).values() if len(norm(v)) >= 6}
        same = lambda a, b: a in b or b in a
        hit = [p for p in cands if any(same(a, norm(v)) for a in mine for v in _gguf_names(p).values() if len(norm(v)) >= 6)]
    rank = lambda p: (0 if "f16" in p.name.lower() and "bf16" not in p.name.lower() else 1 if "bf16" in p.name.lower() else 2 if "q8" in p.name.lower() else 3, p.name)
    return min(hit, key=rank).name if hit else None


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
    flags = dict(s.get("flags") or {})
    mm = find_mmproj(root, model) if flags.pop("--mmproj", None) == "auto" else None   # "auto": the vision file next to the model, if any
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
    if mm:
        args += ["--mmproj", inside(mm)]
    own = list(MANAGED) + list(flags) + (["--mmproj"] if mm else [])
    if k:   # the adopted command, minus what the card sets, then the card's values
        return _strip(k["base_args"], own) + args
    return [c["llama"]] + _strip(c.get("base_args") or [], own) + args + ["--host", c.get("llama_host", "0.0.0.0"), "--port", str(c["llama_port"])]


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
    state["split"] = None   # read again from this start's output once it is ready
    k = c.get("container")
    if k:
        cmd = [k["engine"], "run", "--detach", "--name", k["name"]]
        for v in k.get("binds", []):
            cmd += ["--volume", v]
        for p in k.get("ports", []):
            cmd += ["--publish", p]
        cmd += _gpu_args(k)
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


# ---------- the prompt cache in RAM: the machine's memory limit minus a margin ----------
CG = Path("/sys/fs/cgroup")
MIB = 1024 * 1024


def _limit():
    """This container's memory limit in bytes, or None: from its cgroup when it has a limit, else from /proc/meminfo (in
    a Proxmox LXC that shows the container's own limit)."""
    try:
        lim = (CG / "memory.max").read_text().strip()
        if lim.isdigit():
            return int(lim)
    except OSError:
        pass
    try:
        mi = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in Path("/proc/meminfo").read_text().splitlines() if len(l.split()) >= 2}
        return mi["MemTotal"]
    except (OSError, ValueError, KeyError, IndexError):
        return None


def _cache_mib(note=True):
    """The prompt cache size: the memory limit minus a margin of 10% (at least 512 MiB). Nothing is measured, so
    llama-server starts once. 0 (cache off) when the limit cannot be read or leaves nothing."""
    lim = _limit()
    if lim is None:
        if note:
            _note("prompt cache in RAM: off (the memory limit could not be read)")
        return 0
    mib = max(0, (lim - max(lim // 10, 512 * MIB)) // MIB)
    if note:
        _note(f"prompt cache in RAM: {mib:,} MiB (memory limit {lim // MIB:,} MiB minus the margin)" if mib else
              "prompt cache in RAM: off (the memory limit leaves nothing after the margin)")
    return int(mib)


# ---------- for the Stats page: this machine's memory, its graphics cards, and where llama-server put the model ----------
def ram():
    """{"limit", "used", "cache", "free"} in bytes for this container, or None. Used is what programs hold; the file
    cache (model files read from disk count here) can be freed by the kernel."""
    try:
        lim = (CG / "memory.max").read_text().strip()
        if lim.isdigit():
            cur = int((CG / "memory.current").read_text())
            stat = dict(l.split() for l in (CG / "memory.stat").read_text().splitlines() if len(l.split()) == 2)
            file = int(stat.get("file", 0))
            return {"limit": int(lim), "used": max(0, cur - file), "cache": file, "free": max(0, int(lim) - cur)}
    except (OSError, ValueError):
        pass
    try:
        mi = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in Path("/proc/meminfo").read_text().splitlines() if len(l.split()) >= 2}
        cache = mi.get("Cached", 0) + mi.get("Buffers", 0)
        return {"limit": mi["MemTotal"], "used": max(0, mi["MemTotal"] - mi["MemFree"] - cache), "cache": cache, "free": mi["MemFree"]}
    except (OSError, ValueError, KeyError, IndexError):
        return None


def gpus():
    """The graphics cards this machine can use, with their memory in bytes: AMD from the driver's files (only the cards
    whose device is in this container's /dev/dri), NVIDIA from nvidia-smi. [] when none can be read."""
    out = []
    drm = Path("/sys/class/drm")
    for card in sorted(drm.glob("card[0-9]*")) if drm.is_dir() else []:
        dev = card / "device"
        if not re.fullmatch(r"card\d+", card.name) or not (dev / "mem_info_vram_total").exists():
            continue
        nodes = [card.name] + [p.name for p in (dev / "drm").glob("renderD*")] if (dev / "drm").is_dir() else [card.name]
        if not any((Path("/dev/dri") / n).exists() for n in nodes):
            continue   # a card of the host that was not passed into this container
        try:
            rd = lambda f: int((dev / f).read_text().strip())
            name = ""
            for f in ("product_name",):
                if (dev / f).exists():
                    name = (dev / f).read_text().strip()
            if not name:
                ue = dict(l.split("=", 1) for l in (dev / "uevent").read_text().splitlines() if "=" in l)
                name = f"AMD card {ue.get('PCI_SLOT_NAME', card.name)}"
            out.append({"name": name, "total": rd("mem_info_vram_total"), "used": rd("mem_info_vram_used"),
                        "gtt": rd("mem_info_gtt_used") if (dev / "mem_info_gtt_used").exists() else None})
        except (OSError, ValueError):
            continue
    if shutil.which("nvidia-smi"):
        r = _run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"], 10)
        for line in r.stdout.splitlines() if r.returncode == 0 else []:
            p = [x.strip() for x in line.split(",")]
            if len(p) == 3 and p[1].isdigit() and p[2].isdigit():
                out.append({"name": p[0], "used": int(p[1]) * MIB, "total": int(p[2]) * MIB, "gtt": None})
    return out


_BUF = re.compile(r"(\S+)\s+(model|KV|RS|compute|output) buffer size\s*=\s*([\d.]+) MiB")
_GPU_DEV = re.compile(r"(Vulkan|CUDA|ROCm|HIP|SYCL|Metal|MTL)\d+")


def _startup(c):
    """llama-server's output since its last start (only the start matters: the buffers are printed then)."""
    k = c.get("container")
    if k:
        r = _run([k["engine"], "logs", k["name"]], 60)
        return (r.stdout or "") + (r.stderr or "")
    try:
        with open(LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4 * MIB))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    starts = [m.start() for m in re.finditer(r"^=== .*--model", text, flags=re.M)]
    return text[starts[-1]:] if starts else ""


def parse_split(text):
    """llama-server's startup lines -> {"layers": [on the GPUs, all] or None, "devices": [{"dev", "name", "weights",
    "context", "working"} in MiB], "ram": {"weights", "context", "working"}}. Buffers of a GPU device count for it; the
    CPU ones and the *_Host ones are in system RAM."""
    names = {}
    for m in re.finditer(r"ggml_vulkan: (\d+) = ([^|]+?)\s*(?:\(|\|)", text):
        names.setdefault(f"Vulkan{m.group(1)}", m.group(2).strip())
    for m in re.finditer(r"Device (\d+): ([^,\n]+),", text):
        names.setdefault(f"CUDA{m.group(1)}", m.group(2).strip())
    for m in re.finditer(r"using device (\S+) \((.*)\)\s+-", text):
        names.setdefault(m.group(1), m.group(2).strip())
    kind = {"model": "weights", "KV": "context", "RS": "context", "compute": "working", "output": "working"}
    devs, ram = {}, {"weights": 0.0, "context": 0.0, "working": 0.0}
    for m in _BUF.finditer(text):
        where, k, mib = m.group(1), kind[m.group(2)], float(m.group(3))
        if _GPU_DEV.fullmatch(where):
            d = devs.setdefault(where, {"dev": where, "name": names.get(where, ""), "weights": 0.0, "context": 0.0, "working": 0.0})
            d[k] += mib
        else:
            ram[k] += mib
    lay = re.findall(r"offloaded (\d+)/(\d+) layers to GPU", text)
    return {"layers": [int(lay[-1][0]), int(lay[-1][1])] if lay else None,
            "devices": [devs[k] for k in sorted(devs)], "ram": ram}


def split(c):
    """Where llama-server put the model, read once after each start (when it is ready), or None before that."""
    if state["split"] is None and running(c) and health(c) == "ready":
        state["split"] = parse_split(_startup(c))
    return state["split"]


def _launch(c, s, wait=False):
    """Start llama-server once with these settings and the prompt cache size. -> True if it is up (always True when not
    waiting)."""
    args = _strip(build(c, s), ["--cache-ram"])
    if "--cache-ram" in flags_supported(c):
        args += ["--cache-ram", str(_cache_mib())]
    _start(c, args)
    return _wait_ready(c) if wait else True


def _launch_bg(c, s):
    """_launch in the background (it waits for llama-server to load)."""
    def run():
        with state["lock"]:
            try:
                if not load().get("stopped"):
                    _launch(c, s)
            except (ValueError, RuntimeError) as e:
                _note(f"could not start llama-server: {e}")
    threading.Thread(target=run, daemon=True).start()


def apply(s):
    """Restart llama-server with these settings; roll back to the previous ones if it does not come up."""
    c = load()
    args = build(c, s)
    with state["lock"]:
        old = c.get("settings")
        _stop(c)
        try:
            ok = _launch(c, s, wait=True)
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
                _launch(c, old, wait=True)
            except (ValueError, RuntimeError) as e:   # the old ones cannot run either (their model file was deleted): say so
                c["settings"], c["stopped"] = s, True   # what you asked for, so a start does not go back to a file that is gone
                save(c)
                return {"ok": False, "kept": False, "log": why,
                        "error": f"llama-server did not come up with these settings, and the previous ones could not be put back "
                                 f"either: {e}. Nothing is running; pick a model under Model on that server and press Apply all"}
        return {"ok": False, "kept": True, "error": "llama-server did not come up with these settings; the previous ones were put back",
                "log": why}


def _note(text):
    try:
        with open(LOG, "a") as f:
            f.write(f"\n=== {time.ctime()} {text}\n")
    except OSError:
        pass


def watchdog(interval=15):
    """Start llama-server again when it died by itself (a crash, the machine ran out of memory), as long as you did not
    stop it from the card. Each quick death doubles the wait, up to 5 minutes, so a server that cannot run is not
    started in a tight loop."""
    wait, started = interval, 0.0
    while True:
        time.sleep(wait)
        try:
            c = load()
            if not c.get("settings") or c.get("stopped") or state["lock"].locked() or running(c):
                if time.time() - started > 600:
                    wait = interval
                continue
            with state["lock"]:
                if running(load()) or load().get("stopped"):
                    continue
                quick = time.time() - started < 120
                _stop(c)   # an exited container still holds its name
                _launch(c, c["settings"])
                started = time.time()
                _note("llama-server was not running; the runner started it again" + (" (it died again soon after the last start)" if quick else ""))
            wait = min(300, wait * 2) if quick else interval
        except Exception as e:
            _note(f"could not start llama-server again: {type(e).__name__}: {str(e)[:200]}")
            wait = min(300, wait * 2)


# ---------- updating itself: the agent asks, the file comes from GitHub ----------
def version(c):
    """What this runner runs: its file's sha256, the commit it came from (if an update put it here) and when it started."""
    u = c.get("update") or {}
    return {"sha256": RUNNING, "commit": u.get("commit", "") if u.get("sha256") == RUNNING else "", "started": STARTED}


def update(body):
    """Install model_runner.py from GitHub at the agent's commit, if it is the file the agent has, and restart. The request
    carries only the commit and the expected sha256: the file itself always comes from GitHub, from this runner's repo."""
    commit, want = str(body.get("commit", "")), str(body.get("sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        return {"ok": False, "kind": "bad", "error": f'the commit must be 40 characters 0-9 and a-f; got "{commit[:60]}"'}
    if not re.fullmatch(r"[0-9a-f]{64}", want):
        return {"ok": False, "kind": "bad", "error": f'the sha256 must be 64 characters 0-9 and a-f; got "{want[:80]}"'}
    if want == RUNNING:
        return {"ok": True, "same": True}
    c = load()
    repo = str(c.get("repo") or REPO)
    if not re.fullmatch(r"[\w.\-]+/[\w.\-]+", repo):
        return {"ok": False, "kind": "bad", "error": f'"repo" in model_runner.json must be owner/name; got "{repo[:80]}"'}
    url = f"https://raw.githubusercontent.com/{repo}/{commit}/model_runner.py"
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read(4 * CHUNK + 1)
        if len(data) > 4 * CHUNK:
            raise ValueError("the file is larger than 4 MB")
    except Exception as e:
        return {"ok": False, "kind": "download",
                "error": f"could not download model_runner.py from {url}: {type(e).__name__}: {str(e)[:150]}. Nothing was changed."}
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        return {"ok": False, "kind": "mismatch",
                "error": f"the downloaded file does not match the agent's copy (expected sha256 {want}, got {got}), so it was not "
                         f"installed. This runner downloads from {repo}; check that the agent uses the same repository."}
    try:
        compile(data, str(SELF), "exec")
    except (SyntaxError, ValueError) as e:
        return {"ok": False, "kind": "python", "error": f"the downloaded file is not valid Python ({type(e).__name__}: {e}), so it was not installed."}
    tmp = SELF.with_name(SELF.name + ".new")
    tmp.write_bytes(data)
    os.chmod(tmp, SELF.stat().st_mode & 0o7777)
    os.replace(tmp, SELF)
    c["update"] = {"commit": commit, "sha256": got}
    save(c)
    msg = f"updated model_runner.py to commit {commit[:7]} from GitHub; restarting"
    _note(msg)
    print(msg, flush=True)
    threading.Thread(target=_restart, daemon=True).start()
    return {"ok": True}


def _restart():
    """Run the new file in this same process (as a service or in a terminal alike). A llama-server program it started is
    stopped first so the new runner can start it again on its port; a container keeps running."""
    time.sleep(1)   # the reply to /update goes out first
    with state["lock"]:
        c = load()
        if not c.get("container"):
            _stop(c)
        os.execv(sys.executable, [sys.executable, str(SELF)] + sys.argv[1:])


def status():
    c = load()
    return {"version": version(c), "running": running(c), "health": health(c), "settings": c.get("settings"), "stopped": bool(c.get("stopped")),
            "models": models(c), "flags": flags_supported(c), "llama_port": c["llama_port"],
            "container": (c.get("container") or {}).get("name"), "log": _tail(c, 6), "downloads": downloads(c),
            "ram": ram(), "gpus": gpus(), "split": split(c), "cache_mib": _cache_mib(note=False) if "--cache-ram" in flags_supported(c) else None}


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
            if self.path == "/update":
                return self._send(200, update(body))
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
                    raise ValueError("nothing to start yet: pick a model under Model on that server on a card, and press Apply all")
                build(c, c["settings"])   # settings that cannot be used are refused here, not later in the background
                with state["lock"]:
                    c["stopped"] = False
                    save(c)
                    if not running(c):
                        _launch_bg(c, c["settings"])
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
    ap.add_argument("--security-opt", action="append", default=[], metavar="OPT", help="with --create: another --security-opt for the container (ROCm wants seccomp=unconfined)")
    ap.add_argument("--run-arg", action="append", default=[], metavar="ARG", help="with --create: another argument for the container engine's run, written --run-arg=--gpus --run-arg=all (Docker with an NVIDIA GPU)")
    ap.add_argument("--base-arg", action="append", default=[], metavar="ARG", help="with --create or --llama: another argument for llama-server itself, written --base-arg=--n-gpu-layers --base-arg=999")
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
            dev = [d if ":" in d or "=" in d else f"{d}:{d}" for d in a.device]   # nvidia.com/gpu=all is a CDI name, kept as it is
            k = {"engine": a.engine, "name": a.name, "image": a.image, "binds": [f"{models}:/models:ro"],
                 "ports": [f"{a.llama_port}:8080"], "devices": dev, "security_opt": ["label=disable"] + a.security_opt,
                 "group_add": a.group_add, "run_args": a.run_arg, "restart": "unless-stopped", "models_in": "/models",
                 "base_args": ["--host", "0.0.0.0", "--port", "8080", "--jinja"] + a.base_arg + (["--api-key", a.api_key] if a.api_key else [])}
            c = dict(old, container=k, models=str(models), llama_port=a.llama_port)
            c.pop("llama", None)
            c.pop("settings", None)
        elif a.llama and Path(a.llama).exists() and a.models and Path(a.models).is_dir():
            c = dict(old, llama=str(Path(a.llama).resolve()), models=str(Path(a.models).resolve()), llama_port=a.llama_port,
                     base_args=(["--jinja"] if a.jinja else []) + a.base_arg + (["--api-key", a.api_key] if a.api_key else []))
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
    if c.get("settings") and not c.get("stopped") and not running(c):   # a container still running from before keeps running
        _launch_bg(c, c["settings"])
    threading.Thread(target=watchdog, daemon=True).start()
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
