"""Extraction orchestration with a content-addressed cache.

Cache key = (sha256 of the *downloaded* PDF, model, prompt version). The cache is committed
to the repo so `LINDA_OCR_MODE=replay` reproduces decisions without an API key; the PDF is
still downloaded through the portal every run and its hash must match a cache entry.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import tesseract
from .render import has_text_layer, render_pages
from .vision import PROMPT_VERSION, VisionUnavailable, cost_usd, extract_packet


def _cache_path(cache_dir: Path, sha: str, model: str) -> Path:
    return cache_dir / f"{sha[:16]}.{model}.{PROMPT_VERSION}.json"


def extract(pdf_path: Path, sha: str, fax_id: str, expected_pages: int, *, mode: str, model: str,
            cache_dir: Path) -> dict:
    out: dict = {"sha256": sha, "prompt_version": PROMPT_VERSION, "error": None}
    out["text_layer_present"] = has_text_layer(pdf_path)
    cpath = _cache_path(cache_dir, sha, model)
    t0 = time.monotonic()

    if cpath.exists():
        cached = json.loads(cpath.read_text())
        out.update(engine="vision", pages=cached["pages"], packet_summary=cached["packet_summary"],
                   vision_meta={**cached["meta"], "cache_hit": True})
    elif mode == "replay":
        out.update(engine="unavailable", pages=[], packet_summary="",
                   error=f"replay mode: no cached extraction for sha {sha[:12]} (model={model}, {PROMPT_VERSION})")
    else:
        try:
            r = extract_packet(pdf_path, model, expected_pages)
        except VisionUnavailable as e:
            out.update(engine="unavailable", pages=[], packet_summary="", error=str(e))
        else:
            pages = [p.model_dump() for p in r.extraction.pages]
            meta = {"model": r.model, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                    "cost_usd": cost_usd(r.model, r.input_tokens, r.output_tokens),
                    "latency_ms": r.latency_ms, "request_id": r.request_id}
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cpath.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fax_id": fax_id, "sha256": sha, "pages": pages,
                                       "packet_summary": r.extraction.packet_summary, "meta": meta}, indent=2))
            tmp.replace(cpath)
            out.update(engine="vision", pages=pages, packet_summary=r.extraction.packet_summary,
                       vision_meta={**meta, "cache_hit": False})

    # Independent local OCR cross-check (if installed).
    if out["pages"] and tesseract.available():
        ocr = tesseract.ocr_pages(render_pages(pdf_path, scale=3.0))
        cc = tesseract.cross_check(ocr, out["pages"], fax_id)
        out["crosscheck"] = {"ran": True, "contradictions": cc.contradictions, "warnings": cc.warnings,
                             "mean_conf": cc.mean_conf}
    else:
        out["crosscheck"] = {"ran": False, "contradictions": [], "warnings": [], "mean_conf": None}
    out["latency_ms"] = int((time.monotonic() - t0) * 1000)
    return out
