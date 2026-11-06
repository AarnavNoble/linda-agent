"""End-to-end replay tests against a real, private Linda Mini portal.

These are the brief's "repeatable demonstration": normal routing, an exception, two-session
behaviour, and retry / uncertain-submit handling. Each scenario starts its own portal on a
fresh DB (fault state is per-DB), runs the real agent CLI in replay mode (committed vision
cache, no API key), then checks the outcome two independent ways:

  * the agent's own artifacts (results.json, events.jsonl);
  * the portal itself, read back through the browser UI only (fax detail + Routed Documents).

No __admin endpoint, portal DB, or portal data directory is ever touched.
"""
from __future__ import annotations

import collections

import pytest

from tests.conftest import (EXPECTED_ACTIONS, EXPECTED_DUPLICATE_OF, FX211_PREEXISTING, REVIEW_ROUTE,
                            UIObserver, _start_portal, _stop_portal, run_agent)

pytestmark = pytest.mark.e2e

ALL_IDS = sorted(EXPECTED_ACTIONS)
ROUTING_IDS = [f for f, a in EXPECTED_ACTIONS.items() if a in ("routed", "needs_review")]

# Exact SOP strings from the sandbox README (written out independently of agent/sop.py).
EXPECTED_ROUTES = {
    "FX-201": {"assigned_to": "Olivia Chen - Referrals", "doc_type": "Referral", "folder": "Referrals / New"},
    "FX-203": {"assigned_to": "Noah Patel - Clinical Review", "doc_type": "Imaging Report",
               "folder": "Clinical / Imaging"},
    "FX-204": {"assigned_to": "Ethan Brooks - Authorizations", "doc_type": "Prior Authorization",
               "folder": "Authorizations / Pending"},
    "FX-205": {"assigned_to": "Ava Wilson - Nursing / Medication", "doc_type": "Refill Request",
               "folder": "Nursing / Medication"},
    "FX-206": {"assigned_to": "Mia Gomez - Medical Records", "doc_type": "Records Request",
               "folder": "Records / Requests"},
    "FX-207": {"assigned_to": "Liam Rivera - Billing", "doc_type": "EOB", "folder": "Billing / EOBs"},
    "FX-208": {"assigned_to": "Noah Patel - Clinical Review", "doc_type": "Lab Result", "folder": "Clinical / Labs"},
    "FX-202": REVIEW_ROUTE, "FX-209": REVIEW_ROUTE, "FX-212": REVIEW_ROUTE,
}
DETERMINISTIC_FIELDS = ("fax_id", "action", "extracted_type", "source_doc_id", "selected_fields", "duplicate_of")


class Scenario:
    """One agent run against one fresh portal, with results + events + a UI snapshot frozen at the end."""

    def __init__(self, tmp_path_factory, fault_mode: str):
        self.fault_mode = fault_mode
        self.tmp = tmp_path_factory.mktemp(f"scenario-{fault_mode}")
        self.portal = _start_portal(self.tmp / "portal", fault_mode)
        self.run = run_agent(self.portal.base_url, self.tmp / "work")
        has = (self.tmp / "work" / "results" / "results.json").exists()
        self.doc = self.run.results_doc if has else {}  # frozen: later reruns overwrite the file
        self.results = {r["fax_id"]: r for r in self.doc.get("results", [])}
        self.events = self.run.events()
        obs = UIObserver(self.portal.base_url)
        try:
            self.ui = obs.snapshot(ALL_IDS)
        finally:
            obs.close()

    def close(self):
        _stop_portal(self.portal)

    def events_for(self, fax_id: str, prefix: str = "") -> list[dict]:
        return [e for e in self.events if e.get("fax_id") == fax_id and e["event"].startswith(prefix)]


@pytest.fixture(scope="module")
def scenario(tmp_path_factory):
    made: dict[str, Scenario] = {}

    def get(fault_mode: str = "none") -> Scenario:
        if fault_mode not in made:
            made[fault_mode] = Scenario(tmp_path_factory, fault_mode)
        return made[fault_mode]

    yield get
    for s in made.values():
        s.close()


@pytest.fixture(scope="module")
def clean(scenario) -> Scenario:
    return scenario("none")


# ---- shared assertions ------------------------------------------------------

def assert_outcomes(sc: Scenario) -> None:
    assert sc.run.returncode == 0, sc.run.stderr[-3000:]
    assert sorted(sc.results) == ALL_IDS, f"results cover {sorted(sc.results)}"
    got = {f: r["action"] for f, r in sc.results.items()}
    assert got == EXPECTED_ACTIONS
    for fid in ROUTING_IDS:
        r = sc.results[fid]
        assert r["verification"]["status"] == "ui_verified", (fid, r["verification"], r["errors"])
        assert r["selected_fields"] == EXPECTED_ROUTES[fid], fid
    assert sc.results["FX-210"]["duplicate_of"] == EXPECTED_DUPLICATE_OF["FX-210"]
    assert sc.results["FX-210"]["verification"]["status"] == "ui_status_new"
    assert sc.results["FX-210"]["selected_fields"] is None
    assert sc.results["FX-211"]["selected_fields"] is None


def assert_portal_ui(sc: Scenario) -> None:
    """What the portal shows, read through the UI, matches the expected end state exactly."""
    ui = sc.ui
    for fid in ROUTING_IDS:
        assert ui[fid]["status"] == "Routed", (fid, ui[fid])
        assert ui[fid]["panel"] == EXPECTED_ROUTES[fid], fid
        row = ui[fid]["queue_row"]
        assert row is not None, f"{fid} missing from Routed Documents"
        assert {k: row[k] for k in ("assigned_to", "doc_type", "folder")} == EXPECTED_ROUTES[fid], fid
    # The confirmed resend is never submitted.
    assert ui["FX-210"]["status"] == "New"
    assert ui["FX-210"]["routed"] is False
    assert ui["FX-210"]["queue_row"] is None
    # The pre-routed fax keeps the clinic's values.
    assert ui["FX-211"]["status"] == "Routed"
    assert ui["FX-211"]["panel"] == FX211_PREEXISTING
    # Exactly one Routed Documents row per routed fax, and nothing else.
    ids = [r[0] for r in ui["_queue_rows"]]
    assert collections.Counter(ids) == collections.Counter(ROUTING_IDS + ["FX-211"])


def submit_events(sc: Scenario, fax_id: str) -> list[dict]:
    return sc.events_for(fax_id, "submit_")


# ---- 1. normal routing ------------------------------------------------------

def test_normal_routing_results(clean):
    assert_outcomes(clean)
    assert clean.doc["pending"] == []


def test_normal_routing_ui_state(clean):
    assert_portal_ui(clean)


def test_normal_routing_submits_each_fax_exactly_once(clean):
    for fid in ALL_IDS:
        subs = submit_events(clean, fid)
        if fid in ROUTING_IDS:
            assert [e["event"] for e in subs] == ["submit_success"], fid
        else:
            assert subs == [], f"{fid} must never be submitted"


def test_already_routed_fax_reports_observed_preexisting_values(clean):
    r = clean.results["FX-211"]
    assert r["verification"]["status"] == "preexisting_route_observed"
    assert r["verification"]["portal_values"] == FX211_PREEXISTING


# ---- 2. exceptions ----------------------------------------------------------

def test_exception_critical_result_goes_to_manual_review_with_flag_in_note(clean):
    r = clean.results["FX-202"]
    assert r["action"] == "needs_review"
    assert r["selected_fields"] == REVIEW_ROUTE
    assert clean.ui["FX-202"]["panel"] == REVIEW_ROUTE
    assert "CRITICAL RESULT" in (r["note_sent"] or ""), r["note_sent"]
    assert any("CRITICAL RESULT" in f for f in r["flags"]), r["flags"]
    assert r["note_ui_verifiable"] is False  # the portal never renders notes; honest about it


def test_exception_mixed_packet_note(clean):
    r = clean.results["FX-212"]
    assert r["action"] == "needs_review"
    assert "mixed" in (r["note_sent"] or "").lower(), r["note_sent"]


def test_exception_incomplete_packet_has_reason_note(clean):
    r = clean.results["FX-209"]
    assert r["action"] == "needs_review"
    assert r["note_sent"] and r["reasons"]


def test_every_review_has_a_reason(clean):
    for fid, r in clean.results.items():
        if r["action"] == "needs_review":
            assert r["reasons"], fid
            assert r["note_sent"].startswith("NEEDS REVIEW"), fid


# ---- 3. two-session behaviour ----------------------------------------------

def test_two_session_routing_sessions_differ_from_download_session(clean):
    for fid in ROUTING_IDS:
        s = clean.results[fid]["sessions"]
        a, b = set(s["download_session_a"]), set(s["route_sessions_b"])
        assert a and b, (fid, s)
        assert not (a & b), f"{fid} routed in its download session: {s}"
    # And each B session was opened after that fax's download finished.
    for fid in ROUTING_IDS:
        dl = [e["ts"] for e in clean.events_for(fid) if e["event"] == "downloaded"]
        b = [e["ts"] for e in clean.events_for(fid) if e["event"] == "session_started" and e.get("label") == "B"]
        assert dl and b and max(dl) <= min(b), (fid, dl, b)


def test_two_session_rule_negative_same_context_is_rejected(portal, ui_observer, tmp_path):
    """Deliberately violate the rule: download and route in ONE context. The portal must refuse,
    the fax must stay New; a fresh second context then succeeds (positive control)."""
    from agent.acquire import download_fax
    from agent.route import click_submit, fill_and_review, read_status
    from agent.sop import Route

    fid = "FX-203"
    route = Route(**EXPECTED_ROUTES[fid])
    obs = ui_observer(portal)

    a = obs.session("A")
    try:
        a.goto_fax(fid)
        pages = int(a.transmission_info()["Pages"])
        download_fax(a, fid, pages, tmp_path, max_attempts=3, wait_ms=10_000)
        a.goto_fax(fid)
        fill_and_review(a, fid, route, "same-session negative test")
        out = click_submit(a, fid, 20_000)
        assert out.http_status == 409, out
        assert out.kind == "two_session", out
        assert a.page.title().startswith("Separate session required"), a.page.title()
        assert any("separate login session" in n for n in a.flash_notices()), a.flash_notices()
        status, _ = read_status(a, fid)
        assert status == "New"
        assert a.routed_documents_row(fid) is None
    finally:
        a.close()

    b = obs.session("B")  # new context, logged in after A's download
    try:
        b.goto_fax(fid)
        fill_and_review(b, fid, route, "separate-session positive control")
        out = click_submit(b, fid, 20_000)
        assert out.kind == "success", out
        status, values = read_status(b, fid)
        assert status == "Routed" and values == EXPECTED_ROUTES[fid]
    finally:
        b.close()


def test_two_session_rule_preview_alone_does_not_count(portal, ui_observer):
    """A session that never downloaded cannot route, even from a separate context."""
    from agent.route import click_submit, fill_and_review, read_status
    from agent.sop import Route

    fid = "FX-204"
    obs = ui_observer(portal)
    b = obs.session("B")
    try:
        b.goto_fax(fid)
        fill_and_review(b, fid, Route(**EXPECTED_ROUTES[fid]), "no prior download")
        out = click_submit(b, fid, 20_000)
        assert out.http_status == 409 and out.kind == "two_session", out
        assert read_status(b, fid)[0] == "New"
    finally:
        b.close()


# ---- 4. retries / uncertain submit -----------------------------------------

def test_uncertain_submit_is_verified_not_resubmitted(scenario):
    sc = scenario("route_commit_then_503_once")
    assert_outcomes(sc)
    assert_portal_ui(sc)
    unknown = [e for e in sc.events if e["event"] == "submit_unknown"]
    assert len(unknown) == 1, "the injected 503-after-commit should surface as exactly one unknown submit"
    fid = unknown[0]["fax_id"]
    assert "503" in unknown[0]["detail"]
    assert [e["event"] for e in submit_events(sc, fid)] == ["submit_unknown"], "must not resubmit after commit"
    assert sc.results[fid]["verification"]["status"] == "ui_verified"
    assert sc.results[fid]["verification"].get("submit_outcome") == "unknown"
    assert [r[0] for r in sc.ui["_queue_rows"]].count(fid) == 1


def test_download_503_is_retried(scenario):
    sc = scenario("download_503_once")
    assert_outcomes(sc)
    assert_portal_ui(sc)
    err = [e for e in sc.events if e["event"] == "download_error_page"]
    assert len(err) == 1, [e["event"] for e in sc.events if e["event"].startswith("download_")]
    fid = err[0]["fax_id"]
    assert "503" in err[0]["detail"]
    seq = [e["event"] for e in sc.events_for(fid, "download_")]
    assert seq == ["download_error_page", "download_ok"], seq
    assert sc.results[fid]["retries"] >= 1


def test_session_expiry_on_submit_relogs_and_routes_once(scenario):
    sc = scenario("session_expire_on_route_once")
    assert_outcomes(sc)
    assert_portal_ui(sc)
    exp = [e for e in sc.events if e["event"] == "submit_expired"]
    assert len(exp) == 1
    fid = exp[0]["fax_id"]
    assert [e["event"] for e in submit_events(sc, fid)] == ["submit_expired", "submit_success"]
    s = sc.results[fid]["sessions"]
    # Re-login happened in the same B context: a second, different B session prefix.
    assert len(s["route_sessions_b"]) == 2 and len(set(s["route_sessions_b"])) == 2, s
    assert not set(s["route_sessions_b"]) & set(s["download_session_a"])
    assert any("expired" in e for e in sc.results[fid]["errors"])
    assert [r[0] for r in sc.ui["_queue_rows"]].count(fid) == 1


# ---- 5. determinism + idempotent rerun -------------------------------------

def test_replay_is_deterministic_across_fresh_portals(clean, tmp_path_factory):
    second = Scenario(tmp_path_factory, "none")
    try:
        def proj(sc):
            return {f: {k: r[k] for k in DETERMINISTIC_FIELDS} for f, r in sc.results.items()}
        assert proj(second) == proj(clean)
        assert {f: r["pdf_sha256"] for f, r in second.results.items()} == \
               {f: r["pdf_sha256"] for f, r in clean.results.items()}
    finally:
        second.close()


def test_rerun_on_same_checkpoint_is_a_noop(clean):
    """Running again against the same portal + checkpoint must not submit or re-report anything."""
    again = run_agent(clean.portal.base_url, clean.run.work_dir)
    assert again.returncode == 0, again.stderr[-3000:]
    doc = again.results_doc
    run_events = [e for e in again.events() if e["run"] == doc["run_id"]]
    assert not [e for e in run_events if e["event"].startswith("submit_")]
    assert not [e for e in run_events if e["event"] == "intent_written"]
    assert doc["newly_reported_this_run"] == []
    assert {f: r["action"] for f, r in again.results.items()} == EXPECTED_ACTIONS
