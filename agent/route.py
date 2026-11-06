"""Submit a routing decision through the UI and verify it from persisted UI state.

A submit click, an HTTP status, or a flash message is never treated as confirmation.
The only thing that confirms a route is reading it back: the fax's Document routing panel
and the Routed Documents work queue, both matching the intended values.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.sync_api import TimeoutError as PWTimeout

from .browser import PortalError, PortalSession
from .sop import Route


@dataclass
class SubmitOutcome:
    kind: str           # success | already | expired | two_session | csrf | invalid | unknown
    http_status: int | None
    final_url: str
    detail: str = ""


@dataclass
class Verification:
    routed: bool
    matches: bool
    panel: dict
    queue_row: dict | None
    detail: str


def fill_and_review(s: PortalSession, fax_id: str, route: Route, note: str) -> None:
    """On the fax detail page: open the route form, fill all fields by visible label, confirm in modal."""
    s.page.get_by_role("link", name="Route / Assign Fax").click()
    s.page.wait_for_load_state("domcontentloaded")
    if s.on_login_page():
        raise PortalError("session expired opening route form")
    h1 = s.page.locator("main h1").first.inner_text().strip()
    if h1 != f"Route fax {fax_id}":
        raise PortalError(f"expected route form for {fax_id}, got {h1!r}")

    for sel, label in (("#assignee", route.assigned_to), ("#doc_type", route.doc_type), ("#folder", route.folder)):
        s.page.select_option(sel, label=label)
        shown = s.page.eval_on_selector(sel, "el => el.selectedOptions[0] ? el.selectedOptions[0].text : ''")
        if shown != label:
            raise PortalError(f"{sel} shows {shown!r} after selecting {label!r}")
    s.page.fill("#notes", note)

    s.page.get_by_role("button", name="Review Routing").click()
    try:
        s.page.wait_for_selector("#confirmation.show", state="visible", timeout=5000)
    except PWTimeout as e:
        empty = s.page.evaluate("""() => [...document.querySelectorAll('#route-form [required]')]
                                   .filter(el => !el.value).map(el => el.id)""")
        raise PortalError(f"confirmation modal did not open; empty required fields: {empty}") from e
    summary = {
        "assigned_to": s.page.inner_text("#summary-assignee").strip(),
        "doc_type": s.page.inner_text("#summary-type").strip(),
        "folder": s.page.inner_text("#summary-folder").strip(),
    }
    if summary != route._asdict():
        raise PortalError(f"confirmation modal shows {summary}, intended {route._asdict()}")


def click_submit(s: PortalSession, fax_id: str, timeout_ms: int) -> SubmitOutcome:
    """Press 'OK - Submit Routing' and classify what came back. Does NOT imply success."""
    page = s.page
    path = f"/faxes/{fax_id}/route"
    status = None
    try:
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(path),
                                  timeout=timeout_ms) as info:
            page.locator("#confirm-ok").click()
        status = info.value.status
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except PWTimeout:
        return SubmitOutcome("unknown", status, page.url, "no response / page load timed out")
    url = page.url
    if status == 303 or status == 200:
        if "notice_type=success" in url:
            return SubmitOutcome("success", status, url)
        if "notice_type=already" in url:
            return SubmitOutcome("already", status, url)
        if "/login" in url:
            return SubmitOutcome("expired", status, url, "redirected to login: session expired during submit")
    kind = {409: "two_session", 403: "csrf", 400: "invalid"}.get(status, "unknown")
    try:
        detail = " ".join(s.flash_notices())[:300]
    except Exception:
        detail = ""
    return SubmitOutcome(kind, status, url, detail)


def verify(s: PortalSession, fax_id: str, intended: Route) -> Verification:
    """Re-read persisted state from the UI: fax detail routing panel + Routed Documents row."""
    s.goto_fax(fax_id)
    panel = s.routing_panel()
    panel_d = {"assigned_to": panel.assigned_to, "doc_type": panel.doc_type, "folder": panel.folder}
    row = s.routed_documents_row(fax_id)
    if not panel.routed:
        return Verification(False, False, panel_d, row,
                            "fax still shows New" + ("; but a work-queue row exists?!" if row else ""))
    want = intended._asdict()
    row_d = {k: row[k] for k in want} if row else None
    matches = panel_d == want and row_d == want
    detail = "routing panel and Routed Documents both match intent" if matches else (
        f"persisted route differs from intent: panel={panel_d} queue={row_d} intended={want}")
    return Verification(True, matches, panel_d, row, detail)


def read_status(s: PortalSession, fax_id: str) -> tuple[str, dict]:
    s.goto_fax(fax_id)
    info = s.transmission_info()
    panel = s.routing_panel()
    return info.get("Status", "?"), {"assigned_to": panel.assigned_to, "doc_type": panel.doc_type,
                                     "folder": panel.folder} if panel.routed else {}
