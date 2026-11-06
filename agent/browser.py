"""Portal UI driver. All portal interaction goes through visible UI elements.

`page.goto` is used for exactly one thing: opening the portal's base URL to start a
session (tests/test_boundaries.py enforces this). Every other navigation is a click.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.sync_api import Browser, BrowserContext, Page

from .config import Config


class PortalError(RuntimeError):
    pass


class InvariantViolation(PortalError):
    """The page we are on is not the fax we intended to act on."""


@dataclass
class RoutingPanel:
    routed: bool
    assigned_to: str | None = None
    doc_type: str | None = None
    folder: str | None = None


class PortalSession:
    """One isolated, authenticated browser context (Session A or Session B)."""

    def __init__(self, browser: Browser, cfg: Config, label: str, trace_path=None):
        self.cfg = cfg
        self.label = label
        self.trace_path = trace_path
        self.context: BrowserContext = browser.new_context(accept_downloads=True)
        self.context.set_default_timeout(cfg.nav_timeout_ms)
        if trace_path:
            self.context.tracing.start(screenshots=True, snapshots=True)
        self.page: Page = self.context.new_page()
        self.session_prefix: str | None = None
        self.logins = 0

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> "PortalSession":
        self.page.goto(self.cfg.base_url + "/")  # the only goto: entry point
        self.ensure_auth()
        return self

    def close(self) -> None:
        try:
            if self.trace_path:
                self.trace_path.parent.mkdir(parents=True, exist_ok=True)
                self.context.tracing.stop(path=str(self.trace_path))
        finally:
            self.context.close()

    def screenshot(self, path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path), full_page=True)
        except Exception:
            pass

    # ---- auth --------------------------------------------------------------
    def on_login_page(self) -> bool:
        return "/login" in self.page.url

    def ensure_auth(self) -> bool:
        """If the portal bounced us to /login, sign in again. Returns True if a re-login happened."""
        if not self.on_login_page():
            return False
        self.page.fill("#username", self.cfg.username)
        self.page.fill("#password", self.cfg.password)
        self.page.click("form[action='/login'] button[type=submit]")
        self.page.wait_for_load_state("domcontentloaded")
        if self.on_login_page():
            if "error=" in self.page.url:
                raise PortalError("login failed: still on /login (bad credentials)")
            # Login succeeded, but the portal's post-login redirect (`next`) was a file download:
            # an attachment never navigates, so the tab is stranded on /login. Re-enter through the
            # portal's entry point (the same one start() uses); callers then navigate by clicking.
            if not any(c["name"] == "linda_sid" for c in self.context.cookies()):
                raise PortalError("login failed: still on /login and no session was issued")
            self.page.wait_for_timeout(300)  # let the attachment response settle
            self.start()
            if self.on_login_page():
                raise PortalError("login failed: re-entry after login still lands on /login")
            self.logins += 1
            self.session_prefix = self._read_session_prefix()
            return True
        self.logins += 1
        self.session_prefix = self._read_session_prefix()
        return True

    def _read_session_prefix(self) -> str | None:
        txt = self.page.locator(".userbar").inner_text()
        m = re.search(r"Session\s+(\S+)", txt)
        return m.group(1) if m else None

    # ---- navigation (clicks only) -----------------------------------------
    def _click_nav(self, name: str) -> None:
        self.ensure_auth()
        if self.page.locator(".side").count() == 0:
            # A bare error page (e.g. a proxy 502) has no portal navigation. Re-enter through the
            # portal's entry URL (same as start(); session cookie kept) instead of giving up.
            self.start()
            if self.page.locator(".side").count() == 0:
                raise PortalError(f"no portal navigation even after re-entry ({self.page.url}); cannot open {name}")
        self.page.locator("a.nav", has_text=re.compile(rf"^{re.escape(name)}$")).click()
        self.page.wait_for_load_state("domcontentloaded")
        if self.ensure_auth():  # session expired between pages; the portal returns us to `next`
            self.page.wait_for_load_state("domcontentloaded")

    def open_inbox(self) -> None:
        """Visible-UI path required by the brief: Fax Management -> Inbound Faxes."""
        self._click_nav("Fax Management")
        self.page.get_by_role("link", name="Go to Inbound Faxes").click()
        self.page.wait_for_load_state("domcontentloaded")
        self.ensure_auth()
        self._expect_h1("Inbound Faxes")

    def open_inbox_quick(self) -> None:
        self._click_nav("Inbound Faxes")
        self._expect_h1("Inbound Faxes")

    def inbox_rows(self) -> list[dict]:
        rows = []
        for tr in self.page.locator("table[aria-label='Inbound faxes'] tbody tr").all():
            cells = [c.inner_text().strip() for c in tr.locator("td").all()]
            if len(cells) < 5:
                continue
            rows.append({"fax_id": cells[0], "received": cells[1], "sender": cells[2],
                         "pages": int(cells[3]), "status": cells[4]})
        return rows

    def inbox_summary(self) -> tuple[int, int, int]:
        """(page, last_page, total) from 'Page 1 of 2 · 12 transmissions'."""
        txt = self.page.locator(".pagination .small").inner_text()
        m = re.search(r"Page\s+(\d+)\s+of\s+(\d+)\s+·\s+(\d+)\s+transmissions", txt)
        if not m:
            raise PortalError(f"unparseable pagination: {txt!r}")
        return int(m.group(1)), int(m.group(2)), int(m.group(3))

    def inbox_next(self) -> bool:
        nxt = self.page.locator(".pagination a", has_text=re.compile(r"^Next$"))
        if nxt.count() == 0:
            return False
        nxt.click()
        self.page.wait_for_load_state("domcontentloaded")
        self.ensure_auth()
        return True

    def goto_fax(self, fax_id: str) -> None:
        """Locate the exact fax ID through Inbound Faxes search and open it.

        Search is a substring match on ID *and* sender, so we click only the link whose text
        equals the ID, then assert the detail page shows that same ID (matching invariant).
        """
        for attempt in range(2):
            self.open_inbox_quick()
            self.page.fill("#search", fax_id)
            self.page.locator("form.controls button[type=submit]").click()
            self.page.wait_for_load_state("domcontentloaded")
            if self.ensure_auth():
                continue  # bounced through login; redo the search from a clean inbox
            # Search is a substring match, so other IDs that contain this one (FX-205 -> FX-2050...)
            # can push the exact match onto a later results page: page through until it appears.
            link = None
            for _ in range(50):
                cand = self.page.locator("table[aria-label='Inbound faxes'] tbody tr td:first-child a").filter(
                    has_text=re.compile(rf"^{re.escape(fax_id)}$"))
                if cand.count() > 1:
                    raise PortalError(f"expected exactly one inbox link for {fax_id}, found {cand.count()}")
                if cand.count() == 1:
                    link = cand
                    break
                if not self.inbox_next():
                    break
            if link is None:
                raise PortalError(f"no inbox row with fax ID exactly {fax_id}")
            link.click()
            self.page.wait_for_load_state("domcontentloaded")
            if self.ensure_auth():
                continue
            self.assert_on_fax(fax_id)
            return
        raise PortalError(f"could not open {fax_id} after re-authentication")

    def assert_on_fax(self, fax_id: str) -> None:
        h1 = self.page.locator("main h1").first.inner_text().strip()
        shown = self.transmission_info().get("Fax ID")
        if h1 != f"Fax {fax_id}" or shown != fax_id:
            raise InvariantViolation(f"wanted {fax_id}, page shows h1={h1!r} id={shown!r}")

    def _expect_h1(self, text: str) -> None:
        h1 = self.page.locator("main h1").first.inner_text().strip()
        if h1 != text:
            raise PortalError(f"expected page {text!r}, got {h1!r} at {self.page.url}")

    # ---- detail page readers (scoped: the page has two dl.kv blocks) ------
    def _kv(self, card_heading: str) -> dict[str, str]:
        card = self.page.locator(".card").filter(
            has=self.page.locator("h2", has_text=re.compile(rf"^{re.escape(card_heading)}$")))
        if card.count() != 1:
            return {}
        out = {}
        dts = card.locator("dl.kv dt").all()
        dds = card.locator("dl.kv dd").all()
        for dt, dd in zip(dts, dds):
            out[dt.inner_text().strip()] = dd.inner_text().strip()
        return out

    def transmission_info(self) -> dict[str, str]:
        return self._kv("Transmission information")

    def routing_panel(self) -> RoutingPanel:
        kv = self._kv("Document routing")
        if kv.get("Routing status") == "Completed":
            return RoutingPanel(True, kv.get("Assigned To"), kv.get("Document Type"), kv.get("Document Folder"))
        return RoutingPanel(False)

    def flash_notices(self) -> list[str]:
        return [n.inner_text().strip() for n in self.page.locator("main .notice[role=alert]").all()]

    def routed_documents_row(self, fax_id: str) -> dict | None:
        self._click_nav("Routed Documents")
        self._expect_h1("Routed Documents")
        for tr in self.page.locator("table[aria-label='Routed documents'] tbody tr").all():
            cells = [c.inner_text().strip() for c in tr.locator("td").all()]
            if cells and cells[0] == fax_id:
                return {"fax_id": cells[0], "doc_type": cells[1], "assigned_to": cells[2],
                        "folder": cells[3], "status": cells[4]}
        return None
