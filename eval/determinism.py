"""Diff the deterministic fields of two runs (e.g. two replay runs on fresh portals, or replay vs. live).

    uv run python -m eval.determinism results_a.json results_b.json

Timing, session IDs and run IDs are expected to differ and are ignored. Exit 1 on any difference.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

FIELDS = ("action", "extracted_type", "source_doc_id", "duplicate_of", "selected_fields", "flags", "reasons",
          "note_sent", "pdf_sha256")


def project(r: dict) -> dict:
    out = {k: r.get(k) for k in FIELDS}
    out["verification"] = (r.get("verification") or {}).get("status")
    return out


def diff(a: list[dict], b: list[dict]) -> list[str]:
    A, B = {r["fax_id"]: project(r) for r in a}, {r["fax_id"]: project(r) for r in b}
    out = [f"{f}: only in {'A' if f in A else 'B'}" for f in sorted(set(A) ^ set(B))]
    for fid in sorted(set(A) & set(B)):
        for k in A[fid]:
            if A[fid][k] != B[fid][k]:
                out.append(f"{fid}.{k}: {A[fid][k]!r} != {B[fid][k]!r}")
    return out


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    a, b = (json.loads(Path(p).read_text())["results"] for p in argv)
    d = diff(a, b)
    print(f"{len(a)} vs {len(b)} faxes; {len(d)} differences in deterministic fields "
          f"({', '.join(FIELDS)}, verification.status)")
    for line in d:
        print("  " + line)
    return 1 if d else 0


if __name__ == "__main__":
    sys.exit(main())
