"""Read a GGUF header and estimate how much memory a model server uses (shown as "expected" on the Thinking tab)."""
import re
import struct
from pathlib import Path

MODELS = Path("/opt/models")
GIB = 1024 ** 3
OVERHEAD = 1 * GIB      # working buffers for one server (estimate)
KV_MARGIN = 1.25        # safety factor on the context cache estimate
_FMT = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
_cache = {}


def header(path):
    path = Path(path)
    key = (str(path), path.stat().st_mtime)
    if key in _cache:
        return _cache[key]
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise ValueError("not a GGUF file")
        f.read(4 + 8)  # version, tensor count
        n = struct.unpack("<Q", f.read(8))[0]

        def s():
            return f.read(struct.unpack("<Q", f.read(8))[0]).decode("utf-8", "replace")

        def val(t):
            if t == 8:
                return s()
            if t == 9:
                it = struct.unpack("<I", f.read(4))[0]
                c = struct.unpack("<Q", f.read(8))[0]
                return [val(it) for _ in range(c)]
            c = _FMT[t]
            return struct.unpack("<" + c, f.read(struct.calcsize(c)))[0]

        out = {}
        for _ in range(n):
            k = s()
            v = val(struct.unpack("<I", f.read(4))[0])
            if not k.startswith("tokenizer."):
                out[k] = v
    _cache[key] = out
    return out


def _g(h, key, default=None):
    v = h.get(f"{h.get('general.architecture', '')}.{key}", default)
    return max(v) if isinstance(v, list) else v


def kv_bytes_per_token(h):
    layers = int(_g(h, "block_count", 0))
    heads = int(_g(h, "attention.head_count", 0))
    if not layers or not heads:
        raise ValueError("unknown model layout")
    kv = int(_g(h, "attention.head_count_kv", heads))
    klen = int(_g(h, "attention.key_length", int(_g(h, "embedding_length", 0)) // heads))
    vlen = int(_g(h, "attention.value_length", klen))
    if klen <= 0:
        raise ValueError("unknown model layout")
    raw = h.get(f"{h.get('general.architecture', '')}.attention.head_count_kv")
    if isinstance(raw, list):  # per-layer KV heads; 0 means that layer keeps no cache
        return sum(int(x) for x in raw) * (klen + vlen) * 2
    every = _g(h, "full_attention_interval")
    full = layers // int(every) if every else layers
    if h.get("general.architecture") == "gemma3" and _g(h, "attention.sliding_window"):
        full = layers // 6  # 5 sliding-window layers for every full-attention layer
    pat = h.get(f"{h.get('general.architecture', '')}.attention.sliding_window_pattern")
    if isinstance(pat, list) and len(pat) == layers and _g(h, "attention.sliding_window"):
        full = sum(1 for x in pat if not x)  # True marks a sliding-window layer, False a full-attention one
    return full * kv * (klen + vlen) * 2  # 2 bytes per value (f16 cache)


def fixed_bytes(h, parallel):
    inner, state = _g(h, "ssm.inner_size"), _g(h, "ssm.state_size")
    if not (inner and state):
        sw = _g(h, "attention.sliding_window")
        if h.get("general.architecture") == "gemma3" and sw:
            layers = int(_g(h, "block_count", 0))
            kv = int(_g(h, "attention.head_count_kv", 1))
            per = kv * (int(_g(h, "attention.key_length", 0)) + int(_g(h, "attention.value_length", 0))) * 2
            return (layers - layers // 6) * per * (int(sw) + 512) * parallel
        pat = h.get(f"{h.get('general.architecture', '')}.attention.sliding_window_pattern")
        if sw and isinstance(pat, list) and pat:
            kv = int(_g(h, "attention.head_count_kv", 1))
            heads = max(1, int(_g(h, "attention.head_count", 1)))
            klen = int(_g(h, "attention.key_length", int(_g(h, "embedding_length", 0)) // heads))
            vlen = int(_g(h, "attention.value_length", klen))
            return sum(1 for x in pat if x) * kv * (klen + vlen) * 2 * (int(sw) + 512) * parallel
        return 0
    layers = int(_g(h, "block_count", 0))
    every = _g(h, "full_attention_interval")
    rec = layers - layers // int(every) if every else layers
    return rec * int(inner) * int(state) * 4 * parallel


def model_size(name):
    m = re.fullmatch(r"(.*)-\d{5}-of-(\d{5})\.gguf", name)
    if m:
        return sum(p.stat().st_size for p in MODELS.glob(f"{m.group(1)}-*-of-{m.group(2)}.gguf"))
    return (MODELS / name).stat().st_size


def footprint(name, ctx, parallel):
    h = header(MODELS / name)
    return model_size(name) + OVERHEAD + fixed_bytes(h, parallel) + int(KV_MARGIN * kv_bytes_per_token(h) * ctx)


def _fp_safe(name, ctx, parallel):
    """Footprint of another server; a model whose header cannot be read counts as its file size plus 2 GiB."""
    try:
        return footprint(name, ctx, parallel)
    except Exception:
        return model_size(name) + OVERHEAD + 2 * GIB
