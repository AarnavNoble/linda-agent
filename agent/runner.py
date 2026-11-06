"""End-to-end worker: discover -> acquire (Session A) -> extract -> decide -> route (fresh Session B) -> verify."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

from . import report
from .acquire import DownloadFailed, download_fax
from .browser import PortalError, PortalSession
from .config import Config
from .decide import decide_all
from .discover import discover_inbox
from .events import EventLog
from .extract.pipeline import extract
from .route import click_submit, fill_and_review, read_status, verify
from .sop import (ALREADY_ROUTED, FAILED, NEEDS_REVIEW_ACTION, ROUTED, SKIPPED_DUPLICATE, Route)
from .state import RANK, Store, run_lock

ROUTING_ACTIONS = (ROUTED, NEEDS_REVIEW_ACTION)


class Runner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_dir = cfg.artifacts_dir / self.run_id
        self.store = Store(cfg.state_dir / "checkpoint.sqlite3")
        self.log = EventLog(self.run_dir / "events.jsonl", self.run_id)
        self.sessions_a: list[str] = []

    # ---- helpers -----------------------------------------------------------
    def crash_point(self, name: str, fax_id: str) -> None:
        if self.cfg.crash_at == name:
            self.log.emit("simulated_crash", fax_id, at=name)
            os._exit(137)  # kill -9 equivalent: no cleanup, no flush beyond what is already durable

    def attempt(self, fax_id: str, stage: str, outcome: str, detail: str = "") -> None:
        self.store.attempt(fax_id, self.run_id, stage, outcome, detail)
        self.log.emit(f"{stage}_{outcome}", fax_id, detail=detail)

    def session(self, browser, label: str, fax_id: str | None = None) -> PortalSession:
        trace = self.run_dir / "traces" / f"{label}-{fax_id or 'inbox'}-{int(time.time()*1000)}.zip"
        s = PortalSession(browser, self.cfg, label, trace_path=trace).start()
        self.log.emit("session_started", fax_id, label=label, session=s.session_prefix)
        return s

    # ---- phases ------------------------------------------------------------
    def reconcile(self, rows: list[dict]) -> None:
        """Make the checkpoint consistent with what the portal shows *now* (handles sandbox resets)."""
        for r in rows:
            rec = self.store.get(r["fax_id"])
            if rec is None:
                self.store.upsert_discovered(r["fax_id"], r["received"], r["sender"], r["pages"], r["status"])
                continue
            self.store.upsert_discovered(r["fax_id"], r["received"], r["sender"], r["pages"], r["status"])
            ours = bool(rec.get("intent"))
            result = rec.get("result") or {}
            if r["status"] == "New" and (ours and rec["state"] == "DONE" and result.get("action") in ROUTING_ACTIONS):
                self.log.emit("reconcile_portal_reset", r["fax_id"], was=result.get("action"))
                self.store.update(r["fax_id"], state="DISCOVERED", intent=None, result=None, decision=None,
                                  reported=0, ui_status_at_discovery="New")
            elif r["status"] == "New" and rec["ui_status_at_discovery"] == "Routed" and not ours:
                self.log.emit("reconcile_unrouted", r["fax_id"])
                self.store.update(r["fax_id"], state="DISCOVERED", result=None, decision=None, reported=0,
                                  ui_status_at_discovery="New")
            elif r["status"] == "Routed" and rec["ui_status_at_discovery"] == "New" and not ours:
                self.log.emit("reconcile_routed_elsewhere", r["fax_id"])
                self.store.update(r["fax_id"], state="DISCOVERED", result=None, decision=None, reported=0,
                                  ui_status_at_discovery="Routed")

    def acquire(self, s: PortalSession, fax_ids: list[str]) -> None:
        for fid in fax_ids:
            rec = self.store.get(fid)
            t0 = time.monotonic()
            try:
                d = download_fax(s, fid, rec["pages"], self.cfg.download_dir, self.cfg.max_download_attempts,
                                 self.cfg.download_wait_ms,
                                 on_attempt=lambda st, oc, det, fid=fid: self.attempt(fid, st, oc, det))
            except (DownloadFailed, PortalError) as e:
                self.attempt(fid, "download", "gave_up", str(e))
                continue
            fields = {"pdf_path": str(d.path), "sha256": d.sha256}
            if rec.get("sha256") and rec["sha256"] != d.sha256:
                fields["extraction"] = None  # content changed: re-extract
                self.log.emit("pdf_changed", fid, old=rec["sha256"][:12], new=d.sha256[:12])
            if RANK[rec["state"]] < RANK["DOWNLOADED"] or fields.get("extraction", 1) is None:
                fields["state"] = "DOWNLOADED" if RANK[rec["state"]] < RANK["INTENT"] else rec["state"]
            self.store.update(fid, **fields)
            self.log.emit("downloaded", fid, session=s.session_prefix, sha=d.sha256[:12], pages=d.pages,
                          attempts=d.attempts, ms=int((time.monotonic() - t0) * 1000))
            self.crash_point("after_download", fid)

    def extract_all(self) -> None:
        for rec in self.store.all():
            if not rec.get("pdf_path") or (rec.get("extraction") and rec["extraction"].get("sha256") == rec["sha256"]
                                            and not rec["extraction"].get("error")):
                continue
            ext = extract(__import__("pathlib").Path(rec["pdf_path"]), rec["sha256"], rec["fax_id"], rec["pages"],
                          mode=self.cfg.ocr_mode, model=self.cfg.vision_model, cache_dir=self.cfg.cache_dir)
            new_state = "EXTRACTED" if RANK[rec["state"]] < RANK["EXTRACTED"] else rec["state"]
            self.store.update(rec["fax_id"], extraction=ext, state=new_state)
            self.log.emit("extracted", rec["fax_id"], engine=ext["engine"], error=ext.get("error"),
                          cache_hit=(ext.get("vision_meta") or {}).get("cache_hit"),
                          contradictions=len(ext["crosscheck"]["contradictions"]), ms=ext["latency_ms"])

    def decide(self, failed_originals: set[str] = frozenset()) -> None:
        recs = self.store.all()
        decisions = decide_all(recs, set(failed_originals))
        for rec in recs:
            if rec.get("intent") or rec["state"] == "DONE":
                continue  # decisions freeze once an intent exists or the fax is finished
            d = decisions[rec["fax_id"]]
            self.store.advance(rec["fax_id"], "DECIDED", decision=d.to_dict())
            self.log.emit("decided", rec["fax_id"], action=d.action, category=d.category,
                          reasons=d.reasons, duplicate_of=d.duplicate_of)

    # ---- routing -----------------------------------------------------------
    def finish(self, fid: str, action: str, verification: dict, errors: list[str] | None = None,
               sessions_b: list[str] | None = None, selected: dict | None = None) -> None:
        rec = self.store.get(fid)
        res = report.build_result(rec, action, verification, errors or [], self.sessions_a, sessions_b or [],
                                  selected, self.store.attempts(fid))
        self.store.advance(fid, "DONE", result=res)
        self.log.emit("fax_done", fid, action=action, verified=verification.get("status"))

    def handle_non_routing(self, browser, rec: dict) -> None:
        """already_routed / skipped_duplicate: confirm the portal state; never submit."""
        fid, dec = rec["fax_id"], rec["decision"]
        s = self.session(browser, "V", fid)
        try:
            status, values = read_status(s, fid)
        finally:
            s.close()
        if dec["action"] == ALREADY_ROUTED:
            self.finish(fid, ALREADY_ROUTED, {"status": "preexisting_route_observed", "ui_status": status,
                                               "portal_values": values})
        else:
            ok = status == "New"
            self.finish(fid, SKIPPED_DUPLICATE, {"status": "ui_status_new" if ok else "unexpected_status",
                                                  "ui_status": status, "portal_values": values})

    def route_fax(self, browser, rec: dict) -> None:
        fid = rec["fax_id"]
        intent = rec.get("intent")
        if not intent:
            dec = rec["decision"]
            intent = {"action": dec["action"], "route": dec["route"], "note": dec["note"],
                      "key": f"{fid}:{(rec.get('sha256') or '')[:12]}", "written_at": time.time()}
            self.store.advance(fid, "INTENT", intent=intent)  # write-ahead: durable before any click
            self.log.emit("intent_written", fid, route=intent["route"])
        route = Route(**intent["route"])
        sessions_b: list[str] = []
        errors: list[str] = []
        s: PortalSession | None = None
        attempts = 0
        try:
            while attempts < self.cfg.max_route_attempts:
                attempts += 1
                if s is None:
                    s = self.session(browser, "B", fid)  # new isolated context, logged in after the download
                    sessions_b.append(s.session_prefix)
                # Always re-read portal state first: we may have submitted before a crash/503.
                status, values = read_status(s, fid)
                if status == "Routed":
                    v = verify(s, fid, route)
                    if v.matches:
                        self.attempt(fid, "route", "verified_existing", v.detail)
                        self.finish(fid, intent["action"], {"status": "ui_verified", "detail": v.detail,
                                                            "panel": v.panel, "queue_row": v.queue_row},
                                    errors, sessions_b, route._asdict())
                    else:
                        self.attempt(fid, "route", "conflict", v.detail)
                        self.finish(fid, FAILED, {"status": "conflict", "detail": v.detail, "panel": v.panel},
                                    errors + [v.detail], sessions_b, route._asdict())
                    return
                self.crash_point("before_click", fid)
                fill_and_review(s, fid, route, intent["note"])
                self.store.advance(fid, "SUBMITTED_UNVERIFIED")
                out = click_submit(s, fid, self.cfg.submit_timeout_ms)
                self.attempt(fid, "submit", out.kind, f"http={out.http_status} url={out.final_url} {out.detail}")
                self.crash_point("after_click", fid)
                if out.kind == "expired":
                    errors.append("session expired on submit; re-authenticated in the same Session B context")
                    s.ensure_auth()
                    sessions_b.append(s.session_prefix)
                    continue  # loop re-reads status before refilling
                if out.kind == "two_session":
                    errors.append("portal rejected route: no download in an earlier separate session; re-downloading")
                    s.close(); s = None
                    a = self.session(browser, "A", fid)
                    self.sessions_a.append(a.session_prefix)
                    try:
                        self.acquire(a, [fid])
                    finally:
                        a.close()
                    continue
                if out.kind == "csrf":
                    errors.append("stale form token; reloading form")
                    continue
                if out.kind == "invalid":
                    errors.append(f"portal rejected field values: {out.detail}")
                    self.finish(fid, FAILED, {"status": "rejected_invalid"}, errors, sessions_b, route._asdict())
                    return
                # success / already / unknown -> the only truth is persisted UI state
                if out.kind == "unknown":
                    errors.append(f"uncertain submit outcome (http={out.http_status}); re-reading UI before retry")
                v = verify(s, fid, route)
                if v.routed and v.matches:
                    self.attempt(fid, "verify", "ok", v.detail)
                    self.finish(fid, intent["action"], {"status": "ui_verified", "detail": v.detail,
                                                        "panel": v.panel, "queue_row": v.queue_row,
                                                        "submit_outcome": out.kind},
                                errors, sessions_b, route._asdict())
                    return
                if v.routed:
                    self.finish(fid, FAILED, {"status": "conflict", "detail": v.detail}, errors + [v.detail],
                                sessions_b, route._asdict())
                    return
                self.attempt(fid, "verify", "not_routed", v.detail)
                errors.append(f"submit reported {out.kind} but fax is not routed; retrying")
            self.finish(fid, FAILED, {"status": "unverified", "detail": "route attempts exhausted"}, errors,
                        sessions_b, route._asdict())
        except PortalError as e:
            errors.append(f"{type(e).__name__}: {e}")
            if s:
                s.screenshot(self.run_dir / "screens" / f"{fid}-error.png")
            self.attempt(fid, "route", "error", str(e))
            self.store.update(fid, result=None)
            # leave state at INTENT/SUBMITTED_UNVERIFIED: the next run will re-read UI before acting
            raise
        finally:
            if s:
                s.close()

    # ---- main --------------------------------------------------------------
    def run(self) -> int:
        with run_lock(self.cfg.state_dir):
            self.store.start_run(self.run_id)
            self.log.emit("run_started", mode=self.cfg.ocr_mode, model=self.cfg.vision_model,
                          base_url=self.cfg.base_url)
            status = "failed"
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=not self.cfg.headed, slow_mo=150 if self.cfg.headed else 0)
                try:
                    # Session A: discover + download everything not finished.
                    a = self.session(browser, "A")
                    self.sessions_a.append(a.session_prefix)
                    try:
                        rows = discover_inbox(a)
                        self.log.emit("discovered", count=len(rows), ids=[r["fax_id"] for r in rows])
                        self.reconcile(rows)
                        todo = [r["fax_id"] for r in rows
                                if self.store.get(r["fax_id"])["state"] != "DONE"
                                or not self.store.get(r["fax_id"]).get("extraction")]
                        self.acquire(a, todo)
                    finally:
                        a.close()

                    self.extract_all()
                    self.decide()

                    failed: set[str] = set()
                    for _pass in range(2):  # second pass re-decides resends whose original failed
                        recs = sorted(self.store.all(), key=lambda r: (r["received"] or "", r["fax_id"]))
                        for rec in recs:
                            if rec["state"] == "DONE":
                                continue
                            if not rec.get("decision") and not rec.get("intent"):
                                self.finish(rec["fax_id"], FAILED, {"status": "not_processed"},
                                            [a["detail"] for a in self.store.attempts(rec["fax_id"])
                                             if a["outcome"] in ("gave_up",)][-1:] or ["no download"])
                                continue
                            action = (rec.get("intent") or rec["decision"])["action"]
                            try:
                                if action in ROUTING_ACTIONS:
                                    self.route_fax(browser, rec)
                                else:
                                    self.handle_non_routing(browser, rec)
                            except PortalError as e:
                                self.log.emit("fax_error", rec["fax_id"], error=str(e))
                        newly_failed = {r["fax_id"] for r in self.store.all()
                                        if (r.get("result") or {}).get("action") == FAILED}
                        if newly_failed <= failed:
                            break
                        failed = newly_failed
                        # Re-open dependants of failed originals that were not yet submitted.
                        for r in self.store.all():
                            dec = r.get("decision") or {}
                            if dec.get("duplicate_of") in failed and dec.get("action") == SKIPPED_DUPLICATE:
                                self.store.update(r["fax_id"], state="DECIDED", result=None, reported=0)
                        self.decide(failed)
                    status = "completed"
                finally:
                    browser.close()
            paths = report.write_results(self.store, self.cfg.results_dir, self.run_id)
            self.store.finish_run(self.run_id, status)
            self.log.emit("run_finished", status=status, results=str(paths[0]))
            return 0 if status == "completed" else 1
