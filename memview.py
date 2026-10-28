"""Container memory for the Thinking tab: the total, what each model server holds (weights vs working memory), and the safe limit."""
import os

import settings

GIB = 1024 ** 3
PORTS = {"8080": "qwen", "8081": "gpt", "8082": "cur"}


def _meminfo():
    d = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            if v.strip():
                d[k] = int(v.split()[0]) * 1024
    return d


def _expected(key):
    try:
        import gguf_info
        s = settings.get(key)
        return int(gguf_info._fp_safe(s["model_file"], s["context"], s["parallel"]))
    except Exception:
        return 0


def memory():
    mi = _meminfo()
    total = mi.get("MemTotal", 0)
    models = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().split(b"\0")
            if b"llama-server" not in cmd[0]:
                continue
            port = next((cmd[i + 1].decode() for i, a in enumerate(cmd[:-1]) if a == b"--port"), None)
            key = PORTS.get(port)
            if not key:
                continue
            st = {}
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    if k in ("VmRSS", "RssAnon", "RssFile", "RssShmem"):
                        st[k] = int(v.split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            continue
        m = models.setdefault(key, {"rss": 0, "file": 0, "anon": 0})
        m["rss"] += st.get("VmRSS", 0)
        m["file"] += st.get("RssFile", 0) + st.get("RssShmem", 0)
        m["anon"] += st.get("RssAnon", 0)
    for k, m in models.items():
        m["exp"] = _expected(k)
    anon_all = max(0, total - mi.get("MemFree", 0) - mi.get("Cached", 0) - mi.get("Buffers", 0))
    other = max(0, anon_all - sum(m["anon"] for m in models.values()))
    try:
        import gguf_info
        head = gguf_info.HEADROOM
    except Exception:
        head = 4 * GIB
    return {"total": total, "safe": max(0, total - head), "models": models, "other": other,
            "models_total": sum(m["rss"] for m in models.values()),
            "projected": sum(max(m["rss"], m["exp"]) for m in models.values()) + other,
            "free": mi.get("MemFree", 0), "cached": mi.get("Cached", 0) + mi.get("Buffers", 0),
            "available": mi.get("MemAvailable", 0),
            "swap_total": mi.get("SwapTotal", 0), "swap_used": mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)}
