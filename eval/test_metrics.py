"""Unit tests for the eval metrics on synthetic labels/results (no portal, no real faxes)."""
import pytest

from eval import metrics as M

REVIEW = dict(zip(M.ROUTE_KEYS, M.ORACLE_REVIEW))
REF = dict(zip(M.ROUTE_KEYS, M.ORACLE_SOP["referral"]))
LAB = dict(zip(M.ROUTE_KEYS, M.ORACLE_SOP["lab_result"]))


def lab(action, cat, route="auto", dup=None):
    return {"expected_action": action, "expected_category": cat, "expected_route": route,
            "expected_duplicate_of": dup}


def res(fid, action, cat, sel=None, dup=None, reasons=()):
    return {"fax_id": fid, "action": action, "extracted_type": cat, "selected_fields": sel,
            "duplicate_of": dup, "reasons": list(reasons)}


def test_perfect_run_scores_clean():
    labels = {"faxes": {"A": lab("routed", "referral"), "B": lab("needs_review", "lab_result"),
                        "C": lab("skipped_duplicate", "referral", "none", dup="A"),
                        "D": lab("already_routed", "eob", "none")}}
    results = [res("A", "routed", "referral", REF), res("B", "needs_review", "lab_result", REVIEW),
               res("C", "skipped_duplicate", "referral", dup="A"), res("D", "already_routed", "eob")]
    assert M.validate_labels(labels) == []
    s = M.score(results, labels)
    assert s.exact_route_match == 4 and s.classification_correct == 4
    assert not (s.unsafe_auto_routes or s.misroutes or s.extra_submissions or s.over_review)
    assert (s.dup_precision, s.dup_recall) == (1.0, 1.0)
    assert s.over_review_rate == 0.0


def test_unsafe_auto_route_is_flagged():
    labels = {"faxes": {"B": lab("needs_review", "lab_result")}}
    s = M.score([res("B", "routed", "lab_result", LAB)], labels)
    assert s.unsafe_auto_routes == ["B"] and s.exact_route_match == 0
    assert s.confusion["needs_review"]["routed"] == 1


def test_over_review_is_counted_but_not_unsafe():
    labels = {"faxes": {"A": lab("routed", "referral"), "X": lab("routed", "eob")}}
    s = M.score([res("A", "needs_review", "referral", REVIEW), res("X", "routed", "eob",
                                                                   dict(zip(M.ROUTE_KEYS, M.ORACLE_SOP["eob"])))],
                labels)
    assert s.over_review == ["A"] and s.over_review_rate == 0.5
    assert s.unsafe_auto_routes == []


def test_misroute_and_wrong_duplicate_original():
    labels = {"faxes": {"A": lab("routed", "referral"), "C": lab("skipped_duplicate", "referral", "none", dup="A")}}
    s = M.score([res("A", "routed", "referral", LAB), res("C", "skipped_duplicate", "referral", dup="Z")], labels)
    assert s.misroutes == ["A"]
    assert s.dup_tp == 1 and s.dup_wrong_original == ["C"] and s.exact_route_match == 0


def test_submitting_on_duplicate_is_extra_submission_and_dup_fn():
    labels = {"faxes": {"C": lab("skipped_duplicate", "referral", "none", dup="A")}}
    s = M.score([res("C", "needs_review", "referral", REVIEW)], labels)
    assert s.extra_submissions == ["C"] and s.dup_fn == 1 and s.dup_recall == 0.0


def test_missing_result_counts_as_failed():
    s = M.score([], {"faxes": {"A": lab("routed", "referral")}})
    assert s.failed == ["A"] and s.missing_results == ["A"]


def test_mixed_and_unclear_category_mapping():
    assert M.predicted_category(res("M", "needs_review", "lab_result", reasons=["Mixed packet: ..."])) == "mixed"
    assert M.predicted_category(res("U", "needs_review", None)) == "unclear"


@pytest.mark.parametrize("bad, needle", [
    (lab(None, "referral"), "expected_action is blank"),
    (lab("routed", ""), "expected_category is blank"),
    (lab("routed", "referral", None), "expected_route is blank"),
    (lab("routed", "referral", {"assigned_to": "Olivia Chen", "doc_type": "Referral", "folder": "Referrals / New"}),
     "not an exact portal dropdown value"),
    (lab("skipped_duplicate", "referral", "none"), "needs expected_duplicate_of"),
    (lab("routed", "mixed"), "needs a concrete category"),
    (lab("bogus", "referral"), "not in"),
])
def test_validation_refuses_blank_or_invalid(bad, needle):
    problems = M.validate_labels({"faxes": {"A": bad}})
    assert any(needle in p for p in problems), problems


def test_stage_latencies_from_events():
    ev = [{"ts": 10.0, "event": "downloaded", "fax_id": "A", "ms": 200},
          {"ts": 11.0, "event": "extracted", "fax_id": "A", "ms": 50},
          {"ts": 20.0, "event": "intent_written", "fax_id": "A"},
          {"ts": 20.5, "event": "submit_unknown", "fax_id": "A"},
          {"ts": 21.0, "event": "submit_success", "fax_id": "A"},
          {"ts": 21.3, "event": "fax_done", "fax_id": "A"}]
    lat = M.stage_latencies(ev, [{"fax_id": "A", "extraction": {"vision_latency_ms": 4000}}])["A"]
    assert lat["download"] == 200 and lat["extract"] == 50 and lat["vision"] == 4000
    assert lat["route"] == pytest.approx(1000) and lat["verify"] == pytest.approx(300)


def test_cost_table_totals():
    rows, tot = M.cost_table([{"fax_id": "A", "pages": 2, "extraction": {"input_tokens": 10, "output_tokens": 2,
                                                                          "cost_usd": 0.02}},
                              {"fax_id": "B", "pages": 1, "extraction": {}}])
    assert tot["faxes_with_cost"] == 1 and tot["cost_usd"] == 0.02 and tot["cost_per_page"] == 0.01
