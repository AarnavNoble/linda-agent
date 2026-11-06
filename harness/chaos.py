"""Chaos suite: `uv run python -m agent.chaos`.

Spawns throwaway Linda Mini portals (ports 8101-8120, one fresh DB each), runs the agent
against them as real subprocesses under every injected fault, kill point and restart
scenario, and judges the outcome ONLY through:
  * the portal's browser UI (inbox status, fax detail routing panel, Routed Documents),
    read with the agent's own PortalSession in a separate, read-only session;
  * the agent's own artifacts (results.json, events.jsonl).
It never calls __admin endpoints, reads the portal DB, or reads the portal's data files.

The expected outcome of every scenario is the outcome of a clean baseline run in the same
invocation (golden), plus invariants that must hold everywhere:
  - every routed / needs_review fax is ui_verified and has exactly one matching
    Routed Documents row and a matching routing panel;
  - skipped duplicates are still New with no row; the pre-routed fax is unchanged;
  - after a submit that may have committed (success / already / 503), the agent never
    submits that fax again, in that run or any later one;
  - the route session differs from the download session;
  - every fax is reported exactly once across all runs of a scenario.
Exit status is non-zero if any check fails. Portal and agent processes are always cleaned up.
"""
from __future__ import annotations

import argparse
import atexit
import dataclasses
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent import config
from agent.config import ROOT

PORTS = range(8101, 8121)
AGENT_TIMEOUT_S = 300
LOCK_EXIT = 75
CRASH_EXIT = 137
# Submit outcomes after which the route may be persisted: a later submit would be a second route attempt.
COMMITTING_SUBMITS = ("submit_success", "submit_already", "submit_unknown")

_procs: list[subprocess.Popen] = []
_procs_lock = threading.Lock()
_port_lock = threading.Lock()
_ports_in_use: set[int] = set()


# ---------------------------------------------------------------------------
# process management
# ---------------------------------------------------------------------------
def _track(p: subprocess.Popen) -> subprocess.Popen:
    with _procs_lock:
        _procs.append(p)
    return p


def _kill_tree(p: subprocess.Popen, sig=signal.SIGTERM, wait_s: float = 8) -> None:
    if p.poll() is not None:
        return
    try:
        os.killpg(p.pid, sig)
    except (ProcessLookupError, PermissionError):
        return
    try:
        p.wait(wait_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait(5)


def cleanup_all() -> None:
    with _procs_lock:
        procs = list(_procs)
    for p in procs:
        try:
            os.kill(p.pid, signal.SIGCONT)  # a stopped process ignores SIGTERM until continued
        except (ProcessLookupError, PermissionError):
            pass
        _kill_tree(p)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _alloc_port() -> int:
    with _port_lock:
        for port in PORTS:
            if port not in _ports_in_use and _port_free(port):
                _ports_in_use.add(port)
                return port
    raise RuntimeError("no free port in 8101-8120")


def _find_portal_dir() -> Path:
    env = os.getenv("LINDA_PORTAL_DIR")
    if env:
        return Path(env)
    for parent in [ROOT, *ROOT.parents]:
        cand = parent / "linda-mini"
        if (cand / "app.py").exists():
            return cand
    raise RuntimeError("cannot find the linda-mini portal; set LINDA_PORTAL_DIR")


class Portal:
    """One throwaway portal: its own port, its own DB file, one fault mode."""

    def __init__(self, fault_mode: str, tmp: Path):
        self.fault_mode = fault_mode
        self.tmp = tmp
        self.db = tmp / "portal.sqlite3"
        self.port = _alloc_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None
        self.dir = _find_portal_dir()
        self.starts = 0

    def start(self) -> "Portal":
        uvicorn = self.dir / ".venv" / "bin" / "uvicorn"
        cmd = [str(uvicorn)] if uvicorn.exists() else [sys.executable, "-m", "uvicorn"]
        cmd += ["app:app", "--host", "127.0.0.1", "--port", str(self.port)]
        env = {**os.environ, "LINDA_DB_PATH": str(self.db), "LINDA_FAULT_MODE": self.fault_mode}
        self.starts += 1
        log = (self.tmp / f"portal-{self.starts}.log").open("w")
        self.proc = _track(subprocess.Popen(cmd, cwd=self.dir, env=env, stdout=log, stderr=subprocess.STDOUT,
                                            start_new_session=True))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"portal on {self.port} exited early; see {log.name}")
            with socket.socket() as s:
                s.settimeout(0.5)
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    return self
            time.sleep(0.2)
        raise RuntimeError(f"portal on {self.port} did not start")

    def stop(self) -> None:
        if self.proc:
            _kill_tree(self.proc)
            self.proc = None

    def reset(self) -> None:
        """What a reviewer does to reset the sandbox: stop, delete its DB, start again (same port)."""
        self.stop()
        for f in self.tmp.glob("portal.sqlite3*"):
            f.unlink()
        self.start()

    def release(self) -> None:
        self.stop()
        with _port_lock:
            _ports_in_use.discard(self.port)


# ---------------------------------------------------------------------------
# agent runs (subprocesses) and their artifacts
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class AgentRun:
    rc: int
    seconds: float
    stderr_tail: str
    results: dict | None  # results.json snapshot taken right after this run (None if not rewritten)


def _agent_env(portal: Portal, work: Path, crash_at: str = "") -> dict:
    return {**os.environ, "LINDA_BASE_URL": portal.url, "LINDA_WORK_DIR": str(work), "LINDA_OCR_MODE": "replay",
            "LINDA_CACHE_DIR": str(ROOT / "cache"), "LINDA_AGENT_CRASH_AT": crash_at, "LINDA_HEADED": "0",
            "PYTHONUNBUFFERED": "1"}


# Harness-only wrapper: the agent, unmodified, except that every download of one fax gives up
# (a persistent download failure no once-only portal fault can produce).
_FAIL_DOWNLOAD_WRAPPER = """
import os, sys
import agent.runner as r
from agent.acquire import DownloadFailed
target, real = os.environ["CHAOS_FAIL_DOWNLOAD"], r.download_fax
def download_fax(s, fax_id, *a, **k):
    if fax_id == target:
        raise DownloadFailed(f"{fax_id}: chaos: download permanently unavailable")
    return real(s, fax_id, *a, **k)
r.download_fax = download_fax
from agent.__main__ import main
sys.exit(main(["run"]))
"""


def spawn_agent(portal: Portal, work: Path, crash_at: str = "", tag: str = "run",
                fail_download: str = "") -> tuple[subprocess.Popen, Path]:
    work.mkdir(parents=True, exist_ok=True)
    log = work / f"agent-{tag}-{int(time.time() * 1000)}.log"
    argv = [sys.executable, "-m", "agent", "run"]
    env = _agent_env(portal, work, crash_at)
    if fail_download:
        argv = [sys.executable, "-c", _FAIL_DOWNLOAD_WRAPPER]
        env["CHAOS_FAIL_DOWNLOAD"] = fail_download
    p = _track(subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log.open("w"),
                                stderr=subprocess.STDOUT, start_new_session=True))
    return p, log


def _results_mtime(work: Path) -> float:
    f = work / "results" / "results.json"
    return f.stat().st_mtime if f.exists() else 0.0


def finish_agent(p: subprocess.Popen, log: Path, work: Path, before_mtime: float, t0: float) -> AgentRun:
    try:
        rc = p.wait(AGENT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _kill_tree(p, signal.SIGKILL)
        rc = -999
    tail = "\n".join(log.read_text(errors="replace").splitlines()[-25:])
    res = None
    if _results_mtime(work) > before_mtime:
        res = json.loads((work / "results" / "results.json").read_text())
    return AgentRun(rc, round(time.monotonic() - t0, 1), tail, res)


def run_agent(portal: Portal, work: Path, crash_at: str = "", tag: str = "run", fail_download: str = "") -> AgentRun:
    t0 = time.monotonic()
    before = _results_mtime(work)
    p, log = spawn_agent(portal, work, crash_at, tag, fail_download)
    return finish_agent(p, log, work, before, t0)


def load_events(work: Path) -> list[dict]:
    evs = []
    for f in (work / "runs").glob("*/events.jsonl"):
        for line in f.read_text().splitlines():
            if line.strip():
                evs.append(json.loads(line))
    evs.sort(key=lambda e: e["ts"])
    return evs


def wait_for_event(work: Path, pred, timeout_s: float, proc: subprocess.Popen | None = None) -> dict | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for e in load_events(work):
            if pred(e):
                return e
        if proc is not None and proc.poll() is not None:
            return None
        time.sleep(0.05)
    return None


# ---------------------------------------------------------------------------
# UI oracle: a separate read-only browser session using the agent's own PortalSession
# ---------------------------------------------------------------------------
def ui_snapshot(portal: Portal) -> dict:
    from playwright.sync_api import sync_playwright

    from agent.browser import PortalSession
    from agent.discover import discover_inbox

    cfg = dataclasses.replace(config.load(), base_url=portal.url)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            s = PortalSession(browser, cfg, "oracle").start()
            rows = discover_inbox(s)
            faxes = {}
            for r in rows:
                s.goto_fax(r["fax_id"])
                panel = s.routing_panel()
                faxes[r["fax_id"]] = {
                    "received": r["received"], "inbox_status": r["status"], "detail_status": s.transmission_info().get("Status"),
                    "panel": {"assigned_to": panel.assigned_to, "doc_type": panel.doc_type,
                              "folder": panel.folder} if panel.routed else None}
            s._click_nav("Routed Documents")
            queue = []
            for tr in s.page.locator("table[aria-label='Routed documents'] tbody tr").all():
                c = [x.inner_text().strip() for x in tr.locator("td").all()]
                if len(c) >= 4:
                    queue.append({"fax_id": c[0], "doc_type": c[1], "assigned_to": c[2], "folder": c[3]})
            s.close()
        finally:
            browser.close()
    return {"faxes": faxes, "queue": queue}


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------
class Checks:
    def __init__(self):
        self.items: list[dict] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.items.append({"check": name, "ok": bool(ok), "detail": detail if not ok else (detail or "")})
        return ok

    @property
    def ok(self) -> bool:
        return all(i["ok"] for i in self.items) and bool(self.items)


def _route_of(sel: dict | None) -> dict | None:
    return {k: sel[k] for k in ("assigned_to", "doc_type", "folder")} if sel else None


def check_final_state(c: Checks, ui: dict, results: dict | None, golden: dict | None) -> None:
    """Invariants on the end state of a scenario (portal UI + final results.json)."""
    if not c.check("results.json written", results is not None, "no results.json from the final run"):
        return
    res = {r["fax_id"]: r for r in results["results"]}
    ui_ids = set(ui["faxes"])
    c.check("every inbox fax has a result", set(res) == ui_ids and not results["pending"],
            f"ui={sorted(ui_ids)} results={sorted(res)} pending={results['pending']}")
    failed = [f for f, r in res.items() if r["action"] == "failed"]
    c.check("no failed outcomes", not failed, f"failed: {failed}")

    rows_by_fax: dict[str, list[dict]] = {}
    for row in ui["queue"]:
        rows_by_fax.setdefault(row["fax_id"], []).append(row)
    dup_rows = {f: len(v) for f, v in rows_by_fax.items() if len(v) != 1}
    c.check("at most one Routed Documents row per fax", not dup_rows, f"{dup_rows}")

    bad = []
    for fid, r in res.items():
        u = ui["faxes"].get(fid, {})
        rows = rows_by_fax.get(fid, [])
        want = _route_of(r.get("selected_fields"))
        if r["action"] in ("routed", "needs_review"):
            if (r.get("verification") or {}).get("status") != "ui_verified":
                bad.append(f"{fid}: verification={r.get('verification', {}).get('status')}")
            if u.get("detail_status") != "Routed" or u.get("inbox_status") != "Routed":
                bad.append(f"{fid}: UI status {u.get('inbox_status')}/{u.get('detail_status')}, expected Routed")
            if u.get("panel") != want:
                bad.append(f"{fid}: routing panel {u.get('panel')} != selected {want}")
            if len(rows) != 1 or _route_of(rows[0]) != want:
                bad.append(f"{fid}: Routed Documents rows {rows} != selected {want}")
        elif r["action"] == "skipped_duplicate":
            if u.get("detail_status") != "New" or rows:
                bad.append(f"{fid}: duplicate should stay New with no row; status={u.get('detail_status')} rows={rows}")
        elif r["action"] == "already_routed":
            if u.get("detail_status") != "Routed" or len(rows) != 1:
                bad.append(f"{fid}: pre-routed fax should show one route; status={u.get('detail_status')} rows={rows}")
    c.check("UI matches every result (panel + Routed Documents)", not bad, "; ".join(bad))
    unexplained = [f for f in rows_by_fax if res.get(f, {}).get("action") not in ("routed", "needs_review",
                                                                                     "already_routed")]
    c.check("no Routed Documents row the agent cannot account for", not unexplained, f"{unexplained}")

    if golden is not None:
        diffs = []
        gres = {r["fax_id"]: r for r in golden["results"]["results"]}
        for fid, g in gres.items():
            r = res.get(fid)
            if r is None:
                diffs.append(f"{fid}: missing")
                continue
            if r["action"] != g["action"] or _route_of(r.get("selected_fields")) != _route_of(g.get("selected_fields")) \
                    or r.get("duplicate_of") != g.get("duplicate_of"):
                diffs.append(f"{fid}: {r['action']}/{_route_of(r.get('selected_fields'))} vs golden "
                             f"{g['action']}/{_route_of(g.get('selected_fields'))}")
            gu, u = golden["ui"]["faxes"].get(fid), ui["faxes"].get(fid)
            if gu != u:
                diffs.append(f"{fid}: UI {u} vs golden UI {gu}")
        c.check("outcome identical to clean baseline (actions, fields, UI)", not diffs, "; ".join(diffs))


def check_event_invariants(c: Checks, events: list[dict], results: dict | None) -> None:
    """Invariants over every run of the scenario (events.jsonl of all runs in the work dir)."""
    # no submit after a submit that may have committed
    committed_at: dict[str, tuple[str, str]] = {}
    second = []
    for e in events:
        ev, fid = e["event"], e.get("fax_id")
        if not fid or not ev.startswith("submit_"):
            continue
        if fid in committed_at:
            second.append(f"{fid}: {ev} in run {e['run']} after {committed_at[fid][0]} in run {committed_at[fid][1]}")
        if ev in COMMITTING_SUBMITS:
            committed_at.setdefault(fid, (ev, e["run"]))
    c.check("no submit after a possibly-committed submit (any run)", not second, "; ".join(second))
    errs = [f"{e.get('fax_id')}: {e.get('error')}" for e in events if e["event"] == "fax_error"]
    c.check("no unhandled per-fax errors", not errs, "; ".join(errs)[:600])
    if results:
        last_dl: dict[str, str] = {}
        for e in events:
            if e["event"] == "downloaded" and e.get("fax_id"):
                last_dl[e["fax_id"]] = e.get("session")
        wrong = [f"{r['fax_id']}: reported {r['sessions']['download_session_a']} but downloaded in "
                 f"{last_dl.get(r['fax_id'])}" for r in results["results"]
                 if r["fax_id"] in last_dl and r["sessions"]["download_session_a"] != [last_dl[r["fax_id"]]]]
        c.check("results attribute each download to the session it happened in", not wrong, "; ".join(wrong))
        overlap = []
        for r in results["results"]:
            a = set(r["sessions"]["download_session_a"])
            b = set(x for x in r["sessions"]["route_sessions_b"] if x)
            if a & b:
                overlap.append(r["fax_id"])
        c.check("route session never equals the download session", not overlap, f"{overlap}")


def check_reported_once(c: Checks, runs: list[AgentRun], all_ids: set[str]) -> None:
    reported: list[str] = []
    for r in runs:
        if r.results:
            reported += r.results["newly_reported_this_run"]
    dups = sorted({x for x in reported if reported.count(x) > 1})
    c.check("every fax reported exactly once across runs", not dups and set(reported) == all_ids,
            f"duplicated={dups} missing={sorted(all_ids - set(reported))}")


def events_for(events: list[dict], fid: str | None = None, run: str | None = None, prefix: str = "") -> list[dict]:
    return [e for e in events if (fid is None or e.get("fax_id") == fid) and (run is None or e["run"] == run)
            and e["event"].startswith(prefix)]


def run_ids(events: list[dict]) -> list[str]:
    out = []
    for e in events:
        if e["event"] == "run_started" and e["run"] not in out:
            out.append(e["run"])
    return out


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Case:
    name: str
    scenario: str
    fn: object


def case_baseline(tmp: Path, golden=None) -> tuple[Checks, dict, dict]:
    """Clean run, plus a second worker launched on the same work dir while the first holds the lock."""
    c = Checks()
    portal = Portal("none", tmp).start()
    work = tmp / "work"
    try:
        t0 = time.monotonic()
        before = _results_mtime(work)
        p1, log1 = spawn_agent(portal, work, tag="first")
        started = wait_for_event(work, lambda e: e["event"] == "run_started", 60, p1)
        c.check("first worker started", started is not None)
        t1 = time.monotonic()
        second = run_agent(portal, work, tag="second")
        c.check("second worker on same work dir fails fast on the lock",
                second.rc == LOCK_EXIT and (time.monotonic() - t1) < 30 and p1.poll() is None,
                f"rc={second.rc} after {time.monotonic() - t1:.1f}s; first still running={p1.poll() is None}")
        c.check("second worker left no run behind", second.results is None and len(run_ids(load_events(work))) == 1,
                f"runs={run_ids(load_events(work))}")
        first = finish_agent(p1, log1, work, before, t0)
        c.check("first worker exits 0", first.rc == 0, f"rc={first.rc}\n{first.stderr_tail}")
        ui = ui_snapshot(portal)
        check_final_state(c, ui, first.results, None)
        check_event_invariants(c, load_events(work), first.results)
        check_reported_once(c, [first], set(ui["faxes"]))
        rerun = run_agent(portal, work, tag="noop")
        c.check("idempotent rerun: exits 0, reports nothing new, submits nothing",
                rerun.rc == 0 and rerun.results is not None and not rerun.results["newly_reported_this_run"]
                and not events_for(load_events(work), run=run_ids(load_events(work))[-1], prefix="submit_"),
                f"rc={rerun.rc} newly={(rerun.results or {}).get('newly_reported_this_run')}")
        ui2 = ui_snapshot(portal)
        c.check("idempotent rerun leaves UI unchanged", ui2 == ui)
        return c, {"results": first.results, "ui": ui}, {"seconds": first.seconds}
    finally:
        portal.release()


def _fault_case(mode: str, extra):
    def fn(tmp: Path, golden) -> tuple[Checks, dict, dict]:
        c = Checks()
        portal = Portal(mode, tmp).start()
        work = tmp / "work"
        try:
            r = run_agent(portal, work)
            c.check("agent exits 0", r.rc == 0, f"rc={r.rc}\n{r.stderr_tail}")
            ui = ui_snapshot(portal)
            evs = load_events(work)
            check_final_state(c, ui, r.results, golden)
            check_event_invariants(c, evs, r.results)
            check_reported_once(c, [r], set(ui["faxes"]))
            info = extra(c, evs, r.results or {"results": []}) or {}
            return c, None, {"seconds": r.seconds, **info}
        finally:
            portal.release()
    return fn


def _extra_download_503(c: Checks, evs, results):
    errs = events_for(evs, prefix="download_error_page")
    if not c.check("fault fired: a download returned an HTML error page", len(errs) == 1, f"{len(errs)} error pages"):
        return {}
    fid = errs[0]["fax_id"]
    after = [e for e in events_for(evs, fid) if e["ts"] > errs[0]["ts"]]
    ok = [e for e in after if e["event"] == "download_ok"]
    c.check("error page detected without waiting out a timeout",
            not events_for(evs, fid, prefix="download_timeout"), "a download timed out instead")
    c.check(f"{fid}: re-navigated and retried to a valid PDF (pages + sha validated)",
            bool(ok) and "pages=" in ok[0].get("detail", ""), f"after-error events: {[e['event'] for e in after][:6]}")
    c.check(f"{fid}: no invalid PDF was ever accepted", not events_for(evs, fid, prefix="download_invalid_pdf"))
    return {"fault_fax": fid}


def _extra_session_expire(c: Checks, evs, results):
    exp = events_for(evs, prefix="submit_expired")
    if not c.check("fault fired: session expired on submit", len(exp) == 1, f"{len(exp)} expiries"):
        return {}
    fid = exp[0]["fax_id"]
    b_ctx = [e for e in events_for(evs, fid, prefix="session_started") if e.get("label") == "B"]
    res = next((r for r in results["results"] if r["fax_id"] == fid), {})
    sess_b = res.get("sessions", {}).get("route_sessions_b", [])
    c.check(f"{fid}: re-login happened in the SAME Session B browser context",
            len(b_ctx) == 1 and len(sess_b) == 2 and sess_b[0] != sess_b[1],
            f"B contexts opened={len(b_ctx)} portal sessions used={sess_b}")
    after = [e["event"] for e in events_for(evs, fid) if e["ts"] > exp[0]["ts"]]
    c.check(f"{fid}: refilled, resubmitted once and verified",
            after.count("submit_success") == 1 and "verify_ok" in after, f"after expiry: {after}")
    return {"fault_fax": fid}


def _extra_commit_503(c: Checks, evs, results):
    unk = events_for(evs, prefix="submit_unknown")
    if not c.check("fault fired: route committed but response was 503", len(unk) == 1 and "http=503" in
                   unk[0].get("detail", ""), f"{[e.get('detail') for e in unk]}"):
        return {}
    fid = unk[0]["fax_id"]
    subs = events_for(evs, fid, prefix="submit_")
    c.check(f"{fid}: exactly one submit (no second submit after the 503)", len(subs) == 1,
            f"{[e['event'] for e in subs]}")
    after = [e["event"] for e in events_for(evs, fid) if e["ts"] > unk[0]["ts"]]
    c.check(f"{fid}: re-read UI, found it routed and verified", "verify_ok" in after, f"after 503: {after}")
    return {"fault_fax": fid}


def _kill_case(point: str):
    base = point.partition("#")[0]

    def fn(tmp: Path, golden) -> tuple[Checks, dict, dict]:
        c = Checks()
        portal = Portal("none", tmp).start()
        work = tmp / "work"
        try:
            r1 = run_agent(portal, work, crash_at=point, tag="crash")
            evs = load_events(work)
            crash = events_for(evs, prefix="simulated_crash")
            c.check(f"run 1 hard-exited at {point}", r1.rc == CRASH_EXIT and len(crash) == 1 and r1.results is None,
                    f"rc={r1.rc} crash events={len(crash)}")
            done1 = {e["fax_id"] for e in events_for(evs, prefix="fax_done")}
            if "#" in point:
                c.check("run 1 finished some faxes before dying (they are unreported until resume)", bool(done1),
                        "no fax finished before the crash")
            if not crash:
                return c, None, {}
            fid, run1 = crash[0]["fax_id"], crash[0]["run"]
            subs1 = events_for(evs, fid, run1, "submit_")
            r2 = run_agent(portal, work, tag="resume")
            r3 = run_agent(portal, work, tag="noop")
            c.check("resume run exits 0", r2.rc == 0, f"rc={r2.rc}\n{r2.stderr_tail}")
            evs = load_events(work)
            runs = run_ids(evs)
            run2 = runs[1] if len(runs) > 1 else None
            subs2 = events_for(evs, fid, run2, "submit_")
            if base == "after_download":
                c.check("run 1 submitted nothing", not events_for(evs, run=run1, prefix="submit_"))
            elif base == "before_click":
                c.check(f"{fid}: no submit before the crash; exactly one after resume",
                        not subs1 and [e["event"] for e in subs2] == ["submit_success"],
                        f"run1={[e['event'] for e in subs1]} run2={[e['event'] for e in subs2]}")
            elif base == "after_click":
                c.check(f"{fid}: submitted before the crash, NOT resubmitted on resume",
                        [e["event"] for e in subs1] == ["submit_success"] and not subs2,
                        f"run1={[e['event'] for e in subs1]} run2={[e['event'] for e in subs2]}")
                c.check(f"{fid}: resume re-read the UI and verified the existing route",
                        bool(events_for(evs, fid, run2, "route_verified_existing")))
            ui = ui_snapshot(portal)
            check_final_state(c, ui, r2.results, golden)
            check_event_invariants(c, evs, r2.results)
            check_reported_once(c, [r1, r2, r3], set(ui["faxes"]))
            c.check("third (no-op) run exits 0 and submits nothing", r3.rc == 0 and len(runs) == 3
                    and not events_for(evs, run=runs[-1], prefix="submit_"), f"rc={r3.rc} runs={len(runs)}")
            return c, None, {"crashed_fax": fid, "done_before_crash": len(done1), "seconds": r1.seconds + r2.seconds}
        finally:
            portal.release()
    return fn


def case_portal_reset(tmp: Path, golden) -> tuple[Checks, dict, dict]:
    """Full run, then the sandbox is reset under the agent's (now stale) checkpoint; rerun same work dir."""
    c = Checks()
    portal = Portal("none", tmp).start()
    work = tmp / "work"
    try:
        r1 = run_agent(portal, work, tag="before-reset")
        c.check("first run exits 0", r1.rc == 0, f"rc={r1.rc}")
        portal.reset()
        ui_reset = ui_snapshot(portal)
        c.check("portal really was reset (only the pre-routed fax is Routed)",
                sum(1 for f in ui_reset["faxes"].values() if f["detail_status"] == "Routed") == len(ui_reset["queue"])
                and len(ui_reset["queue"]) <= 1, f"queue={ui_reset['queue']}")
        r2 = run_agent(portal, work, tag="after-reset")
        c.check("rerun after reset exits 0", r2.rc == 0, f"rc={r2.rc}\n{r2.stderr_tail}")
        evs = load_events(work)
        run2 = run_ids(evs)[-1]
        routed_before = {r["fax_id"] for r in (r1.results or {"results": []})["results"]
                         if r["action"] in ("routed", "needs_review")}
        reset_seen = {e["fax_id"] for e in events_for(evs, run=run2, prefix="reconcile_portal_reset")}
        c.check("reconcile noticed every fax we had routed is New again", reset_seen == routed_before,
                f"reconciled={sorted(reset_seen)} routed before={sorted(routed_before)}")
        resub = {e["fax_id"] for e in events_for(evs, run=run2, prefix="submit_success")}
        c.check("every such fax was routed again (one submit each)", resub == routed_before,
                f"resubmitted={sorted(resub)}")
        ui = ui_snapshot(portal)
        check_final_state(c, ui, r2.results, golden)
        # the 'no submit after commit' invariant is per portal lifetime: check only the post-reset run
        check_event_invariants(c, [e for e in evs if e["run"] == run2], r2.results)
        c.check("reset outcomes re-reported (new portal world), nothing else",
                set((r2.results or {}).get("newly_reported_this_run", [])) == routed_before,
                f"{(r2.results or {}).get('newly_reported_this_run')}")
        return c, None, {"seconds": r1.seconds + r2.seconds}
    finally:
        portal.release()


def case_reset_mid_run_409(tmp: Path, golden) -> tuple[Checks, dict, dict]:
    """Portal reset between Session A's first download and routing: provokes a genuine 409
    (the download the agent made no longer exists) and an invalidated Session A login."""
    c = Checks()
    portal = Portal("none", tmp).start()
    work = tmp / "work"
    try:
        t0 = time.monotonic()
        before = _results_mtime(work)
        p, log = spawn_agent(portal, work)
        first_dl = wait_for_event(work, lambda e: e["event"] == "downloaded", 90, p)
        if not c.check("agent downloaded a first fax", first_dl is not None):
            _kill_tree(p, signal.SIGKILL)
            return c, None, {}
        os.kill(p.pid, signal.SIGSTOP)  # freeze the worker so the reset lands between two of its steps
        time.sleep(0.5)
        try:
            portal.reset()
        finally:
            os.kill(p.pid, signal.SIGCONT)
        r = finish_agent(p, log, work, before, t0)
        c.check("agent exits 0", r.rc == 0, f"rc={r.rc}\n{r.stderr_tail}")
        evs = load_events(work)
        fid = first_dl["fax_id"]
        two = events_for(evs, fid, prefix="submit_two_session")
        c.check(f"{fid}: portal rejected the route with 409 (download lost in reset)", len(two) >= 1,
                f"{[e['event'] for e in events_for(evs, fid, prefix='submit_')]}")
        if two:
            after = [e for e in events_for(evs, fid) if e["ts"] > two[0]["ts"]]
            a_new = [e for e in after if e["event"] == "session_started" and e.get("label") == "A"]
            c.check(f"{fid}: re-downloaded in a new Session A, then routed from a new Session B",
                    bool(a_new) and any(e["event"] == "download_ok" for e in after)
                    and any(e["event"] == "submit_success" for e in after),
                    f"after 409: {[e['event'] for e in after]}")
        first_route = min([e["ts"] for e in evs if e["event"].startswith("submit_")] or [float("inf")])
        a_sessions = {e.get("session") for e in evs if e["event"] == "downloaded" and e["ts"] < first_route}
        c.check("Session A re-authenticated after its login vanished in the reset", len(a_sessions) >= 2,
                f"download sessions before routing: {a_sessions}")
        ui = ui_snapshot(portal)
        check_final_state(c, ui, r.results, golden)
        check_event_invariants(c, evs, r.results)
        check_reported_once(c, [r], set(ui["faxes"]))
        return c, None, {"seconds": r.seconds, "fault_fax": fid}
    finally:
        portal.release()


def case_download_gives_up(tmp: Path, golden) -> tuple[Checks, dict, dict]:
    """The earliest New fax can never be downloaded in run 1. Nothing unread may be routed, nothing that
    could be its resend may be routed, and run 2 (download healthy again) must converge to the baseline."""
    c = Checks()
    portal = Portal("none", tmp).start()
    work = tmp / "work"
    try:
        inbox = ui_snapshot(portal)["faxes"]
        order = sorted(inbox, key=lambda f: (inbox[f]["received"], f))
        target = next(f for f in order if inbox[f]["detail_status"] == "New")
        later = {f for f in order if (inbox[f]["received"], f) > (inbox[target]["received"], target)
                 and inbox[f]["detail_status"] == "New"}
        r1 = run_agent(portal, work, tag="broken-download", fail_download=target)
        evs = load_events(work)
        run1 = run_ids(evs)[0]
        res1 = {r["fax_id"]: r for r in (r1.results or {"results": []})["results"]}
        c.check("run 1 exits 1 (completed with failures), not a crash", r1.rc == 1 and r1.results is not None,
                f"rc={r1.rc}\n{r1.stderr_tail}")
        t = res1.get(target, {})
        c.check(f"{target}: undownloadable fax reported failed/not_processed, never decided or submitted",
                t.get("action") == "failed" and (t.get("verification") or {}).get("status") == "not_processed"
                and not events_for(evs, target, run1, "decided") and not events_for(evs, target, run1, "submit_"),
                f"result={t.get('action')}/{(t.get('verification') or {}).get('status')}")
        sub_later = sorted({e["fax_id"] for e in events_for(evs, run=run1, prefix="submit_")} & later)
        held = sorted({e["fax_id"] for e in events_for(evs, run=run1, prefix="decision_held")})
        c.check("faxes received after it (possible resends) were held, not submitted",
                not sub_later and set(held) == later, f"submitted={sub_later} held={held} later={sorted(later)}")
        earlier_ok = [f for f in order if f not in later and f != target and res1.get(f, {}).get("action") == "failed"]
        c.check("faxes received before it were unaffected", not earlier_ok, f"{earlier_ok}")
        ui1 = ui_snapshot(portal)
        c.check("portal untouched for the failed and held faxes",
                all(ui1["faxes"][f]["detail_status"] == "New" for f in later | {target}))

        r2 = run_agent(portal, work, tag="healthy")
        c.check("run 2 (download healthy) exits 0", r2.rc == 0, f"rc={r2.rc}\n{r2.stderr_tail}")
        evs = load_events(work)
        run2 = run_ids(evs)[-1]
        retried = {e["fax_id"] for e in events_for(evs, run=run2, prefix="reconcile_retry_failed")}
        c.check("run 2 retried every failed fax", retried == later | {target}, f"retried={sorted(retried)}")
        ui = ui_snapshot(portal)
        check_final_state(c, ui, r2.results, golden)
        check_event_invariants(c, evs, r2.results)
        c.check("run 2 re-reported exactly the faxes whose outcome changed",
                set((r2.results or {}).get("newly_reported_this_run", [])) == later | {target},
                f"{(r2.results or {}).get('newly_reported_this_run')}")
        return c, None, {"failed_fax": target, "held": len(held), "seconds": r1.seconds + r2.seconds}
    finally:
        portal.release()


def case_download_reauth(tmp: Path, golden) -> tuple[Checks, dict, dict]:
    """Session A's login expires between opening the fax and clicking Download (cookie dropped,
    which is what an expired login looks like to the browser). Pins the stranded-on-/login bug."""
    from playwright.sync_api import sync_playwright

    from agent.acquire import download_fax
    from agent.browser import PortalSession
    from agent.discover import discover_inbox

    c = Checks()
    portal = Portal("none", tmp).start()
    try:
        cfg = dataclasses.replace(config.load(), base_url=portal.url)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                s = PortalSession(browser, cfg, "A").start()
                row = discover_inbox(s)[0]
                real_goto, fired = s.goto_fax, []

                def goto_then_expire(fid):
                    real_goto(fid)
                    if not fired:
                        fired.append(fid)
                        s.context.clear_cookies()

                s.goto_fax = goto_then_expire
                attempts = []
                try:
                    d = download_fax(s, row["fax_id"], row["pages"], tmp / "dl", 3, 5000,
                                     on_attempt=lambda *a: attempts.append(a[1]))
                    err = None
                except Exception as e:  # noqa: BLE001 - this is the assertion
                    d, err = None, f"{type(e).__name__}: {e}"
                c.check("expiry fired before the Download click", bool(fired), f"attempts={attempts}")
                c.check("re-authenticated and downloaded a valid PDF of the same fax",
                        d is not None and d.pages == row["pages"] and s.logins == 2,
                        f"error={err} attempts={attempts} logins={s.logins}")
                c.check("tab returned to the fax page (not stranded on /login)",
                        not s.on_login_page(), s.page.url)
                s.close()
            finally:
                browser.close()
        return c, None, {}
    finally:
        portal.release()


def case_two_session_409(tmp: Path, golden) -> tuple[Checks, dict, dict]:
    """Submitting from the SAME session that downloaded must be refused (409) and classified as such,
    and must not create a route. Uses the agent's own fill/submit code on a fresh portal."""
    from playwright.sync_api import sync_playwright

    from agent.acquire import download_fax
    from agent.browser import PortalSession
    from agent.discover import discover_inbox
    from agent.route import click_submit, fill_and_review, read_status
    from agent.sop import NEEDS_REVIEW

    c = Checks()
    portal = Portal("none", tmp).start()
    try:
        cfg = dataclasses.replace(config.load(), base_url=portal.url)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                s = PortalSession(browser, cfg, "A").start()
                row = next(r for r in discover_inbox(s) if r["status"] == "New")
                fid = row["fax_id"]
                download_fax(s, fid, row["pages"], tmp / "dl", 3, 5000)
                s.goto_fax(fid)
                fill_and_review(s, fid, NEEDS_REVIEW, "chaos: same-session submit must be refused")
                out = click_submit(s, fid, 20000)
                c.check("same-session submit classified as two_session (HTTP 409)",
                        out.kind == "two_session" and out.http_status == 409, f"{out}")
                status, _ = read_status(s, fid)
                c.check("refused submit created no route", status == "New", f"status={status}")
                s.close()
            finally:
                browser.close()
        ui = ui_snapshot(portal)
        c.check("Routed Documents unchanged (pre-routed fax only)", len(ui["queue"]) <= 1, f"{ui['queue']}")
        return c, None, {"fax": fid}
    finally:
        portal.release()


CASES = [
    Case("baseline+lock", "clean run; 2nd worker on same work dir; idempotent rerun", case_baseline),
    Case("download_503_once", "download returns HTML 503 once", _fault_case("download_503_once", _extra_download_503)),
    Case("session_expire_on_route_once", "session deleted during submit",
         _fault_case("session_expire_on_route_once", _extra_session_expire)),
    Case("route_commit_then_503_once", "route saved, response 503",
         _fault_case("route_commit_then_503_once", _extra_commit_503)),
    Case("kill@after_download", "hard exit after first download, resume", _kill_case("after_download")),
    Case("kill@before_click", "hard exit after INTENT, before submit, resume", _kill_case("before_click")),
    Case("kill@after_click", "hard exit after submit, before verify, resume", _kill_case("after_click")),
    Case("kill@after_click#4", "hard exit after the 4th submit (3 faxes done, unreported), resume",
         _kill_case("after_click#4")),
    Case("portal_reset_stale_checkpoint", "full run, sandbox reset, rerun same work dir", case_portal_reset),
    Case("reset_mid_run_409", "sandbox reset after first download: 409 + dead Session A", case_reset_mid_run_409),
    Case("download_gives_up", "one fax undownloadable in run 1, healthy run 2", case_download_gives_up),
    Case("download_reauth", "login expires right before Download click", case_download_reauth),
    Case("two_session_409", "same-session submit is refused and classified", case_two_session_409),
]


# ---------------------------------------------------------------------------
# driver + matrix
# ---------------------------------------------------------------------------
def _run_case(case: Case, base_tmp: Path, golden) -> dict:
    tmp = base_tmp / case.name.replace("@", "_").replace("+", "_")
    tmp.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    try:
        checks, gold, info = case.fn(tmp, golden)
    except Exception as e:  # noqa: BLE001 - a crashed case is a failed case
        checks, gold, info = Checks(), None, {}
        checks.check("case ran to completion", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
    row = {"case": case.name, "scenario": case.scenario, "pass": checks.ok, "seconds": round(time.monotonic() - t0, 1),
           "checks": checks.items, "info": info, "tmp": str(tmp)}
    mark = "PASS" if checks.ok else "FAIL"
    print(f"[chaos] {mark} {case.name} ({row['seconds']}s, {sum(i['ok'] for i in checks.items)}/{len(checks.items)})",
          file=sys.stderr, flush=True)
    for i in checks.items:
        if not i["ok"]:
            print(f"        x {i['check']}: {i['detail'][:400]}", file=sys.stderr, flush=True)
    return {"row": row, "golden": gold}


def write_matrix(rows: list[dict], out_dir: Path, total_s: float) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = all(r["pass"] for r in rows)
    doc = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S %Z"), "pass": ok, "total_seconds": round(total_s, 1),
           "cases": [{k: v for k, v in r.items() if k != "tmp"} for r in rows]}
    jp = out_dir / "chaos_matrix.json"
    jp.write_text(json.dumps(doc, indent=2, default=str))
    lines = [f"# Chaos matrix — {'PASS' if ok else 'FAIL'}", "",
             f"Generated {doc['generated_at']} by `uv run python -m agent.chaos` in {doc['total_seconds']}s. "
             "Each case runs against its own throwaway portal (fresh DB, own port) with the agent as a "
             "subprocess; outcomes are judged only through the portal UI (separate read-only session) and the "
             "agent's results.json / events.jsonl.", "",
             "| Case | Scenario | Result | Checks | Time |", "|---|---|---|---|---|"]
    for r in rows:
        n_ok = sum(i["ok"] for i in r["checks"])
        lines.append(f"| `{r['case']}` | {r['scenario']} | {'PASS' if r['pass'] else '**FAIL**'} | "
                     f"{n_ok}/{len(r['checks'])} | {r['seconds']}s |")
    lines += ["", "## Checks per case", ""]
    for r in rows:
        lines.append(f"### `{r['case']}` — {'PASS' if r['pass'] else 'FAIL'}")
        info = {k: v for k, v in r["info"].items() if k != "seconds"}
        if info:
            lines.append(f"_{', '.join(f'{k}: {v}' for k, v in info.items())}_")
        lines.append("")
        for i in r["checks"]:
            lines.append(f"- {'PASS' if i['ok'] else '**FAIL**'} {i['check']}" + (f" — `{i['detail'][:300]}`" if not i['ok'] and i['detail'] else ""))
        lines.append("")
    mp = out_dir / "chaos_matrix.md"
    mp.write_text("\n".join(lines))
    return mp, jp


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agent.chaos", description=__doc__.split("\n")[0])
    ap.add_argument("--cases", default="", help="comma-separated case names (baseline always runs)")
    ap.add_argument("--jobs", type=int, default=4, help="cases run in parallel after the baseline")
    ap.add_argument("--keep", action="store_true", help="keep temp dirs (portal DBs, work dirs, logs)")
    ap.add_argument("--out", default=str(ROOT / "results"), help="where chaos_matrix.md/json are written")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)
    if args.list:
        for c in CASES:
            print(f"{c.name:32} {c.scenario}")
        return 0
    wanted = {x.strip() for x in args.cases.split(",") if x.strip()}
    unknown = wanted - {c.name for c in CASES}
    if unknown:
        print(f"unknown cases: {sorted(unknown)}", file=sys.stderr)
        return 2

    atexit.register(cleanup_all)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: (cleanup_all(), os._exit(130)))
    base_tmp = Path(tempfile.mkdtemp(prefix="linda-chaos-"))
    t0 = time.monotonic()
    print(f"[chaos] work root {base_tmp}", file=sys.stderr, flush=True)
    try:
        base = _run_case(CASES[0], base_tmp, None)
        rows = [base["row"]]
        golden = base["golden"] if base["row"]["pass"] else None
        rest = [c for c in CASES[1:] if not wanted or c.name in wanted]
        if golden is None:
            print("[chaos] baseline failed: golden outcome unavailable; other cases check invariants only",
                  file=sys.stderr)
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
            rows += [f.result()["row"] for f in [ex.submit(_run_case, c, base_tmp, golden) for c in rest]]
    finally:
        cleanup_all()
        if not args.keep:
            shutil.rmtree(base_tmp, ignore_errors=True)
    total = time.monotonic() - t0
    mp, jp = write_matrix(rows, Path(args.out), total)
    ok = all(r["pass"] for r in rows)
    width = max(len(r["case"]) for r in rows)
    print("\n" + "\n".join(f"{r['case']:<{width}}  {'PASS' if r['pass'] else 'FAIL'}  "
                           f"{sum(i['ok'] for i in r['checks'])}/{len(r['checks'])}  {r['seconds']}s" for r in rows))
    print(f"\nchaos: {'PASS' if ok else 'FAIL'} in {total:.0f}s -> {mp}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
