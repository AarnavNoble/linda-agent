"""Independent local OCR used as a *safety cross-check* on the vision extraction.

Tesseract never classifies on its own. It escalates to review only on a positive
contradiction found in text it read with good confidence:
  * an affirmative urgent/critical marker the vision pass did not report;
  * a footer transmission ID that differs from the portal fax ID;
  * a vision-reported source document ID that is absent from well-read text.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field

from rapidfuzz import fuzz

FLAG_RE = re.compile(r"\b(CRITICAL|URGENT|STAT|EMERGENT|ASAP)\b", re.I)
NEGATION_RE = re.compile(r"\b(no|not|non|without|none)\b[\w\s-]{0,20}$", re.I)
FOOTER_RE = re.compile(r"Transmission\s+(FX-\d+)", re.I)
MIN_GOOD_CONF = 75.0


def available() -> bool:
    return shutil.which("tesseract") is not None


@dataclass
class PageOCR:
    text: str
    mean_conf: float


@dataclass
class CrossCheck:
    ran: bool
    contradictions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mean_conf: float | None = None


def ocr_pages(images) -> list[PageOCR]:
    import pytesseract

    out = []
    for img in images:
        data = pytesseract.image_to_data(img.convert("L"), output_type=pytesseract.Output.DICT)
        confs = [float(c) for c, w in zip(data["conf"], data["text"]) if w.strip() and float(c) >= 0]
        text = pytesseract.image_to_string(img.convert("L"))
        out.append(PageOCR(text=text, mean_conf=sum(confs) / len(confs) if confs else 0.0))
    return out


def affirmative_flags(text: str) -> list[str]:
    hits = []
    for line in text.splitlines():
        for m in FLAG_RE.finditer(line):
            if NEGATION_RE.search(line[: m.start()]):
                continue  # "No urgent flag provided" is not a flag
            hits.append(line.strip())
    return hits


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def cross_check(pages: list[PageOCR], vision_pages: list[dict], fax_id: str) -> CrossCheck:
    cc = CrossCheck(ran=True, mean_conf=round(sum(p.mean_conf for p in pages) / max(len(pages), 1), 1))
    for i, (ocr, vp) in enumerate(zip(pages, vision_pages), 1):
        good = ocr.mean_conf >= MIN_GOOD_CONF
        # 1. urgent/critical markers the vision pass missed (safety-critical; checked even on weak OCR)
        tess_flags = affirmative_flags(ocr.text)
        if tess_flags and not vp.get("explicit_flags"):
            cc.contradictions.append(f"p{i}: local OCR read an urgency marker vision did not report: {tess_flags[0]!r}")
        # 2. footer transmission ID must be this fax
        for fid in FOOTER_RE.findall(ocr.text):
            if fid.upper() != fax_id.upper() and good:
                cc.contradictions.append(f"p{i}: footer reads transmission {fid}, portal fax is {fax_id}")
        # 3. source document ID should be present in well-read text
        sid = vp.get("source_doc_id")
        if sid:
            score = fuzz.partial_ratio(_norm(sid), _norm(ocr.text))
            if score < 90:
                msg = f"p{i}: source ID {sid!r} not found by local OCR (match {score:.0f})"
                (cc.contradictions if good else cc.warnings).append(msg)
    return cc
