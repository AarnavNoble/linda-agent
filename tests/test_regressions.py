"""Regression tests pinned to specific FAILURES.md entries (run against a private throwaway portal)."""
from __future__ import annotations

import dataclasses
import json

import pytest
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from agent import config, runner as runner_mod
from agent.acquire import download_fax
from agent.browser import PortalSession

from .conftest import REPO

pytestmark = pytest.mark.e2e


def test_download_reauthenticates_after_session_expiry(portal, tmp_path):
    """FAILURES.md #3: an expired login during a download used to strand the tab on /login."""
    cfg = dataclasses.replace(config.load(), base_url=portal)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        s = PortalSession(browser, cfg, "A").start()
        try:
            s.goto_fax("FX-201")
            s.context.clear_cookies()  # indistinguishable from an expired session to the browser
            d = download_fax(s, "FX-201", 2, tmp_path, max_attempts=3, wait_ms=10000)
            assert d.pages == 2 and d.path.read_bytes().startswith(b"%PDF-")
            assert not s.on_login_page()
            assert s.logins >= 2  # initial login + the re-authentication
        finally:
            s.close()
            browser.close()


def test_transient_playwright_error_on_one_fax_does_not_sink_the_run(portal, tmp_path, monkeypatch):
    """FAILURES.md #7: one Playwright error used to abort the whole inbox with no results.json."""
    cfg = dataclasses.replace(config.load(), base_url=portal, work_dir=tmp_path, ocr_mode="replay",
                              cache_dir=REPO / "cache", headed=False, crash_at="")
    real = runner_mod.read_status
    calls = {"n": 0}

    def flaky(s, fax_id):
        if fax_id == "FX-203" and calls["n"] == 0:
            calls["n"] += 1
            raise PWTimeout("simulated: page did not load in time")
        return real(s, fax_id)

    monkeypatch.setattr(runner_mod, "read_status", flaky)
    rc = runner_mod.Runner(cfg).run()
    assert calls["n"] == 1, "the injected failure never fired"
    results = {r["fax_id"]: r for r in json.loads((tmp_path / "results" / "results.json").read_text())["results"]}
    assert rc == 0 and len(results) == 12
    assert results["FX-203"]["action"] == "routed"
    assert results["FX-203"]["verification"]["status"] == "ui_verified"


def test_bare_error_page_does_not_strand_the_session(portal):
    """Breaker P1-1: a page without portal navigation (e.g. a proxy 502) used to raise and sink the run."""
    cfg = dataclasses.replace(config.load(), base_url=portal)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        s = PortalSession(browser, cfg, "A").start()
        try:
            s.page.set_content("<html><body><h1>502 Bad Gateway</h1></body></html>")  # no sidebar
            s.goto_fax("FX-203")  # must recover by re-entering through the base URL
            s.assert_on_fax("FX-203")
        finally:
            s.close()
            browser.close()


def test_unreadable_original_holds_its_resend_instead_of_double_review(portal, tmp_path):
    """Breaker P1-3: with the original unreadable, its resend became a second Manual Review task."""
    import shutil
    cache = tmp_path / "cache"
    shutil.copytree(REPO / "cache", cache)
    for f in cache.glob("*.json"):
        if json.loads(f.read_text()).get("fax_id") == "FX-201":
            f.unlink()  # same effect as a vision outage for that one fax
    cfg = dataclasses.replace(config.load(), base_url=portal, work_dir=tmp_path / "w", ocr_mode="replay",
                              cache_dir=cache, headed=False, crash_at="")
    rc = runner_mod.Runner(cfg).run()
    res = {r["fax_id"]: r for r in json.loads((tmp_path / "w" / "results" / "results.json").read_text())["results"]}
    assert rc == 1  # completed_with_failures: something is genuinely unfinished
    assert res["FX-201"]["action"] == "failed" and res["FX-201"]["verification"]["status"] == "not_processed"
    assert res["FX-210"]["action"] == "failed", "resend must be held while its original is unreadable"
    assert res["FX-211"]["action"] == "already_routed"
    assert all(res[f]["action"] != "needs_review" or f in ("FX-202", "FX-208", "FX-209")
               for f in res), "no extra review tasks created for held faxes"
