"""Advanced llama-server flags: what the Settings page offers, how they are checked, and how they become arguments."""
import re
import subprocess
from pathlib import Path

MODELS = Path("/opt/models")
BIN = "/opt/llama.cpp/build/bin/llama-server"
NAME = re.compile(r"[\w.\-]+\.gguf")
KV = ["f16", "q8_0", "q5_1", "q4_0"]
SPEC = [
    {"key": "threads_batch", "flag": "--threads-batch", "kind": "int", "lo": 1, "hi": 64, "label": "Prompt-reading threads (blank = same as threads)"},
    {"key": "batch_size", "flag": "--batch-size", "kind": "int", "lo": 32, "hi": 8192, "min": 1, "label": "Batch size (default 2048)"},
    {"key": "ubatch_size", "flag": "--ubatch-size", "kind": "int", "lo": 32, "hi": 4096, "min": 1, "label": "Micro-batch size (default 512)"},
    {"key": "flash_attn", "flag": "--flash-attn", "kind": "choice", "choices": ["on", "off", "auto"], "label": "Flash attention"},
    {"key": "cache_type_k", "flag": "--cache-type-k", "kind": "choice", "choices": KV, "label": "Key cache type"},
    {"key": "cache_type_v", "flag": "--cache-type-v", "kind": "choice", "choices": KV, "label": "Value cache type (needs flash attention)"},
    {"key": "numa", "flag": "--numa", "kind": "choice", "choices": ["distribute", "isolate", "numactl"], "label": "NUMA mode"},
    {"key": "swa_full", "flag": "--swa-full", "kind": "bool", "label": "Full sliding-window cache (gpt-oss and Gemma 3 only)"},
    {"key": "cache_reuse", "flag": "--cache-reuse", "kind": "int", "lo": 0, "hi": 65536, "label": "Reuse cache chunks of at least N tokens (0 off)"},
    {"key": "no_context_shift", "flag": "--no-context-shift", "kind": "bool", "label": "No context shift (stop instead of dropping old text)"},
    {"key": "reasoning_budget", "flag": "--reasoning-budget", "kind": "int", "lo": -1, "hi": 131072, "label": "Reasoning budget in tokens (-1 unlimited, 0 off)"},
    {"key": "reasoning_format", "flag": "--reasoning-format", "kind": "choice", "choices": ["none", "deepseek", "auto"], "label": "Reasoning format (none puts thinking inside the reply)"},
    {"key": "rope_scaling", "flag": "--rope-scaling", "kind": "choice", "choices": ["none", "linear", "yarn"], "label": "Rope scaling (longer context)"},
    {"key": "rope_freq_scale", "flag": "--rope-freq-scale", "kind": "float", "lo": 0.05, "hi": 4, "min": 0.01, "step": "0.05", "label": "Rope frequency scale (1 divided by the stretch)"},
    {"key": "yarn_orig_ctx", "flag": "--yarn-orig-ctx", "kind": "int", "lo": 0, "hi": 1048576, "label": "YaRN original context (0 = from the model)"},
    # Newer llama.cpp can lower the context you set so it fits free memory. Off by default: the context you set is
    # what runs, and the memory estimate on the card is only a recommendation. Passed only if this build has --fit.
    {"key": "fit", "flag": "--fit", "kind": "choice", "choices": ["off", "on"], "default": "off",
     "label": "Fit context to free memory (default here: off; on lets llama.cpp lower the context you set)"},
    {"key": "draft_model", "flag": "--model-draft", "kind": "model", "label": "Draft model (speculative decoding)"},
    {"key": "draft_max", "flag": "--draft-max", "kind": "int", "lo": 1, "hi": 64, "label": "Draft tokens to guess at most"},
    {"key": "draft_min", "flag": "--draft-min", "kind": "int", "lo": 0, "hi": 64, "label": "Draft tokens to guess at least"},
]
_help = None


def supported():
    global _help
    if _help is None:
        try:
            r = subprocess.run([BIN, "--help"], capture_output=True, text=True, timeout=30)
            _help = set(re.findall(r"--[a-z0-9][a-z0-9-]*", r.stdout + r.stderr))
        except Exception:
            _help = set()
    return _help


def spec():
    sup = supported()
    return [{"key": s["key"], "label": s["label"], "kind": s["kind"], "choices": s.get("choices"),
             "step": s.get("step", "1"), "ok": (not sup) or s["flag"] in sup} for s in SPEC]


def check(flags, model_file="", old=None, warn=None):
    """Checks the flags and returns the ones set. lo and hi are the usual range, min (default lo) the lowest that can
    work: a changed number outside either only adds a warning to warn. Nothing numeric is refused."""
    by, sup, out, old = {s["key"]: s for s in SPEC}, supported(), {}, old or {}
    warn = [] if warn is None else warn
    for k, v in (flags or {}).items():
        s = by.get(k)
        if s is None:
            raise ValueError("unknown setting " + str(k))
        if v is None or v is False or str(v).strip() == "":
            continue
        if sup and s["flag"] not in sup:
            raise ValueError(f"this llama.cpp build has no {s['flag']}")
        kind = s["kind"]
        if kind == "bool":
            out[k] = True
        elif kind == "choice":
            if str(v) not in s["choices"]:
                raise ValueError(f"{s['label']}: pick one of " + ", ".join(s["choices"]))
            out[k] = str(v)
        elif kind == "model":
            name = str(v)
            if not NAME.fullmatch(name) or not (MODELS / name).is_file():
                raise ValueError("the draft model must be a file in the model list")
            if name == model_file:
                raise ValueError("the draft model must be a different file")
            out[k] = name
        else:
            x = float(v)
            if x < s.get("min", s["lo"]) and x != old.get(k):
                warn.append(f"{s['label']}: {x:g} is below {s.get('min', s['lo'])}, the lowest it can be; the server may not start "
                            "(a server that does not come up is rolled back).")
            elif not s["lo"] <= x <= s["hi"] and x != old.get(k):
                warn.append(f"{s['label']}: {x:g} is outside the usual {s['lo']:g} to {s['hi']:g}.")
            out[k] = int(x) if kind == "int" else x
    if out.get("cache_type_v", "f16") != "f16" and out.get("flash_attn") == "off":
        warn.append("A quantized value cache needs flash attention on or auto; with it off the server may not start "
                    "(a server that does not come up is rolled back).")
    if ("draft_max" in out or "draft_min" in out) and "draft_model" not in out:
        warn.append("Draft token counts do nothing without a draft model.")
    return out


def build(s):
    f, parts = s.get("flags") or {}, []
    for sp in SPEC:
        v = f.get(sp["key"])
        if (v is None or v == "") and sp.get("default") and sp["flag"] in supported():
            v = sp["default"]   # a flag whose default here differs from llama.cpp's, for builds that have it
        if v is None or v is False or v == "":
            continue
        if sp["kind"] == "bool":
            parts.append(sp["flag"])
        elif sp["kind"] == "model":
            parts += [sp["flag"], f"/opt/models/{v}"]
        else:
            parts += [sp["flag"], str(v)]
    return " ".join(parts)
