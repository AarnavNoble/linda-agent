"""Per-fax result records and the results.json / results.csv / summary.md artifacts."""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path


def build_result(rec: dict, action: str, verification: dict, errors: list[str], sessions_a: list[str],
                 sessions_b: list[str], selected: dict | None, attempts: list[dict]) -> dict:
    dec = rec.get("decision") or {}
    ext = rec.get("extraction") or {}
    meta = ext.get("vision_meta") or {}
    intent = rec.get("intent") or {}
    return {
        "fax_id": rec["fax_id"],
        "received": rec.get("received"),
        "sender": rec.get("sender"),
        "pages": rec.get("pages"),
        "extracted_type": dec.get("category"),
        "source_doc_id": dec.get("source_doc_id"),
        "evidence": dec.get("evidence") or [],
        "flags": dec.get("flags") or [],
        "action": action,
        "reasons": dec.get("reasons") or [],
        "duplicate_of": dec.get("duplicate_of"),
        "selected_fields": selected,
        "note_sent": intent.get("note") if selected else None,
        "note_ui_verifiable": False,  # the portal never renders notes; recorded, not verified
        "verification": verification,
        "sessions": {"download_session_a": sessions_a[-1:] or [], "route_sessions_b": sessions_b},
        "pdf_sha256": rec.get("sha256"),
        "extraction": {
            "engine": ext.get("engine"), "error": ext.get("error"),
            "model": meta.get("model"), "cache_hit": meta.get("cache_hit"),
            "input_tokens": meta.get("input_tokens"), "output_tokens": meta.get("output_tokens"),
            "cost_usd": meta.get("cost_usd"), "vision_latency_ms": meta.get("latency_ms"),
            "crosscheck": ext.get("crosscheck"), "text_layer_present": ext.get("text_layer_present"),
        },
        "errors": errors,
        "attempts": [{k: a[k] for k in ("run_id", "stage", "outcome", "detail")} for a in attempts],
        "retries": sum(1 for a in attempts if a["outcome"] not in ("ok", "verified_existing", "success")),
        "finished_at": time.time(),
    }


CSV_COLS = ["fax_id", "received", "sender", "pages", "extracted_type", "source_doc_id", "action", "duplicate_of",
            "assigned_to", "doc_type", "folder", "verification", "retries", "reasons", "flags", "errors"]


def write_results(store, out_dir: Path, run_id: str) -> tuple[Path, Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    recs = store.all()
    results = [r["result"] for r in recs if r.get("result")]
    newly = [r["fax_id"] for r in recs if r.get("result") and not r["reported"]]
    pending = [r["fax_id"] for r in recs if not r.get("result")]
    doc = {"run_id": run_id, "generated_at": time.time(), "count": len(results),
           "newly_reported_this_run": newly, "pending": pending, "results": results}
    jpath = out_dir / "results.json"
    jpath.write_text(json.dumps(doc, indent=2, default=str))

    cpath = out_dir / "results.csv"
    with cpath.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLS)
        w.writeheader()
        for r in results:
            sel = r.get("selected_fields") or {}
            w.writerow({
                **{k: r.get(k) for k in ("fax_id", "received", "sender", "pages", "extracted_type",
                                         "source_doc_id", "action", "duplicate_of", "retries")},
                "assigned_to": sel.get("assigned_to"), "doc_type": sel.get("doc_type"), "folder": sel.get("folder"),
                "verification": (r.get("verification") or {}).get("status"),
                "reasons": " | ".join(r.get("reasons") or []), "flags": " | ".join(r.get("flags") or []),
                "errors": " | ".join(r.get("errors") or []),
            })

    mpath = out_dir / "summary.md"
    lines = ["| Fax | Type | Source ID | Action | Assigned To | Verification | Retries |",
             "|---|---|---|---|---|---|---|"]
    for r in results:
        sel = r.get("selected_fields") or {}
        lines.append(f"| {r['fax_id']} | {r.get('extracted_type') or '-'} | {r.get('source_doc_id') or '-'} | "
                     f"{r['action']}{' of ' + r['duplicate_of'] if r.get('duplicate_of') and r['action'] == 'skipped_duplicate' else ''} | "
                     f"{sel.get('assigned_to') or '-'} | {(r.get('verification') or {}).get('status')} | {r.get('retries')} |")
    mpath.write_text("\n".join(lines) + "\n")

    # Report-once: mark only after the artifacts are durably written.
    for fid in newly:
        store.update(fid, reported=1)
    return jpath, cpath, mpath
