"""Deterministic routing decisions (assignment §4-5). Pure functions, no I/O.

Rule precedence (first match wins):
  1. already routed in the portal            -> already_routed (never touched)
  2. confirmed resend of an earlier fax      -> skipped_duplicate
     uncertain resend identity / tie          -> needs_review
  3. extraction unavailable / wrong document -> needs_review
  4. explicit urgent/critical marker         -> needs_review (flag quoted in note)
  5. mixed categories in one packet          -> needs_review
  6. missing / cut-off / illegible pages     -> needs_review
  7. unclear or low-confidence category      -> needs_review
  8. independent OCR contradicts vision      -> needs_review
  9. otherwise                               -> routed per SOP table
Duplicates are decided before content exceptions on purpose: a resend of an urgent original
must be skipped, not filed a second time into review.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .sop import (ALREADY_ROUTED, CATEGORIES, NEEDS_REVIEW, NEEDS_REVIEW_ACTION, ROUTED, SKIPPED_DUPLICATE,
                  SOP, Route)

ID_RE = re.compile(r"\b([A-Z]{1,5}-\d{2,})\b")
PAGE_MARK_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.I)
POSITIONAL_REF_RE = re.compile(
    r"\b(?:following|next|subsequent)\s+(?:page|pages|table|sheet)\b|\bcontinued\b|\bsee\s+(?:over|reverse)\b"
    r"|\bpage\s+(?P<page>\d+)\b", re.I)
CONTENT_ROLES = ("primary_document", "supporting_attachment")


@dataclass
class Decision:
    fax_id: str
    action: str
    route: Route | None
    category: str | None
    source_doc_id: str | None
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    duplicate_of: str | None = None
    note: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["route"] = self.route._asdict() if self.route else None
        return d


# ---------------------------------------------------------------------------
# Per-packet analysis
# ---------------------------------------------------------------------------
def _norm_name(s: str | None) -> str | None:
    return re.sub(r"[^a-z]", "", s.lower()) if s else None


def _norm_id(s: str | None) -> str | None:
    if not s:
        return None
    m = ID_RE.search(s.upper())
    return m.group(1) if m else re.sub(r"[^A-Z0-9-]", "", s.upper()) or None


@dataclass
class Packet:
    fax_id: str
    received: str
    portal_pages: int
    ok: bool
    error: str | None
    pages: list[dict]
    categories: set[str]
    primary_category: str | None
    source_doc_id: str | None
    identity_ids: set[str]
    patient: str | None
    dob: str | None
    flags: list[str]
    resend_markers: list[str]
    issues: list[str]          # completeness / legibility / wrong-document problems
    low_confidence: list[str]
    crosscheck: list[str]
    evidence: list[str]
    absent_refs: list[str]     # informational: refers to content not in the packet (not incompleteness)


def analyse(fax: dict) -> Packet:
    ext = fax.get("extraction") or {}
    pages = ext.get("pages") or []
    fid = fax["fax_id"]
    ok = bool(pages) and not ext.get("error")
    issues: list[str] = []
    low: list[str] = []

    if ok and len(pages) != fax["pages"]:
        issues.append(f"extraction covers {len(pages)} pages but portal lists {fax['pages']}")

    markers = []
    for p in pages:
        n = p.get("page_number")
        footer_id = (p.get("footer_transmission_id") or "").upper().strip()
        if footer_id and footer_id != fid.upper():
            issues.append(f"p{n}: footer transmission {footer_id} does not match portal fax {fid}")
        m = PAGE_MARK_RE.search(p.get("footer_page_marker") or "")
        if m:
            markers.append((int(m.group(1)), int(m.group(2))))
        for c in p.get("completeness_issues") or []:
            issues.append(f"p{n}: {c}")
        if p.get("page_role") == "unreadable" or p.get("legibility") == "low":
            issues.append(f"p{n}: page is illegible")
        if p.get("page_role") in CONTENT_ROLES and p.get("category_confidence") == "low":
            low.append(f"p{n}: low-confidence category {p.get('category')!r}")

    if markers:
        totals = {t for _, t in markers}
        if len(totals) > 1 or (fax["pages"] and totals != {fax["pages"]}):
            issues.append(f"page markers declare {sorted(totals)} total pages; portal lists {fax['pages']}")
        seen = sorted({i for i, _ in markers})
        expected = list(range(1, max(totals) + 1))
        if seen != expected:
            issues.append(f"page markers {seen} have gaps (expected {expected})")

    # In-packet references that the packet itself must satisfy. Only *positional* references count
    # ("following table", "next page", "page 2", "continued"); references to outside documents
    # ("see full report") and vague ones ("attached details") are not evidence of a missing page.
    present = len(pages)
    for p in pages:
        n = p.get("page_number") or 0
        for ref in p.get("references_to_absent_content") or []:
            m = POSITIONAL_REF_RE.search(ref)
            if not m:
                continue
            if m.group("page"):
                if int(m.group("page")) > present:
                    issues.append(f"p{n}: refers to page {m.group('page')} ({ref!r}) but packet has {present} page(s)")
            elif n >= present:
                issues.append(f"p{n}: refers to content after it ({ref!r}) but it is the last page of the packet")
        for item in p.get("enclosure_list") or []:
            for num in re.findall(r"\bpage\s+(\d+)", item, re.I):
                if int(num) > present:
                    issues.append(f"p{n}: cover lists an enclosure on page {num} ({item!r}) but packet has {present} page(s)")

    content = [p for p in pages if p.get("page_role") in CONTENT_ROLES]
    cats = {p.get("category") for p in content} - {"none", None}
    primaries = [p for p in content if p.get("page_role") == "primary_document"]
    primary_cat = primaries[0].get("category") if primaries else (next(iter(cats)) if len(cats) == 1 else None)

    src = None
    for p in primaries + content:
        if p.get("source_doc_id"):
            src = p["source_doc_id"]
            break

    ids: set[str] = set()
    for p in pages:
        for raw in [p.get("source_doc_id")] + list(p.get("related_doc_ids") or []):
            nid = _norm_id(raw)
            if nid and nid.upper() != fid.upper():
                ids.add(nid)

    patient = next((p.get("patient_name") for p in content if p.get("patient_name")), None)
    dob = next((p.get("dob") for p in content if p.get("dob")), None)
    flags = [f"p{p.get('page_number')}: {f}" for p in pages for f in (p.get("explicit_flags") or [])]
    resend = [r for p in pages for r in (p.get("resend_indicators") or [])]
    evidence = [q for p in content for q in (p.get("evidence_quotes") or [])][:6]
    cc = (ext.get("crosscheck") or {}).get("contradictions") or []
    absent = [f"p{p.get('page_number')}: {r}" for p in pages for r in (p.get("references_to_absent_content") or [])]

    return Packet(fid, fax.get("received") or "", fax["pages"], ok, ext.get("error"), pages, cats, primary_cat,
                  src, ids, patient, dob, flags, resend, issues, low, list(cc), evidence, absent)


# ---------------------------------------------------------------------------
# Duplicate detection across the whole inbox
# ---------------------------------------------------------------------------
def _same_person(a: Packet, b: Packet) -> tuple[bool, str]:
    na, nb = _norm_name(a.patient), _norm_name(b.patient)
    if not na or not nb:
        return False, "patient name missing on one side"
    if na != nb:
        return False, f"patient differs ({a.patient!r} vs {b.patient!r})"
    if a.dob and b.dob and re.sub(r"\D", "", a.dob) != re.sub(r"\D", "", b.dob):
        return False, "date of birth differs"
    if not (a.dob and b.dob):
        return False, "date of birth missing on one side"
    return True, "same patient name and DOB"


def _flag_texts(p: Packet) -> set[str]:
    return {re.sub(r"^p\d+:\s*", "", f).strip().upper() for f in p.flags}


def _new_flags(later: Packet, earlier: Packet) -> list[str]:
    return sorted(_flag_texts(later) - _flag_texts(earlier))


def find_duplicates(packets: list[Packet]) -> dict[str, tuple[str, str | None, str]]:
    """Map fax_id -> (verdict, original_fax_id, reason). verdict in {'duplicate', 'uncertain'}."""
    out: dict[str, tuple[str, str | None, str]] = {}
    order = sorted(packets, key=lambda p: (p.received, p.fax_id))  # earliest received first; ID tie-break
    for i, later in enumerate(order):
        if not later.ok:
            continue
        for earlier in order[:i]:
            if not earlier.ok:
                continue
            shared = later.identity_ids & earlier.identity_ids
            if not shared:
                continue
            same, why = _same_person(later, earlier)
            same_cat = later.primary_category == earlier.primary_category
            ids = ", ".join(sorted(shared))
            if later.received == earlier.received:
                out[later.fax_id] = ("uncertain", earlier.fax_id,
                                     f"shares {ids} with {earlier.fax_id} received at the same time; "
                                     "cannot establish which is the original")
            elif same and same_cat and _new_flags(later, earlier):
                # A resend that adds an urgency marker the original lacked is new information, not a copy.
                out[later.fax_id] = ("uncertain", earlier.fax_id,
                                     f"resend of {earlier.fax_id} ({ids}) but it carries urgency marker(s) the "
                                     f"original does not: {', '.join(_new_flags(later, earlier))}")
            elif same and same_cat:
                out[later.fax_id] = ("duplicate", earlier.fax_id,
                                     f"resend of {earlier.fax_id}: same source ID {ids}, {why}, "
                                     f"same category; {earlier.fax_id} received earlier ({earlier.received})")
            else:
                out[later.fax_id] = ("uncertain", earlier.fax_id,
                                     f"shares {ids} with {earlier.fax_id} but {why if not same else 'category differs'}")
            break
        if later.fax_id not in out and later.resend_markers:
            out[later.fax_id] = ("uncertain", None,
                                 f"document says it is a resend ({later.resend_markers[0]!r}) but no earlier "
                                 "matching fax was found in the inbox")
    return out


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------
def _note(prefix: str, items: list[str]) -> str:
    return (prefix + " " + " | ".join(items))[:990]


def decide_all(faxes: list[dict], failed_originals: set[str] = frozenset()) -> dict[str, Decision]:
    """faxes: [{fax_id, received, pages, ui_status_at_discovery, extraction}] for the whole inbox."""
    packets = {f["fax_id"]: analyse(f) for f in faxes}
    dups = find_duplicates(list(packets.values()))
    out: dict[str, Decision] = {}

    for f in faxes:
        p = packets[f["fax_id"]]
        d = Decision(p.fax_id, "", None, p.primary_category, p.source_doc_id, flags=p.flags, evidence=p.evidence)

        def review(*reasons: str) -> Decision:
            d.action, d.route = NEEDS_REVIEW_ACTION, NEEDS_REVIEW
            d.reasons = list(reasons)
            d.note = _note("NEEDS REVIEW (Linda agent):", d.reasons)
            return d

        # 1. already routed in the portal
        if f.get("ui_status_at_discovery") == "Routed":
            d.action, d.reasons = ALREADY_ROUTED, ["portal shows Routed before this agent acted"]
            out[p.fax_id] = d
            continue

        # 2. duplicates
        if p.fax_id in dups:
            verdict, orig, why = dups[p.fax_id]
            d.duplicate_of = orig
            if verdict == "duplicate" and orig not in failed_originals:
                d.action, d.reasons = SKIPPED_DUPLICATE, [why]
                out[p.fax_id] = d
                continue
            if verdict == "duplicate":
                why += f"; original {orig} could not be routed, so this copy needs a human"
            out[p.fax_id] = review(f"Possible resend: {why}")
            continue

        # 3. extraction unavailable / wrong document
        if not p.ok:
            out[p.fax_id] = review(f"Document content could not be extracted: {p.error}")
            continue
        wrong_doc = [i for i in p.issues if "does not match portal fax" in i]
        if wrong_doc:
            out[p.fax_id] = review(*wrong_doc)
            continue

        # 4. urgent / critical (verbatim flags only)
        if p.flags:
            out[p.fax_id] = review("Explicit urgency flag on document: " + "; ".join(f"'{x}'" for x in p.flags),
                                   "No clinical triage performed by the agent")
            continue

        # 5. mixed packet
        if len(p.categories) > 1:
            out[p.fax_id] = review("Mixed packet: contains " + ", ".join(sorted(p.categories)))
            continue

        # 6. incomplete / illegible
        if p.issues:
            out[p.fax_id] = review("Incomplete or unreadable: " + "; ".join(p.issues))
            continue

        # 7. unclear category
        if not p.categories or p.primary_category not in CATEGORIES:
            out[p.fax_id] = review("Document type could not be established from the scanned content")
            continue
        if p.low_confidence:
            out[p.fax_id] = review("Low-confidence classification: " + "; ".join(p.low_confidence))
            continue

        # 8. independent OCR contradiction
        if p.crosscheck:
            out[p.fax_id] = review("Independent OCR disagrees with vision extraction: " + "; ".join(p.crosscheck))
            continue

        # 9. route per SOP
        d.action, d.route = ROUTED, SOP[p.primary_category]
        d.reasons = [f"classified as {p.primary_category}"] + (
            [f"note: refers to content not in packet ({'; '.join(p.absent_refs)}); page markers show packet complete"]
            if p.absent_refs else [])
        d.note = _note("Routed by Linda agent:", [p.primary_category, f"source {p.source_doc_id or 'n/a'}"])
        out[p.fax_id] = d
    return out
