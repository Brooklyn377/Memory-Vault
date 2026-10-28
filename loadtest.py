import json
import sys
import time
import urllib.request
from pathlib import Path

URL = "http://127.0.0.1:8080/v1/chat/completions"
src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/opt/llama.cpp/README.md")
chars = int(sys.argv[2]) if len(sys.argv) > 2 else 12000

text = src.read_text(errors="replace")[:chars]
payload = {
    "messages": [{"role": "user",
                  "content": "Summarize this document in 5 short bullet points:\n\n" + text}],
    "max_tokens": 200,
    "temperature": 0.2,
}
req = urllib.request.Request(URL, data=json.dumps(payload).encode(),
                             headers={"Content-Type": "application/json"})
start = time.time()
print(f"sending {len(text)} characters at {time.strftime('%H:%M:%S')}", flush=True)
with urllib.request.urlopen(req, timeout=7200) as r:
    data = json.load(r)
minutes = (time.time() - start) / 60
usage = data.get("usage", {})
print(f"finished in {minutes:.1f} min: prompt tokens {usage.get('prompt_tokens')}, "
      f"generated tokens {usage.get('completion_tokens')}")
msg = data["choices"][0]["message"]
print((msg.get("content") or msg.get("reasoning_content") or "")[:500])
