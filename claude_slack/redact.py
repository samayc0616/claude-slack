"""Scrub secrets before posting to Slack."""
from __future__ import annotations

import re

# Conservative patterns. Errs toward over-redaction; tune as you find leaks.
PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "sk-ant-***"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "xox?-***"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"), "ghp_***"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA***"),
    (re.compile(r'(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*["\']?([^"\'\s,]{8,})'),
     r"\1=***"),
    (re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----.*?-----END [A-Z ]+PRIVATE KEY-----", re.S),
     "[REDACTED PRIVATE KEY]"),
]


def scrub(text: str) -> str:
    out = text
    for pat, repl in PATTERNS:
        out = pat.sub(repl, out)
    return out
