"""Live perception comparison: repeatability of the production model and cheaper alternatives.

For each (model, repeat) it re-extracts every downloaded PDF with live vision (no cache), runs the
same deterministic decider, and diffs the *decision* (action, route, category, source ID, flags,
duplicate_of) against the reference extraction committed in cache/. Raw extractions are saved under
eval/model_runs/ so the comparison itself can be re-scored without API calls (--rescore).

    uv run python -m eval.model_compare --downloads downloads \
        --runs claude-opus-5-5:2 claude-sonnet-5-5:1 claude-haiku-5-5:1
    uv run python -m eval.model_compare --rescore        # recompute report from eval/model_runs/

Needs ANTHROPIC_API_KEY (except --rescore). Costs are reported from the API's own token usage.
This scores agreement with the reference decisions, not accuracy vs. human labels (see run_eval).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from pathlib import Path

from agent.config import ROOT
from agent.decide import decide_all
from agent.extract.vision import PROMPT_VERSION, VisionUnavailable, cost_usd, extract_packet

OUT_DIR = ROOT / "eval" / "model_runs"
REPORT = ROOT / "eval" / "model_compare.md"
FIELDS = ("action", "route", "category", "source_doc_id", "flags", "duplicate_of")


def _inbox(downloads: Path) -> list[dict]:
    """fax_id/sha/pages for each downloaded PDF, joined with the reference cache entry (same sha)."""
    ref = {}
    for f in (ROOT / "cache").glob(f"*.{PROMPT_VERSION}.json"):
        j = json.loads(f.read_text())
        if ".claude-opus-5-5." in f.name:
            ref[j["sha256"]] = j
    out = []
    for pdf in sorted(downloads.glob("FX-*.pdf")):
        fid = re.match(r"(FX-\d+)-", pdf.name).group(1)
        sha_prefix = pdf.stem.split("-")[-1]
        entry = next((v for k, v in ref.items() if k.startswith(sha_prefix)), None)
        if entry:
            out.append({"fax_id": fid, "pdf": pdf, "sha256": entry["sha256"], "pages": len(entry["pages"]),
                        "reference_pages": entry["pages"]})
    return out


def _faxes(inbox: list[dict], pages_by_fax: dict[str, list | None], received: dict[str, str],
           prerouted: set[str]) -> list[dict]:
    return [{"fax_id": x["fax_id"], "received": received.get(x["fax_id"], ""), "pages": x["pages"],
             "ui_status_at_discovery": "Routed" if x["fax_id"] in prerouted else "New",
             "extraction": {"pages": pages_by_fax.get(x["fax_id"]) or [],
                            "error": None if pages_by_fax.get(x["fax_id"]) else "extraction unavailable",
                            "crosscheck": {"contradictions": []}}} for x in inbox]


def _project(d) -> dict:
    x = d.to_dict()
    return {k: x.get(k) for k in FIELDS}


def _sample_meta() -> tuple[dict[str, str], set[str]]:
    sample = json.loads((ROOT / "results" / "sample" / "results.json").read_text())["results"]
    return ({r["fax_id"]: r["received"] for r in sample},
            {r["fax_id"] for r in sample if r["action"] == "already_routed"})


def run_live(inbox: list[dict], spec: list[str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for item in spec:
        model, _, n = item.partition(":")
        for rep in range(1, int(n or 1) + 1):
            out = OUT_DIR / f"{model}.r{rep}.json"
            if out.exists():
                print(f"skip {out.name} (exists)")
                continue
            rows = []
            for x in inbox:
                t0 = time.monotonic()
                try:
                    r = extract_packet(x["pdf"], model, x["pages"])
                    rows.append({"fax_id": x["fax_id"], "pages": [p.model_dump() for p in r.extraction.pages],
                                 "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                                 "cost_usd": cost_usd(model, r.input_tokens, r.output_tokens),
                                 "latency_ms": r.latency_ms, "error": None})
                except VisionUnavailable as e:
                    rows.append({"fax_id": x["fax_id"], "pages": None, "error": str(e),
                                 "latency_ms": int((time.monotonic() - t0) * 1000)})
                print(f"{model} r{rep} {x['fax_id']} {'ERR ' + rows[-1]['error'] if rows[-1]['error'] else 'ok'}")
            out.write_text(json.dumps({"model": model, "repeat": rep, "prompt_version": PROMPT_VERSION,
                                       "rows": rows}, indent=2))


def rescore(inbox: list[dict]) -> str:
    received, prerouted = _sample_meta()
    ref_dec = decide_all(_faxes(inbox, {x["fax_id"]: x["reference_pages"] for x in inbox}, received, prerouted))
    ref = {f: _project(d) for f, d in ref_dec.items()}
    lines = [f"# Live perception comparison (prompt {PROMPT_VERSION})", "",
             "Each row re-extracts all 12 downloaded faxes live and runs the same deterministic decider. "
             "**Decision agreement** compares the routing outcome (action + route + duplicate_of) with the "
             "reference Opus extraction committed in `cache/`; **field diffs** also count category, source ID "
             "and flags. This measures agreement and repeatability, not accuracy vs. human labels.", "",
             "| Run | Decision agreement | Unsafe (would auto-route a non-auto fax) | Extra reviews | Field diffs | "
             "Extraction errors | $ total | $/fax | Vision p50 / max |",
             "|---|---|---|---|---|---|---|---|---|"]
    detail = []
    for f in sorted(OUT_DIR.glob("*.json")):
        run = json.loads(f.read_text())
        pages = {r["fax_id"]: r["pages"] for r in run["rows"]}
        dec = {k: _project(v) for k, v in decide_all(_faxes(inbox, pages, received, prerouted)).items()}
        agree = sum(1 for fid in ref if all(dec[fid][k] == ref[fid][k] for k in ("action", "route", "duplicate_of")))
        diffs = [f"{fid}.{k}: {ref[fid][k]!r} -> {dec[fid][k]!r}" for fid in sorted(ref) for k in FIELDS
                 if dec[fid][k] != ref[fid][k]]
        errs = sum(1 for r in run["rows"] if r.get("error"))
        unsafe = sum(1 for fid in ref if dec[fid]["action"] == "routed" and ref[fid]["action"] != "routed")
        extra_review = sum(1 for fid in ref if dec[fid]["action"] == "needs_review" and ref[fid]["action"] == "routed")
        cost = sum(r.get("cost_usd") or 0 for r in run["rows"])
        lat = [r["latency_ms"] for r in run["rows"] if r.get("latency_ms") is not None]
        name = f"{run['model']} #{run['repeat']}"
        lines.append(f"| {name} | {agree}/{len(ref)} | {unsafe} | {extra_review} | {len(diffs)} | {errs} | ${cost:.3f} | ${cost / max(len(ref), 1):.4f} "
                     f"| {statistics.median(lat) / 1000:.1f}s / {max(lat) / 1000:.1f}s |")
        if diffs:
            detail += [f"### {name}", ""] + [f"- `{d}`" for d in diffs] + [""]
    lines += ["", "## Differences", ""] + (detail or ["None."])
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--downloads", default=str(ROOT / "downloads"))
    ap.add_argument("--runs", nargs="*", default=["claude-opus-5-5:2", "claude-sonnet-5-5:1", "claude-haiku-5-5:1"])
    ap.add_argument("--rescore", action="store_true", help="only recompute the report from eval/model_runs/")
    a = ap.parse_args(argv)
    inbox = _inbox(Path(a.downloads))
    if len(inbox) != 12:
        print(f"warning: {len(inbox)} downloaded PDFs matched the reference cache (expected 12)")
    if not a.rescore:
        run_live(inbox, a.runs)
    REPORT.write_text(rescore(inbox))
    print(REPORT.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
