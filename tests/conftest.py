"""Shared fixtures: a private, throwaway Linda Mini portal per test and an agent runner.

Every portal gets its own SQLite DB (fault state lives in the DB, so a fault fires once per
DB) and its own port in 8201-8240. Tests observe the portal only through the browser UI and
the agent's own artifacts (results.json / events.jsonl) -- never via __admin or the DB.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _find_portal_dir() -> Path:
    """LINDA_PORTAL_DIR, else the nearest ancestor-sibling `linda-mini/` (the sandbox's default layout)."""
    if os.getenv("LINDA_PORTAL_DIR"):
        return Path(os.environ["LINDA_PORTAL_DIR"])
    for parent in [REPO, *REPO.parents]:
        if (parent / "linda-mini" / "app.py").exists():
            return parent / "linda-mini"
    return REPO.parent / "linda-mini"


PORTAL_DIR = _find_portal_dir()
PORTAL_UVICORN = PORTAL_DIR / ".venv" / "bin" / "uvicorn"
PORT_RANGE = range(8201, 8241)  # never 8000: that is the user's interactive portal

# Expected outcome of a clean run under the documented SOP interpretation (see NOTES.md).
EXPECTED_ACTIONS = {
    "FX-201": "routed", "FX-203": "routed", "FX-204": "routed", "FX-205": "routed",
    "FX-206": "routed", "FX-207": "routed",
    "FX-202": "needs_review", "FX-208": "needs_review", "FX-209": "needs_review", "FX-212": "needs_review",
    "FX-210": "skipped_duplicate",
    "FX-211": "already_routed",
}
EXPECTED_DUPLICATE_OF = {"FX-210": "FX-201"}
REVIEW_ROUTE = {"assigned_to": "Jordan Ellis - Manual Review", "doc_type": "Needs Review",
                "folder": "Review / Unclassified"}
# FX-211 is pre-routed by clinic staff in the sandbox seed; the agent must leave it alone.
FX211_PREEXISTING = {"assigned_to": "Noah Patel - Clinical Review", "doc_type": "Imaging Report",
                     "folder": "Clinical / Imaging"}


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def _wait_ready(url: str, proc: subprocess.Popen, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url + "/login", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.2)
    return False


@dataclasses.dataclass
class Portal:
    base_url: str
    port: int
    fault_mode: str
    db_path: Path
    proc: subprocess.Popen
    log_path: Path

    def access_log(self) -> list[tuple[str, str, int]]:
        """(method, path, status) per request, from this portal process's own uvicorn access log."""
        rx = re.compile(r'"(GET|POST|PUT|DELETE|PATCH|HEAD) (\S+) HTTP/[\d.]+" (\d{3})')
        return [(m.group(1), m.group(2), int(m.group(3)))
                for m in rx.finditer(self.log_path.read_text())]


def _start_portal(tmp: Path, fault_mode: str) -> Portal:
    if not PORTAL_UVICORN.exists():
        pytest.skip(f"portal not available at {PORTAL_DIR} (set LINDA_PORTAL_DIR; run `make setup` to create its venv)")
    tmp.mkdir(parents=True, exist_ok=True)
    db = tmp / "portal.sqlite3"
    env = {**os.environ, "LINDA_DB_PATH": str(db), "LINDA_FAULT_MODE": fault_mode}
    # Ports are shared with other processes on this machine: probe, try, move on if taken.
    for port in PORT_RANGE:
        if not _port_free(port):
            continue
        log_path = tmp / f"portal-{port}.log"
        log = log_path.open("w")
        proc = subprocess.Popen([str(PORTAL_UVICORN), "app:app", "--host", "127.0.0.1", "--port", str(port)],
                                cwd=PORTAL_DIR, env=env, stdout=log, stderr=subprocess.STDOUT)
        url = f"http://127.0.0.1:{port}"
        if _wait_ready(url, proc):
            return Portal(url, port, fault_mode, db, proc, log_path)
        proc.kill()
        proc.wait()
    raise RuntimeError("no free port in 8201-8240 for a test portal")


def _stop_portal(p: Portal) -> None:
    p.proc.terminate()
    try:
        p.proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.proc.kill()
        p.proc.wait()


@pytest.fixture
def portal_factory(tmp_path_factory):
    """Call with a fault mode; returns a fresh Portal. All are torn down after the test."""
    started: list[Portal] = []

    def make(fault_mode: str = "none") -> Portal:
        p = _start_portal(tmp_path_factory.mktemp(f"portal-{fault_mode}"), fault_mode)
        started.append(p)
        return p

    yield make
    for p in started:
        _stop_portal(p)


@pytest.fixture
def portal(portal_factory):
    """A fresh fault-free portal; yields its base_url."""
    yield portal_factory("none").base_url


# ---- agent runs -------------------------------------------------------------

@dataclasses.dataclass
class AgentRun:
    returncode: int
    work_dir: Path
    stdout: str
    stderr: str

    @property
    def results_doc(self) -> dict:
        return json.loads((self.work_dir / "results" / "results.json").read_text())

    @property
    def results(self) -> dict[str, dict]:
        return {r["fax_id"]: r for r in self.results_doc["results"]}

    def events(self) -> list[dict]:
        out = []
        for f in sorted((self.work_dir / "runs").glob("*/events.jsonl")):
            out += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        return out


def _agent_cmd() -> list[str]:
    uv = shutil.which("uv")
    return [uv, "run", "--frozen", "python", "-m", "agent", "run"] if uv else [sys.executable, "-m", "agent", "run"]


def run_agent(base_url: str, work_dir: Path, extra_env: dict | None = None, timeout: int = 600) -> AgentRun:
    work_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "LINDA_BASE_URL": base_url, "LINDA_WORK_DIR": str(work_dir),
           "LINDA_OCR_MODE": "replay", "LINDA_CACHE_DIR": str(REPO / "cache"), "LINDA_HEADED": "0"}
    env.pop("ANTHROPIC_API_KEY", None)  # replay must not need (or use) a key
    env.update(extra_env or {})
    p = subprocess.run(_agent_cmd(), cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout)
    (work_dir / "agent.stderr.log").write_text(p.stderr)
    return AgentRun(p.returncode, work_dir, p.stdout, p.stderr)


@pytest.fixture
def agent_runner(tmp_path_factory):
    def go(base_url: str, **kw) -> AgentRun:
        return run_agent(base_url, tmp_path_factory.mktemp("agent-work"), **kw)
    return go


# ---- read-only UI observer --------------------------------------------------

class UIObserver:
    """Reads portal state through the browser UI with the agent's own read-only PortalSession."""

    def __init__(self, base_url: str):
        from playwright.sync_api import sync_playwright

        from agent import config
        self.cfg = dataclasses.replace(config.load(), base_url=base_url, headed=False)
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=True)

    def session(self, label: str = "T"):
        from agent.browser import PortalSession
        return PortalSession(self.browser, self.cfg, label).start()

    def snapshot(self, fax_ids) -> dict[str, dict]:
        """Status + routing panel + Routed Documents row for each fax, from one fresh session."""
        s = self.session("observer")
        try:
            out = {}
            for fid in fax_ids:
                s.goto_fax(fid)
                info = s.transmission_info()
                panel = s.routing_panel()
                out[fid] = {"status": info.get("Status"), "routed": panel.routed,
                            "panel": {"assigned_to": panel.assigned_to, "doc_type": panel.doc_type,
                                      "folder": panel.folder}}
            for fid in fax_ids:
                out[fid]["queue_row"] = s.routed_documents_row(fid)
            out["_queue_rows"] = _all_queue_rows(s)
            return out
        finally:
            s.close()

    def close(self):
        self.browser.close()
        self._pw.stop()


def _all_queue_rows(s) -> list[list[str]]:
    s._click_nav("Routed Documents")
    rows = []
    for tr in s.page.locator("table[aria-label='Routed documents'] tbody tr").all():
        rows.append([c.inner_text().strip() for c in tr.locator("td").all()])
    return rows


@pytest.fixture
def ui_observer():
    observers: list[UIObserver] = []

    def make(base_url: str) -> UIObserver:
        o = UIObserver(base_url)
        observers.append(o)
        return o

    yield make
    for o in observers:
        o.close()
