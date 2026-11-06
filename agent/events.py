"""Structured JSONL event log. Patient identifiers are redacted before writing."""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

_REDACT_KEYS = {"patient_name", "dob", "patient", "date_of_birth"}


def _hash(v: str) -> str:
    return "sha1:" + hashlib.sha1(v.encode()).hexdigest()[:10]


def redact(obj):
    if isinstance(obj, dict):
        return {k: (_hash(str(v)) if k in _REDACT_KEYS and v else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


class EventLog:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, fax_id: str | None = None, **data) -> None:
        rec = {"ts": round(time.time(), 3), "run": self.run_id, "event": event}
        if fax_id:
            rec["fax_id"] = fax_id
        safe = redact(data)
        rec.update(safe)
        line = json.dumps(rec, default=str)
        with self.path.open("a") as f:
            f.write(line + "\n")
        brief = " ".join(f"{k}={v}" for k, v in safe.items() if not isinstance(v, (dict, list)))[:160]
        print(f"[{event}] {fax_id or ''} {brief}", file=sys.stderr, flush=True)
