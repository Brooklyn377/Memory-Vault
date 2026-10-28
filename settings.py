"""One place for model settings. The Settings page writes settings.json; every model call reads it."""
import json
import os
from pathlib import Path

FILE = Path("/var/lib/memory-agent/settings.json")
DEFAULTS = {
    "qwen": {"model_file": "Qwen3.5-9B-Q4_K_M.gguf", "context": 16384, "threads": 10, "parallel": 4,
             "temperature": 0.2, "max_tokens": {"sweep": 8000, "ask": 6000}, "thinking": True},
    "gpt": {"model_file": "gpt-oss-20b-MXFP4.gguf", "context": 16384, "threads": 10, "parallel": 4,
            "temperature": 0.2, "max_tokens": {"review": 8000, "chat": 8000}, "reasoning_effort": "low"},
    "cur": {"model_file": "LFM2.5-24B-A2B-Q4_K_M.gguf", "context": 131072, "threads": 10, "parallel": 1,
            "temperature": 0.2, "max_tokens": {"curate": 16000}, "reasoning_effort": "low"},
    # The Assistant: off until you turn it on, with no model chosen, so a new install behaves as before.
    "pa": {"enabled": False, "model_file": "", "context": 16384, "threads": 10, "parallel": 1,
           "temperature": 0.3, "max_tokens": {"chat": 8000}, "reasoning_effort": "low"},
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(base.get(k), dict) and isinstance(v, dict) else v
    return out


def load():
    try:
        saved = json.loads(FILE.read_text())
    except (OSError, ValueError):
        saved = {}
    return {k: _merge(v, saved.get(k)) for k, v in DEFAULTS.items()}


def get(key):
    return load()[key]


def save(new):
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(new, indent=1))
    os.replace(tmp, FILE)
