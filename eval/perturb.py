"""Robustness probe: damage downloaded PDFs and check the agent fails CLOSED (needs_review, never routed).

    uv run python -m eval.perturb [--results results/results.json] [--mode replay|vision] [-o eval/perturbation.md]

For every fax the agent auto-routed in the given run, it builds damaged copies of the PDF that the agent
downloaded through the portal (<work_dir>/downloads/<fax>-<sha12>.pdf):

  drop_page_k    remove page k (multi-page faxes only)
  blank_page_k   replace page k with a white page of the same size

and then runs each copy through two paths, re-deciding over the whole inbox so duplicate rules still apply:

  pipeline  the agent's real `extract()` + `decide_all()`. In replay mode the damaged PDF has a new sha256,
            so there is no cache entry and extraction is `unavailable`: this proves the fail-closed path
            (no extraction -> needs_review) but NOT that vision notices the damage.
  simulated the cached extraction of the original, edited the way a correct vision pass would report the
            damage (page entry removed; blank page reported as unreadable, or, adversarially, as a legible
            empty cover sheet). This exercises the decider's completeness rules without an API key.

What needs a live key: whether Claude *actually* reports a blank/dropped page that way. Run
    LINDA_OCR_MODE=vision ANTHROPIC_API_KEY=... uv run python -m eval.perturb --mode vision
which sends each damaged PDF to the model (about $0.03 per variant) and reports the real decision.
Live results are cached under eval/perturb_cache/ (never mixed into the agent's cache/).

Exit status is 1 if any damaged copy would be auto-routed.
"""
from __future__ import annotations

import argparse
import copy
import os
import hashlib
import json
import sys
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from agent.decide import decide_all
from agent.extract.pipeline import extract
from agent.extract.vision import PROMPT_VERSION

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = Path(__file__).resolve().parent


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def drop_page(src: Path, dst: Path, k: int) -> None:
    r, w = PdfReader(str(src)), PdfWriter()
    for i, page in enumerate(r.pages, 1):
        if i != k:
            w.add_page(page)
    with dst.open("wb") as fh:
        w.write(fh)


def blank_page(src: Path, dst: Path, k: int) -> None:
    r, w = PdfReader(str(src)), PdfWriter()
    for i, page in enumerate(r.pages, 1):
        if i == k:
            w.add_blank_page(width=float(page.mediabox.width), height=float(page.mediabox.height))
        else:
            w.add_page(page)
    with dst.open("wb") as fh:
        w.write(fh)


def _empty_page(n: int, role: str, legibility: str) -> dict:
    return {"page_number": n, "footer_transmission_id": None, "footer_page_marker": None, "title": None,
            "page_role": role, "category": "none" if role == "cover_sheet" else "unknown",
            "category_confidence": "high" if role == "cover_sheet" else "low", "source_doc_id": None,
            "source_doc_id_label": None, "related_doc_ids": [], "patient_name": None, "dob": None,
            "explicit_flags": [], "negated_flag_statements": [], "resend_indicators": [], "completeness_issues": [],
            "references_to_absent_content": [], "enclosure_list": [], "legibility": legibility,
            "evidence_quotes": []}


def simulate(pages: list[dict], kind: str, k: int) -> list[dict]:
    pages = copy.deepcopy(pages)
    if kind == "drop":
        pages = [p for p in pages if p["page_number"] != k]
        for i, p in enumerate(pages, 1):
            p["page_number"] = i
        return pages
    if kind == "blank_unreadable":
        pages[k - 1] = _empty_page(k, "unreadable", "low")
    elif kind == "blank_as_cover":  # adversarial: model calls the white page a clean cover sheet
        pages[k - 1] = _empty_page(k, "cover_sheet", "high")
    return pages


def inbox_records(results: list[dict], cache_dir: Path) -> dict[str, dict]:
    """Rebuild decide_all() inputs for the whole inbox from results.json + the committed cache."""
    recs = {}
    for r in results:
        sha = r.get("pdf_sha256") or ""
        model = (r.get("extraction") or {}).get("model") or "claude-opus-5-5"
        cpath = cache_dir / f"{sha[:16]}.{model}.{PROMPT_VERSION}.json"
        pages = json.loads(cpath.read_text())["pages"] if cpath.exists() else []
        recs[r["fax_id"]] = {
            "fax_id": r["fax_id"], "received": r.get("received"), "pages": r.get("pages"),
            "ui_status_at_discovery": "Routed" if r.get("action") == "already_routed" else "New",
            "extraction": {"pages": pages, "error": None if pages else "no cached extraction",
                           "crosscheck": (r.get("extraction") or {}).get("crosscheck") or {"contradictions": []}},
        }
    return recs


def decide_with(inbox: dict[str, dict], fid: str, extraction: dict):
    faxes = [dict(v) for v in inbox.values()]
    for f in faxes:
        if f["fax_id"] == fid:
            f["extraction"] = extraction
    return decide_all(faxes)[fid]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="eval.perturb", description=__doc__.splitlines()[0])
    ap.add_argument("--results", type=Path, default=ROOT / "results" / "sample" / "results.json")
    ap.add_argument("--downloads", type=Path, default=None, help="default: ./downloads (written by make demo / make run)")
    ap.add_argument("--cache-dir", type=Path, default=ROOT / "cache")
    ap.add_argument("--mode", choices=("replay", "vision"), default="replay")
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--work", type=Path, default=EVAL_DIR / "perturb_work", help="where damaged PDFs are written")
    ap.add_argument("-o", "--out", type=Path, default=EVAL_DIR / "perturbation.md")
    args = ap.parse_args(argv)

    results = json.loads(args.results.read_text())["results"]
    # PDFs are content-addressed (<fax>-<sha12>.pdf), so any run's downloads/ dir with matching hashes works.
    cands = [args.downloads] if args.downloads else [ROOT / "downloads",
                                                     args.results.resolve().parent.parent / "downloads"]
    downloads = next((d for d in cands if d.is_dir() and any(d.glob("*.pdf"))), cands[0])
    if not any(downloads.glob("*.pdf")):
        print(f"error: no downloaded PDFs in {downloads}. Run `make demo` first (it downloads every fax through "
              f"the portal UI), or pass --downloads.", file=sys.stderr)
        return 2
    inbox = inbox_records(results, args.cache_dir)
    args.work.mkdir(parents=True, exist_ok=True)
    pert_cache = EVAL_DIR / "perturb_cache"

    rows, unsafe = [], []
    for r in results:
        if r["action"] != "routed":
            continue
        fid, sha = r["fax_id"], r.get("pdf_sha256") or ""
        src = downloads / f"{fid}-{sha[:12]}.pdf"
        if not src.exists():
            print(f"skip {fid}: downloaded PDF not found at {src}", file=sys.stderr)
            continue
        n = len(PdfReader(str(src)).pages)
        orig_pages = inbox[fid]["extraction"]["pages"]
        variants = [("blank", k) for k in range(1, n + 1)] + ([("drop", k) for k in range(1, n + 1)] if n > 1 else [])
        for kind, k in variants:
            dst = args.work / f"{fid}-{kind}{k}.pdf"
            (drop_page if kind == "drop" else blank_page)(src, dst, k)
            dsha = sha256(dst)
            # 1. real pipeline (replay: cache miss -> unavailable; vision: real model call)
            ext = extract(dst, dsha, fid, r["pages"], mode=args.mode, model=args.model,
                          cache_dir=pert_cache if args.mode == "vision" else args.cache_dir)
            d = decide_with(inbox, fid, ext)
            rows.append((fid, f"{kind}_page_{k}", f"pipeline/{args.mode}", ext["engine"], d.action, d.reasons[:1]))
            # 2. simulated extraction of the damage
            sims = ["drop"] if kind == "drop" else ["blank_unreadable", "blank_as_cover"]
            for s in sims:
                sim_ext = {"pages": simulate(orig_pages, s, k), "error": None, "crosscheck": {"contradictions": []}}
                d2 = decide_with(inbox, fid, sim_ext)
                rows.append((fid, f"{kind}_page_{k}", f"simulated/{s}", "cached+edited", d2.action, d2.reasons[:1]))
    for row in rows:
        if row[4] == "routed":
            unsafe.append(row)

    L = ["# Perturbation probe (fail-closed check)", "",
         f"Source run: `{os.path.relpath(args.results, ROOT)}`; extraction mode: `{args.mode}`. Only faxes the agent auto-routed are "
         "perturbed (a damaged copy of an already-reviewed fax is trivially safe).", "",
         f"**{len(rows)} damaged variants, {len(unsafe)} auto-routed** (target 0).", "",
         "| Fax | Damage | Path | Engine | Decision | First reason |", "|---|---|---|---|---|---|"]
    for fid, dmg, path, eng, act, why in rows:
        L.append(f"| {fid} | {dmg} | {path} | {eng} | **{act}** | {(why[0] if why else '')[:140]} |")
    L += ["", "How to read this:", "",
          "- `pipeline/replay`: the damaged PDF hashes differently, so replay has no extraction and the agent "
          "routes to Manual Review with the reason. This is the real fail-closed path (no evidence, no auto-route).",
          "- `simulated/*`: the original cached extraction edited as a correct vision pass would report the "
          "damage. `blank_as_cover` is adversarial (model calls the white page a clean cover sheet); the "
          "decider still catches it through page-marker gaps or the absence of any content page.",
          "- Not shown without an API key: whether the live model reports the damage at all. Run "
          "`ANTHROPIC_API_KEY=... uv run python -m eval.perturb --mode vision` to measure that (~$0.03/variant).", ""]
    args.out.write_text("\n".join(L))
    print("\n".join(L))
    return 1 if unsafe else 0


if __name__ == "__main__":
    sys.exit(main())
