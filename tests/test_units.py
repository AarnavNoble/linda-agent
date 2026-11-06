"""Fast unit tests for the reliability-critical helpers (no portal, no browser)."""
from __future__ import annotations

import io
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfWriter

from agent import events, report
from agent.acquire import validate_pdf
from agent.extract.tesseract import affirmative_flags
from agent.state import LockHeld, Store, run_lock

REPO = Path(__file__).resolve().parent.parent


# ---- acquire.validate_pdf ---------------------------------------------------

def blank_pdf(pages: int) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(612, 792)
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


def scanned_pdf(pages: int) -> bytes:
    """Image-only PDF like the portal's faxes (noise images, so streams are large and incompressible)."""
    rnd = random.Random(pages)
    imgs = [Image.frombytes("L", (300, 400), bytes(rnd.getrandbits(8) for _ in range(300 * 400)))
            for _ in range(pages)]
    b = io.BytesIO()
    imgs[0].save(b, format="PDF", save_all=True, append_images=imgs[1:])
    return b.getvalue()


@pytest.mark.parametrize("make", [blank_pdf, scanned_pdf])
@pytest.mark.parametrize("n", [1, 3])
def test_valid_pdf_accepted_with_page_count(make, n):
    assert validate_pdf(make(n), n) == n
    assert validate_pdf(make(n), None) == n


@pytest.mark.parametrize("body", [
    b"<!doctype html><html><head><title>Download unavailable | Linda Mini</title></head>"
    b"<body><div role='alert' class='notice error'>Download temporarily unavailable (503).</div></body></html>",
    b"Not found",
    b"",
    b"   %PDF-1.4 leading junk",
])
def test_error_page_bytes_rejected(body):
    with pytest.raises(ValueError, match="not a PDF"):
        validate_pdf(body, 1)


@pytest.mark.parametrize("keep", [0.98, 0.9, 0.5, 0.1])
def test_truncated_pdf_rejected(keep):
    data = scanned_pdf(2)
    with pytest.raises(ValueError, match="unparseable"):
        validate_pdf(data[: int(len(data) * keep)], 2)


def test_truncated_tail_only_rejected():
    data = scanned_pdf(2)
    with pytest.raises(ValueError):
        validate_pdf(data[:-5], 2)  # lost just the %%EOF


def test_page_count_mismatch_rejected():
    with pytest.raises(ValueError, match="page count 2 != portal-reported 3"):
        validate_pdf(scanned_pdf(2), 3)


def test_pdf_header_with_garbage_body_rejected():
    with pytest.raises(ValueError, match="unparseable"):
        validate_pdf(b"%PDF-1.7\n" + os.urandom(2048), 1)


# ---- extract.tesseract.affirmative_flags -----------------------------------

@pytest.mark.parametrize("text", [
    "No urgent flag provided",
    "not urgent",
    "Non-urgent",
    "Not marked urgent or critical",
    "STATUS: final",           # STAT must be a whole word
    "Statement of benefits",
    "Nonurgent follow-up",
    "",
])
def test_affirmative_flags_ignores_negations_and_substrings(text):
    assert affirmative_flags(text) == []


@pytest.mark.parametrize("text,hit", [
    ("CRITICAL RESULT - CALLING PROVIDER", "CRITICAL RESULT - CALLING PROVIDER"),
    ("Priority: Urgent", "Priority: Urgent"),
    ("STAT", "STAT"),
    ("Non-fasting sample, CRITICAL RESULT", "Non-fasting sample, CRITICAL RESULT"),  # negation is scoped
    ("Allergies: none.  Priority: URGENT", "Allergies: none.  Priority: URGENT"),
])
def test_affirmative_flags_hits(text, hit):
    assert affirmative_flags(text) == [hit]


def test_affirmative_flags_is_line_scoped():
    assert affirmative_flags("Lab panel\nNo urgent flag\nCRITICAL VALUE\n") == ["CRITICAL VALUE"]


# Regression (FAILURES.md): negation used to leak across fields on one line and hide real flags.
@pytest.mark.parametrize("text", ["Fasting: No    STAT: Yes", "Without delay - ASAP"])
def test_affirmative_flags_negation_does_not_leak_across_fields(text):
    assert affirmative_flags(text) != []


@pytest.mark.parametrize("text", ["Non-urgent follow up", "No known critical values", "not urgent"])
def test_affirmative_flags_negation_within_clause_still_suppresses(text):
    assert affirmative_flags(text) == []


# ---- events.redact / EventLog ----------------------------------------------

def test_redact_hashes_patient_identifiers_recursively():
    rec = {"patient_name": "Ann Example", "dob": "01/02/1980", "fax_id": "X",
           "pages": [{"patient_name": "Ann Example", "date_of_birth": "01/02/1980", "category": "referral"}],
           "nested": {"patient": "Ann Example", "ok": 1}}
    out = events.redact(rec)
    blob = json.dumps(out)
    assert "Ann Example" not in blob and "01/02/1980" not in blob
    assert out["patient_name"].startswith("sha1:") and out["dob"].startswith("sha1:")
    assert out["pages"][0]["category"] == "referral" and out["fax_id"] == "X" and out["nested"]["ok"] == 1
    # Stable: the same value hashes the same way, so records stay joinable without the PHI.
    assert out["patient_name"] == out["pages"][0]["patient_name"] == out["nested"]["patient"]
    assert events.redact(rec) == out


def test_redact_leaves_empty_values_alone():
    assert events.redact({"patient_name": None, "dob": ""}) == {"patient_name": None, "dob": ""}


def test_event_log_file_is_redacted(tmp_path):
    log = events.EventLog(tmp_path / "events.jsonl", "run1")
    log.emit("extracted", "FX-1", patient_name="Ann Example", dob="01/02/1980", pages=[{"dob": "01/02/1980"}])
    txt = (tmp_path / "events.jsonl").read_text()
    assert "Ann Example" not in txt and "01/02/1980" not in txt
    rec = json.loads(txt)
    assert rec["event"] == "extracted" and rec["fax_id"] == "FX-1" and rec["run"] == "run1"


@pytest.mark.xfail(strict=True, reason=(
    "BUG: EventLog.emit redacts the JSONL record but builds its stderr summary line from the raw "
    "`data` kwargs, so top-level patient_name/dob values are printed to the console unredacted. "
    "Latent today (no caller passes them at top level) but the redaction contract is file-only."))
def test_event_log_console_line_is_redacted(tmp_path, capsys):
    events.EventLog(tmp_path / "events.jsonl", "run1").emit("x", "FX-1", patient_name="Ann Example")
    assert "Ann Example" not in capsys.readouterr().err


# ---- state.run_lock -----------------------------------------------------------

def test_run_lock_is_exclusive_and_released(tmp_path):
    with run_lock(tmp_path):
        with pytest.raises(LockHeld):
            with run_lock(tmp_path):
                pass
    with run_lock(tmp_path):  # released on exit
        pass


def test_run_lock_released_after_exception(tmp_path):
    with pytest.raises(RuntimeError):
        with run_lock(tmp_path):
            raise RuntimeError("boom")
    with run_lock(tmp_path):
        pass


def test_second_agent_process_refuses_to_run_while_lock_held(tmp_path):
    """A concurrent worker must not start (and must not touch the portal) while another holds the lock."""
    env = {**os.environ, "LINDA_WORK_DIR": str(tmp_path), "LINDA_BASE_URL": "http://127.0.0.1:9",
           "LINDA_OCR_MODE": "replay"}
    with run_lock(tmp_path / "state"):
        p = subprocess.run([sys.executable, "-m", "agent", "run"], cwd=REPO, env=env,
                           capture_output=True, text=True, timeout=60)
    assert p.returncode != 0
    assert "LockHeld" in p.stderr
    assert "run_started" not in p.stderr
    assert Store(tmp_path / "state" / "checkpoint.sqlite3").runs() == []


# ---- report.write_results report-once ------------------------------------------

def _result(fid: str) -> dict:
    return {"fax_id": fid, "action": "routed", "verification": {"status": "ui_verified"},
            "selected_fields": {"assigned_to": "a", "doc_type": "d", "folder": "f"}, "retries": 0}


def test_write_results_reports_each_outcome_exactly_once(tmp_path):
    store = Store(tmp_path / "state" / "checkpoint.sqlite3")
    for fid in ("FX-1", "FX-2"):
        store.upsert_discovered(fid, "2026-11-03 08:00", "S", 1, "New")
    out = tmp_path / "results"

    store.update("FX-1", result=_result("FX-1"))
    report.write_results(store, out, "r1")
    doc = json.loads((out / "results.json").read_text())
    assert doc["newly_reported_this_run"] == ["FX-1"] and doc["pending"] == ["FX-2"] and doc["count"] == 1
    assert store.get("FX-1")["reported"] == 1 and store.get("FX-2")["reported"] == 0

    report.write_results(store, out, "r2")
    doc = json.loads((out / "results.json").read_text())
    assert doc["newly_reported_this_run"] == [], "second write must mark nothing new"
    assert [r["fax_id"] for r in doc["results"]] == ["FX-1"]  # still present, just not re-announced

    store.update("FX-2", result=_result("FX-2"))
    report.write_results(store, out, "r3")
    doc = json.loads((out / "results.json").read_text())
    assert doc["newly_reported_this_run"] == ["FX-2"] and doc["pending"] == [] and doc["count"] == 2
    csv_rows = (out / "results.csv").read_text().strip().splitlines()
    assert len(csv_rows) == 3 and csv_rows[0].startswith("fax_id,")
    assert "| FX-2 |" in (out / "summary.md").read_text()


def test_store_survives_reopen(tmp_path):
    path = tmp_path / "checkpoint.sqlite3"
    s = Store(path)
    s.upsert_discovered("FX-1", "t", "S", 2, "New")
    s.advance("FX-1", "INTENT", intent={"key": "k"})
    s.db.close()
    rec = Store(path).get("FX-1")
    assert rec["state"] == "INTENT" and rec["intent"] == {"key": "k"}
