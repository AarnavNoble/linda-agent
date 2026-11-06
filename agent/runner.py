"""End-to-end worker: discover -> acquire (Session A) -> extract -> decide -> route (fresh Session B) -> verify."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from playwright.sync_api import Error as PWError, sync_playwright

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
from .state import RANK, LockHeld, Store, run_lock

ROUTING_ACTIONS = (ROUTED, NEEDS_REVIEW_ACTION)
# A FAILED outcome with one of these verification statuses means "we never got to act",
# so the next run retries it. Conflicts / rejected values stay terminal (a human must look).
RETRYABLE_FAILURES = ("not_processed", "unverified")
# Per-fax errors that must not abort the whole inbox: our own PortalError plus any
# Playwright error (navigation failure, locator timeout) while the portal misbehaves.
FAX_ERRORS = (PortalError, PWError)
EXIT_LOCKED = 75  # EX_TEMPFAIL: another run owns this work dir


class Runner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        # pid suffix: a crashed run and its immediate restart can start in the same second,
        # and run_id is the primary key of the runs table and the artifacts directory name.
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"
        self.run_dir = cfg.artifacts_dir / self.run_id
        self.store = Store(cfg.state_dir / "checkpoint.sqlite3")
        self.log = EventLog(self.run_dir / "events.jsonl", self.run_id)
        self.sessions_a: list[str] = []
        self._crash_hits: dict[str, int] = {}

    # ---- helpers -----------------------------------------------------------
    def crash_point(self, name: str, fax_id: str) -> None:
        # LINDA_AGENT_CRASH_AT=<point> crashes at the first occurrence, <point>#N at the N-th.
        point, _, nth = self.cfg.crash_at.partition("#")
        if point != name:
            return
        self._crash_hits[name] = self._crash_hits.get(name, 0) + 1
        if self._crash_hits[name] == int(nth or 1):
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
            elif (result.get("action") == FAILED
                  and (result.get("verification") or {}).get("status") in RETRYABLE_FAILURES):
                # A previous run gave up without acting (download/route never confirmed). Retry it;
                # an existing intent is kept so routing still re-reads the UI before any submit.
                self.log.emit("reconcile_retry_failed", r["fax_id"], was=result["verification"]["status"])
                self.store.update(r["fax_id"], state="INTENT" if ours else "DISCOVERED", result=None,
                                  decision=rec.get("decision") if ours else None, reported=0)

    def acquire(self, s: PortalSession, fax_ids: list[str]) -> None:
        for fid in fax_ids:
            rec = self.store.get(fid)
            t0 = time.monotonic()
            try:
                d = download_fax(s, fid, rec["pages"], self.cfg.download_dir, self.cfg.max_download_attempts,
                                 self.cfg.download_wait_ms,
                                 on_attempt=lambda st, oc, det, fid=fid: self.attempt(fid, st, oc, det))
            except (DownloadFailed, *FAX_ERRORS) as e:
                self.attempt(fid, "download", "gave_up", f"{type(e).__name__}: {e}"[:500])
                continue
            # The portal session the download actually landed in (a mid-acquire re-login changes it).
            fields = {"pdf_path": str(d.path), "sha256": d.sha256, "download_session": s.session_prefix}
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
        from .extract import tesseract
        if not tesseract.available():
            self.log.emit("crosscheck_unavailable",
                          warning="tesseract not on PATH: independent OCR safety cross-check is OFF for this run")
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
        # A fax we could not download has no content: deciding it would route a document nobody
        # read. And while its content is unknown we cannot rule out that a later fax is a resend
        # of it, so later-received faxes are held too (reported as not_processed; retried next run).
        def no_content(r):  # never downloaded, or downloaded but unreadable (e.g. vision outage)
            return not r.get("pdf_path") or bool((r.get("extraction") or {}).get("error")) \
                or not (r.get("extraction") or {}).get("pages")
        missing = [r for r in recs if no_content(r) and r["state"] != "DONE" and not r.get("intent")
                   and r.get("ui_status_at_discovery") != "Routed"]
        cutoff = min(((r["received"] or ""), r["fax_id"]) for r in missing) if missing else None
        decisions = decide_all(recs, set(failed_originals))
        for rec in recs:
            if rec.get("intent") or rec["state"] == "DONE":
                continue  # decisions freeze once an intent exists or the fax is finished
            held = cutoff is not None and ((rec["received"] or ""), rec["fax_id"]) >= cutoff
            if held and decisions[rec["fax_id"]].action != ALREADY_ROUTED:
                if rec.get("decision"):
                    self.store.update(rec["fax_id"], decision=None)
                if any(m["fax_id"] == rec["fax_id"] for m in missing):
                    self.log.emit("not_decided_no_content", rec["fax_id"])
                else:
                    self.log.emit("decision_held", rec["fax_id"], blocked_by=cutoff[1])
                continue
            d = decisions[rec["fax_id"]]
            self.store.advance(rec["fax_id"], "DECIDED", decision=d.to_dict())
            self.log.emit("decided", rec["fax_id"], action=d.action, category=d.category,
                          reasons=d.reasons, duplicate_of=d.duplicate_of)

    # ---- routing -----------------------------------------------------------
    @staticmethod
    def _sessions(seen: list[str], s) -> list[str]:
        """All Session B prefixes used, including silent re-logins inside ensure_auth()."""
        out = [x for x in seen if x]
        if s is not None and s.session_prefix and s.session_prefix not in out:
            out.append(s.session_prefix)
        return out

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
                    # Did *we* possibly submit (a click happened in this or an earlier run)?
                    ours = rec["state"] == "SUBMITTED_UNVERIFIED" or self.store.get(fid)["state"] == \
                        "SUBMITTED_UNVERIFIED" or any(a["stage"] == "submit" and a["outcome"] != "already"
                                                     for a in self.store.attempts(fid))
                    if v.matches and not ours:
                        self.attempt(fid, "route", "routed_by_other", v.detail)
                        self.finish(fid, ALREADY_ROUTED, {"status": "routed_by_other_matching_intent",
                                                          "detail": "routed by someone else before this agent "
                                                                    "submitted; values match the agent's decision",
                                                          "panel": v.panel, "queue_row": v.queue_row},
                                    errors, self._sessions(sessions_b, s), None)
                    elif v.matches:
                        self.attempt(fid, "route", "verified_existing", v.detail)
                        self.finish(fid, intent["action"], {"status": "ui_verified", "detail": v.detail,
                                                            "panel": v.panel, "queue_row": v.queue_row},
                                    errors, self._sessions(sessions_b, s), route._asdict())
                    else:
                        self.attempt(fid, "route", "conflict", v.detail)
                        self.finish(fid, FAILED, {"status": "conflict", "detail": v.detail, "panel": v.panel},
                                    errors + [v.detail], self._sessions(sessions_b, s), route._asdict())
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
                    self.finish(fid, FAILED, {"status": "rejected_invalid"}, errors, self._sessions(sessions_b, s), route._asdict())
                    return
                # success / already / unknown -> the only truth is persisted UI state
                if out.kind == "unknown":
                    errors.append(f"uncertain submit outcome (http={out.http_status}); re-reading UI before retry")
                v = verify(s, fid, route)
                if v.routed and v.matches and out.kind == "already":
                    # The portal blocked our submit: the route that exists is someone else's.
                    self.attempt(fid, "verify", "routed_by_other", v.detail)
                    self.finish(fid, ALREADY_ROUTED, {"status": "routed_by_other_matching_intent",
                                                      "detail": "portal reported 'already routed' on submit; "
                                                                "existing values match the agent's decision",
                                                      "panel": v.panel, "queue_row": v.queue_row},
                                errors, self._sessions(sessions_b, s), None)
                    return
                if v.routed and v.matches:
                    self.attempt(fid, "verify", "ok", v.detail)
                    self.finish(fid, intent["action"], {"status": "ui_verified", "detail": v.detail,
                                                        "panel": v.panel, "queue_row": v.queue_row,
                                                        "submit_outcome": out.kind},
                                errors, self._sessions(sessions_b, s), route._asdict())
                    return
                if v.routed:
                    self.finish(fid, FAILED, {"status": "conflict", "detail": v.detail}, errors + [v.detail],
                                self._sessions(sessions_b, s), route._asdict())
                    return
                self.attempt(fid, "verify", "not_routed", v.detail)
                errors.append(f"submit reported {out.kind} but fax is not routed; retrying")
            self.finish(fid, FAILED, {"status": "unverified", "detail": "route attempts exhausted"}, errors,
                        self._sessions(sessions_b, s), route._asdict())
        except FAX_ERRORS as e:
            errors.append(f"{type(e).__name__}: {e}")
            if s:
                s.screenshot(self.run_dir / "screens" / f"{fid}-error.png")
            self.attempt(fid, "route", "error", f"{type(e).__name__}: {e}"[:500])
            self.store.update(fid, result=None)
            # leave state at INTENT/SUBMITTED_UNVERIFIED: the next attempt re-reads UI before acting
            raise PortalError(f"{type(e).__name__}: {e}") from e
        finally:
            if s:
                s.close()

    # ---- main --------------------------------------------------------------
    def run(self) -> int:
        try:
            with run_lock(self.cfg.state_dir):
                return self._run_locked()
        except LockHeld as e:
            import sys
            print(f"agent: {e}; refusing to start a second worker on {self.cfg.work_dir}", file=sys.stderr)
            return EXIT_LOCKED

    def _run_locked(self) -> int:
        self.store.start_run(self.run_id)
        self.log.emit("run_started", mode=self.cfg.ocr_mode, model=self.cfg.vision_model,
                      base_url=self.cfg.base_url)
        status = "failed"
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not self.cfg.headed, slow_mo=150 if self.cfg.headed else 0)
            try:
                # Session A: discover + download everything not finished.
                a = None
                for d_try in range(1, 4):  # inbox discovery: retry transient page failures in a fresh Session A
                    a = self.session(browser, "A")
                    self.sessions_a.append(a.session_prefix)
                    try:
                        rows = discover_inbox(a)
                        break
                    except FAX_ERRORS as e:
                        self.log.emit("discover_error", attempt=d_try, error=f"{type(e).__name__}: {e}"[:300])
                        a.close()
                        if d_try == 3:
                            raise
                        time.sleep(d_try)
                try:
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
                for _pass in range(3):  # later passes: transient per-fax errors, resends of failed originals
                    errored: set[str] = set()
                    recs = sorted(self.store.all(), key=lambda r: (r["received"] or "", r["fax_id"]))
                    for rec in recs:
                        if rec["state"] == "DONE":
                            continue
                        if not rec.get("decision") and not rec.get("intent"):
                            why = [a["detail"] for a in self.store.attempts(rec["fax_id"])
                                   if a["outcome"] in ("gave_up",)][-1:] or ["no download"]
                            ext_err = (rec.get("extraction") or {}).get("error")
                            if rec.get("pdf_path") and ext_err:
                                why = [f"content could not be extracted ({ext_err}); not decided, retried next run"]
                            elif rec.get("pdf_path"):
                                why = ["decision held: an earlier-received fax has no readable content, "
                                       "so this fax cannot be ruled out as its resend"]
                            self.finish(rec["fax_id"], FAILED, {"status": "not_processed"}, why)
                            continue
                        action = (rec.get("intent") or rec["decision"])["action"]
                        try:
                            if action in ROUTING_ACTIONS:
                                self.route_fax(browser, rec)
                            else:
                                self.handle_non_routing(browser, rec)
                        except FAX_ERRORS as e:
                            errored.add(rec["fax_id"])
                            self.log.emit("fax_error", rec["fax_id"], error=f"{type(e).__name__}: {e}"[:500])
                    newly_failed = {r["fax_id"] for r in self.store.all()
                                    if (r.get("result") or {}).get("action") == FAILED}
                    if newly_failed <= failed and not errored:
                        break
                    failed = newly_failed
                    # Re-open dependants of failed originals that were not yet submitted.
                    for r in self.store.all():
                        dec = r.get("decision") or {}
                        if dec.get("duplicate_of") in failed and dec.get("action") == SKIPPED_DUPLICATE:
                            self.store.update(r["fax_id"], state="DECIDED", result=None, reported=0)
                    self.decide(failed)
                status = "completed"
            except Exception as e:  # never lose the run's outcomes: record, still write results, exit 1
                self.log.emit("run_error", error=f"{type(e).__name__}: {e}"[:500])
                status = "failed"
            finally:
                browser.close()
        paths = report.write_results(self.store, self.cfg.results_dir, self.run_id)
        recs = self.store.all()
        pending = [r["fax_id"] for r in recs if not r.get("result")]
        failed_ids = [r["fax_id"] for r in recs if (r.get("result") or {}).get("action") == FAILED]
        if status == "completed" and (pending or failed_ids):
            status = "completed_with_failures"
        self.store.finish_run(self.run_id, status)
        self.log.emit("run_finished", status=status, results=str(paths[0]), pending=pending, failed=failed_ids)
        try:  # human-readable summary on stdout (events go to stderr)
            print("\n" + paths[2].read_text() + f"\nstatus: {status} | results: {paths[0]}")
        except (OSError, IndexError):
            pass
        return 0 if status == "completed" else 1
