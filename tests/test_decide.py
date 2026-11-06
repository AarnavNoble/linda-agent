"""Table-driven tests for the deterministic routing policy. Fixtures are hand-written synthetic
extractions (not derived from the sandbox's seeded data)."""
import pytest

from agent.decide import decide_all
from agent.sop import NEEDS_REVIEW, SOP


def page(n=1, total=1, fax="FX-1", role="primary_document", cat="referral", conf="high", sid="RF-1",
         patient="Ann Example", dob="01/02/1980", flags=(), negated=(), resend=(), issues=(), related=(),
         legibility="high", footer=None):
    return {"page_number": n, "footer_transmission_id": fax if footer is None else footer,
            "footer_page_marker": f"Page {n} of {total}", "title": "T", "page_role": role, "category": cat,
            "category_confidence": conf, "source_doc_id": sid, "source_doc_id_label": "ID",
            "related_doc_ids": list(related), "patient_name": patient, "dob": dob,
            "explicit_flags": list(flags), "negated_flag_statements": list(negated),
            "resend_indicators": list(resend), "completeness_issues": list(issues), "enclosure_list": [],
            "legibility": legibility, "evidence_quotes": ["q"]}


def fax(fid, pages, received="2026-11-03 08:00", status="New", contradictions=(), error=None):
    return {"fax_id": fid, "received": received, "pages": len(pages), "ui_status_at_discovery": status,
            "extraction": {"pages": pages, "error": error,
                           "crosscheck": {"ran": True, "contradictions": list(contradictions)}}}


def one(f, **kw):
    return decide_all([f], **kw)[f["fax_id"]]


@pytest.mark.parametrize("cat", list(SOP))
def test_every_sop_row_routes_exactly(cat):
    d = one(fax("FX-1", [page(cat=cat)]))
    assert d.action == "routed"
    assert d.route == SOP[cat]


def test_cover_sheet_plus_document_is_not_mixed():
    d = one(fax("FX-1", [page(1, 2, role="cover_sheet", cat="none", sid=None), page(2, 2, cat="referral")]))
    assert d.action == "routed" and d.route == SOP["referral"]


def test_supporting_attachment_same_category_is_not_mixed():
    d = one(fax("FX-1", [page(1, 2, cat="records_request", sid="MR-1"),
                         page(2, 2, role="supporting_attachment", cat="records_request", sid=None,
                              related=["Related request: MR-1"])]))
    assert d.action == "routed" and d.route == SOP["records_request"]


def test_mixed_packet_goes_to_review():
    d = one(fax("FX-1", [page(1, 3, role="cover_sheet", cat="none", sid="PKT-1"),
                         page(2, 3, cat="lab_result", sid="LAB-1"), page(3, 3, cat="imaging_report", sid="IMG-1")]))
    assert d.action == "needs_review" and d.route == NEEDS_REVIEW
    assert "Mixed packet" in d.note


def test_critical_flag_goes_to_review_and_is_quoted_in_note():
    d = one(fax("FX-1", [page(cat="lab_result", flags=["CRITICAL RESULT - CALLING PROVIDER"])]))
    assert d.action == "needs_review"
    assert "CRITICAL RESULT - CALLING PROVIDER" in d.note


def test_negated_flag_statement_is_not_a_flag():
    d = one(fax("FX-1", [page(cat="lab_result", negated=["No urgent flag provided on this report."])]))
    assert d.action == "routed" and d.route == SOP["lab_result"]


def test_incomplete_scan_goes_to_review():
    d = one(fax("FX-1", [page(cat="unknown", sid="U-1", conf="low", issues=["[SCAN CUT OFF]"])]))
    assert d.action == "needs_review" and "SCAN CUT OFF" in d.note


def test_page_marker_gap_goes_to_review():
    p1 = page(1, 3, cat="referral")
    p3 = page(3, 3, cat="referral")
    f = fax("FX-1", [p1, p3])
    f["pages"] = 2
    d = one(f)
    assert d.action == "needs_review" and "gaps" in d.note


def test_unknown_category_goes_to_review():
    assert one(fax("FX-1", [page(cat="unknown")])).action == "needs_review"


def test_low_confidence_goes_to_review():
    assert one(fax("FX-1", [page(cat="eob", conf="low")])).action == "needs_review"


def test_ocr_contradiction_goes_to_review():
    d = one(fax("FX-1", [page(cat="eob")], contradictions=["p1: urgency marker"]))
    assert d.action == "needs_review" and "Independent OCR" in d.note


def test_extraction_failure_goes_to_review_not_crash():
    d = one(fax("FX-1", [], error="vision API failed"))
    assert d.action == "needs_review" and "could not be extracted" in d.note


def test_footer_for_another_fax_goes_to_review():
    d = one(fax("FX-1", [page(cat="eob", footer="FX-9")]))
    assert d.action == "needs_review" and "does not match portal fax" in d.note


def test_already_routed_is_never_resubmitted():
    d = one(fax("FX-1", [page(cat="eob")], status="Routed"))
    assert d.action == "already_routed" and d.route is None


# ---- duplicates -------------------------------------------------------------
def orig_and_resend(resend_received="2026-11-03 09:00", resend_patient="Ann Example", orig_flags=()):
    o = fax("FX-1", [page(fax="FX-1", sid="RF-1", flags=orig_flags)], received="2026-11-03 08:00")
    r = fax("FX-2", [page(fax="FX-2", sid="RF-1", patient=resend_patient, resend=["This is a retransmission"])],
            received=resend_received)
    return o, r


def test_confirmed_resend_is_skipped_and_original_routed():
    o, r = orig_and_resend()
    ds = decide_all([r, o])  # input order must not matter
    assert ds["FX-1"].action == "routed"
    assert ds["FX-2"].action == "skipped_duplicate" and ds["FX-2"].duplicate_of == "FX-1"


def test_resend_of_urgent_original_is_still_skipped():
    o, r = orig_and_resend(orig_flags=["URGENT"])
    r["extraction"]["pages"][0]["explicit_flags"] = ["URGENT"]
    ds = decide_all([o, r])
    assert ds["FX-1"].action == "needs_review"
    assert ds["FX-2"].action == "skipped_duplicate"


def test_resend_of_prerouted_original_is_skipped():
    o, r = orig_and_resend()
    o["ui_status_at_discovery"] = "Routed"
    ds = decide_all([o, r])
    assert ds["FX-1"].action == "already_routed" and ds["FX-2"].action == "skipped_duplicate"


def test_shared_id_different_patient_is_uncertain_review():
    o, r = orig_and_resend(resend_patient="Someone Else")
    assert decide_all([o, r])["FX-2"].action == "needs_review"


def test_same_timestamp_resend_is_uncertain_review():
    o, r = orig_and_resend(resend_received="2026-11-03 08:00")
    ds = decide_all([o, r])
    assert ds["FX-1"].action == "routed" and ds["FX-2"].action == "needs_review"


def test_resend_claim_without_original_goes_to_review():
    _, r = orig_and_resend()
    assert one(r).action == "needs_review"


def test_resend_whose_original_failed_goes_to_review():
    o, r = orig_and_resend()
    assert decide_all([o, r], failed_originals={"FX-1"})["FX-2"].action == "needs_review"


def test_same_sender_same_time_different_documents_are_not_duplicates():
    a = fax("FX-3", [page(fax="FX-3", cat="imaging_report", sid="IMG-1", patient="P One")], received="2026-11-03 08:12")
    b = fax("FX-4", [page(fax="FX-4", cat="prior_authorization", sid="PA-1", patient="P Two")], received="2026-11-03 08:12")
    ds = decide_all([a, b])
    assert ds["FX-3"].action == ds["FX-4"].action == "routed"


def test_every_review_has_a_reason_note():
    fs = [fax("FX-1", [page(cat="unknown")]), fax("FX-2", [page(fax="FX-2", flags=["STAT"])])]
    for d in decide_all(fs).values():
        assert d.action == "needs_review" and d.note.startswith("NEEDS REVIEW") and len(d.note) > 20


def _with_refs(p, refs=(), enclosures=()):
    p["references_to_absent_content"] = list(refs)
    p["enclosure_list"] = list(enclosures)
    return p


@pytest.mark.parametrize("ref", ["Impression: See full radiology findings.",
                                 "Findings documented in full report.",
                                 "Please review attached patient details."])
def test_reference_to_outside_or_vague_content_is_not_incomplete(ref):
    d = one(fax("FX-1", [_with_refs(page(cat="imaging_report"), [ref])]))
    assert d.action == "routed"
    assert any("other documents not sent" in r for r in d.reasons)


def test_positional_reference_on_last_page_is_incomplete():
    # Regression: a 1-page lab report that says its values are on a following table was auto-routed.
    d = one(fax("FX-1", [_with_refs(page(cat="lab_result"), ["Results: See values on following table"])]))
    assert d.action == "needs_review" and "last page of the packet" in d.note


def test_satisfied_positional_references_route_normally():
    p1 = _with_refs(page(1, 2, cat="records_request", sid="MR-1"), ["Patient authorization: Included on page 2"])
    p2 = page(2, 2, role="supporting_attachment", cat="records_request", sid=None)
    cover = _with_refs(page(1, 2, fax="FX-2", role="cover_sheet", cat="none", sid=None), ["paperwork follows on the next page"])
    assert one(fax("FX-1", [p1, p2])).action == "routed"
    assert one(fax("FX-2", [cover, page(2, 2, fax="FX-2", cat="referral")])).action == "routed"


def test_page_reference_beyond_packet_is_incomplete():
    d = one(fax("FX-1", [_with_refs(page(cat="records_request"), ["Authorization included on page 2"])]))
    assert d.action == "needs_review" and "refers to page 2" in d.note


def test_cover_sheet_enclosure_missing_from_packet_is_incomplete():
    cover = _with_refs(page(1, 2, role="cover_sheet", cat="none", sid=None),
                       enclosures=["Page 2: Laboratory result", "Page 3: Imaging interpretation"])
    d = one(fax("FX-1", [cover, page(2, 2, cat="lab_result")]))
    assert d.action == "needs_review" and "enclosure on page 3" in d.note


def test_resend_that_adds_an_urgency_marker_is_not_silently_skipped():
    o, r = orig_and_resend()
    r["extraction"]["pages"][0]["explicit_flags"] = ["URGENT - PLEASE EXPEDITE"]
    ds = decide_all([o, r])
    assert ds["FX-1"].action == "routed"
    assert ds["FX-2"].action == "needs_review" and "urgency marker" in ds["FX-2"].note
