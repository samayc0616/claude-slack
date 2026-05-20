"""Append-only JSONL audit log. One row per event, connect, api_call, turn_complete."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import AUDIT_PATH


class Audit:
    def __init__(self, path: Path = AUDIT_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, **fields) -> None:
        row = {"ts": time.time(), "kind": kind}
        row.update(fields)
        try:
            with self.path.open("a") as f:
                f.write(json.dumps(row) + "\n")
        except Exception:
            pass  # never block hot path on audit failure
