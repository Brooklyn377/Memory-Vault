"""Streamed model call that also logs the model's thinking, for the Thinking tab."""
import fcntl
import json
import time
import urllib.request
from pathlib import Path

DIR = Path("/var/lib/memory-agent/think")
ABORT = Path("/var/lib/memory-agent/abort")   # one file per model: its time cuts off every call started before it
MAX_BYTES = 5 * 1024 ** 3   # total size of all log files
FLUSH_CHARS = 200           # write a log line once this much text is waiting
FLUSH_SECS = 1.5            # or once this many seconds have passed


def _write(rec):
    DIR.mkdir(parents=True, exist_ok=True)
    path = DIR / time.strftime("think-%Y%m%d-%H.jsonl", time.gmtime())
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def prune():
    files = sorted(DIR.glob("think-*.jsonl"))
    total = sum(p.stat().st_size for p in files)
    while len(files) > 1 and total > MAX_BYTES:
        p = files.pop(0)
        try:
            total -= p.stat().st_size
            p.unlink()
        except OSError:
            pass


TOTALS = DIR / "totals.json"


def backfill():
    """Add up the token counts of every finished call still in the log files."""
    sums = {}
    for p in sorted(DIR.glob("think-*.jsonl")):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"k": "end"' not in line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    sums[r.get("m")] = sums.get(r.get("m"), 0) + int(r.get("n", 0))
        except OSError:
            pass
    return sums


def totals():
    try:
        return json.loads(TOTALS.read_text())
    except (OSError, ValueError):
        return backfill()


def add_total(model, n):
    """Add one finished call to the lifetime counter. The lock stops two services overwriting each other."""
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        with open(DIR / "totals.lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            t = totals()
            t[model] = t.get(model, 0) + n
            tmp = TOTALS.with_suffix(".tmp")
            tmp.write_text(json.dumps(t))
            tmp.replace(TOTALS)
    except OSError:
        pass


def _slots(model):
    """How many parallel slots this server has, from the saved settings. A pinned slot beyond that waits forever."""
    try:
        import settings
        return int(settings.get(model)["parallel"])
    except Exception:
        return 99


SAMPLERS = ("top_p", "top_k", "min_p", "typical_p", "repeat_penalty", "repeat_last_n", "presence_penalty",
            "frequency_penalty", "mirostat", "mirostat_tau", "mirostat_eta", "seed", "dry_multiplier", "dry_base",
            "dry_allowed_length", "xtc_probability", "xtc_threshold")


def _with_prompt(label, body):
    """Swap in a system prompt edited on the Settings page. Returns the body and a note for the call log."""
    try:
        import prompts
        return prompts.apply(body, label)
    except Exception:
        return body, ""


def _with_sampling(model, label, body):
    """Add the optional settings from the Settings page. A blank setting is left out, so the server default applies."""
    try:
        import settings
        s = settings.get(model)
        extra = {k: v for k, v in (s.get("sampling") or {}).items() if k in SAMPLERS and v is not None and v != ""}
        if s.get("json_mode") and label != "chat":
            extra["response_format"] = {"type": "json_object"}
        tb = s.get("thinking_budget")
        if tb not in (None, "") and "seed-oss" in str(s.get("model_file", "")).lower():
            kw = dict(body.get("chat_template_kwargs") or {})
            kw["thinking_budget"] = int(tb)
            extra["chat_template_kwargs"] = kw
        return dict(body, **extra) if extra else body
    except Exception:
        return body


def describe(label, body):
    """A short plain description of what a call is about, for the Queue and History pages."""
    import re
    try:
        user = str(body["messages"][-1]["content"])
    except Exception:
        return ""
    first = next((l.strip() for l in user.splitlines() if l.strip()), "")
    if label == "chat":
        q = re.split(r"\n(?:QUESTION|CORRECTION): ", user)[-1]
        return "Answering: " + " ".join(q.split())[:90]
    m = re.match(r"CURRENT FILE \((.+?)\)", first)
    if m:
        return "Checking chats for changes to " + m.group(1)
    if first.startswith("RECAP:"):
        return "Judging what the Sweeper read in one chunk"
    if first.startswith("OPEN TASKS"):
        return "Checking open tasks against new facts"
    if first.startswith("ALL NOTES"):
        return "Map pass over the whole vault"
    m = re.match(r"NOTE \(?([^\s):]+)", first)
    if m:
        return ("Reviewing " if label == "curate" else "Reading ") + m.group(1)
    return " ".join(first.split())[:110]


import threading

_auth = threading.local()


class _AuthHandler(urllib.request.BaseHandler):
    """Adds the external server's API key to a call. Only active while a call to that server is running."""
    handler_order = 100

    def http_request(self, req):
        k = getattr(_auth, "key", "")
        if k and not req.has_header("Authorization"):
            req.add_unredirected_header("Authorization", "Bearer " + k)
        return req

    https_request = http_request


urllib.request.install_opener(urllib.request.build_opener(_AuthHandler))


class Aborted(RuntimeError):
    """The call was cut off because you switched or stopped its server. Callers queue the work again; not a failure."""


def abort(model):
    """Cut off every call to this model that is running now, in every service: switching or stopping its server."""
    ABORT.mkdir(parents=True, exist_ok=True)
    (ABORT / model).write_text(str(time.time()))


def _aborted_since(model, t):
    try:
        return float((ABORT / model).read_text()) >= t
    except (OSError, ValueError):
        return False


def _watch(model, started, resp, done, cut):
    """Close the connection as soon as the model is aborted; the server then stops generating."""
    import socket
    while not done.wait(0.5):
        if _aborted_since(model, started):
            cut.set()
            try:
                resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                resp.close()
            except Exception:
                pass
            return


def _chat_remote(model, body, label):
    import remote
    c = remote.raw(model)
    b = dict(body)
    if c.get("model"):
        b["model"] = c["model"]
    if c.get("api") != "llamacpp":
        b.pop("chat_template_kwargs", None)
    _auth.key = remote.saved_key(model)
    try:
        return _chat_local(model, remote.endpoint(model), b, label, None)
    finally:
        _auth.key = ""


def chat(model, endpoint, body, label, slot=None):
    try:
        import remote
        external = remote.enabled(model)
    except Exception:
        external = False
    try:
        import power
        power.wait_on(model)   # a model you unloaded or stopped: its calls wait here, local or external
    except Exception:
        pass
    if external:
        return _chat_remote(model, body, label)
    return _chat_local(model, endpoint, body, label, slot)


def _chat_local(model, endpoint, body, label, slot=None):
    """Same job as the old urlopen block, but streamed. Returns {"content": ..., "reasoning_content": ...}."""
    call = str(time.time_ns())
    body = _with_sampling(model, label, body)
    body, pnote = _with_prompt(label, body)

    def rec(k, s="", n=0):
        _write({"t": time.time_ns(), "m": model, "c": call, "k": k, "s": s, "n": n})

    st = {"kind": None, "buf": "", "n": 0, "last": time.time()}

    def flush():
        if st["buf"]:
            rec(st["kind"], st["buf"], st["n"])
        st.update(buf="", n=0, last=time.time())

    req = urllib.request.Request(endpoint.rstrip("/") + "/chat/completions",
                                 data=json.dumps(dict(body, stream=True, cache_prompt=True, **({"id_slot": slot} if slot is not None and slot < _slots(model) else {}))).encode(),
                                 headers={"Content-Type": "application/json"})
    out = {"content": [], "reasoning_content": []}
    total, started = 0, time.time()
    import swap
    swap.wait_for(endpoint)
    if _aborted_since(model, started):
        raise Aborted(f"{model} was switched or stopped")
    _write({"t": time.time_ns(), "m": model, "c": call, "k": "start", "s": label, "n": 0, "w": describe(label, body) + pnote})
    done, cut = threading.Event(), threading.Event()
    try:
        with urllib.request.urlopen(req, timeout=None) as r:
            threading.Thread(target=_watch, args=(model, started, r, done, cut), daemon=True).start()
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:") or line.endswith("[DONE]"):
                    continue
                try:
                    delta = json.loads(line[5:])["choices"][0]["delta"]
                except (ValueError, KeyError, IndexError):
                    continue
                for field, k in (("reasoning_content", "r"), ("content", "o")):
                    piece = delta.get(field) or ""
                    if not piece:
                        continue
                    out[field].append(piece)
                    if st["kind"] != k:
                        flush()
                        st["kind"] = k
                    st["buf"] += piece
                    st["n"] += 1
                    total += 1
                    if "\n" in piece or len(st["buf"]) >= FLUSH_CHARS or time.time() - st["last"] >= FLUSH_SECS:
                        flush()
    except Exception:
        if not cut.is_set():
            raise
    finally:
        done.set()
        flush()
        add_total(model, total)
        rec("end", f"{label} {time.time() - started:.0f}s" + (" (cut off: server switched or stopped)" if cut.is_set() else ""), total)
        prune()
    if cut.is_set():
        raise Aborted(f"{model} was switched or stopped")
    return {"content": "".join(out["content"]), "reasoning_content": "".join(out["reasoning_content"])}
