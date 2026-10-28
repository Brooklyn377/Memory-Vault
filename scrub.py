import re
import sys
from pathlib import Path

PATTERNS = [
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL), "[REDACTED:private_key]"),
    ("known_token", re.compile(
        r"\b(?:sk-[A-Za-z0-9_\-]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9\-]{10,})\b"),
        "[REDACTED:token]"),
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
        "[REDACTED:jwt]"),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
        "Bearer [REDACTED]"),
    ("assignment", re.compile(
        r"(?i)\b([\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key)[\w-]*)(\s*[:=]\s*)"
        r"(?:\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s\"']{4,})"),
        r"\1\2[REDACTED]"),
    ("spoken", re.compile(r"(?i)\b(password|passcode)\s+(is|was)\s+\S+"),
        r"\1 \2 [REDACTED]"),
]

def scrub(text):
    counts = {}
    for name, pattern, replacement in PATTERNS:
        text, n = pattern.subn(replacement, text)
        if n:
            counts[name] = n
    return text, counts

def main():
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    cleaned, counts = scrub(src.read_text(encoding="utf-8"))
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(cleaned, encoding="utf-8")
    print(f"{src.name}: {counts if counts else 'nothing redacted'}")

if __name__ == "__main__":
    main()
