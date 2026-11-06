"""Download a fax PDF through the 'Download Fax PDF' button, robust to transient failures.

A real download is an attachment: the page stays put and Playwright emits a `download`
event. A transient failure is an HTML error page: the tab *navigates* to /download and
no download event fires. We watch for both instead of waiting out a 30s timeout, and we
never trust bytes until they validate as a PDF with the page count the portal reports.
"""
from __future__ import annotations

import hashlib
import io
import os
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Error as PWError
from pypdf import PdfReader

from .browser import PortalError, PortalSession


class DownloadFailed(PortalError):
    pass


@dataclass
class Downloaded:
    path: Path
    sha256: str
    pages: int
    attempts: int


def validate_pdf(data: bytes, expected_pages: int | None) -> int:
    """Return page count, or raise ValueError describing why these bytes are not the fax."""
    if not data.startswith(b"%PDF-"):
        head = data[:80].decode("latin-1", "replace").replace("\n", " ")
        raise ValueError(f"not a PDF (starts with {head!r})")
    try:
        n = len(PdfReader(io.BytesIO(data)).pages)
    except Exception as e:  # truncated / corrupt
        raise ValueError(f"unparseable PDF: {e}") from e
    if expected_pages is not None and n != expected_pages:
        raise ValueError(f"page count {n} != portal-reported {expected_pages}")
    return n


def _trigger_download(s: PortalSession, fax_id: str, wait_ms: int):
    """Click download; return ('ok', Download) | ('error_page', text) | ('login', None) | ('timeout', None)."""
    page = s.page
    got = []

    def handler(d):  # Playwright needs a real function (it tags it); list.append fails
        got.append(d)

    page.on("download", handler)
    try:
        page.locator("#download-fax").click(no_wait_after=True)
        deadline = time.monotonic() + wait_ms / 1000
        while time.monotonic() < deadline:
            if got:
                return "ok", got[0]
            url = page.url
            if "/login" in url:
                return "login", None
            if url.rstrip("/").endswith(f"/faxes/{fax_id}/download"):
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=3000)
                    text = page.locator("body").inner_text(timeout=3000)[:400]
                except Exception:
                    text = "(unreadable error page)"
                return "error_page", text
            page.wait_for_timeout(100)
        return "timeout", None
    finally:
        page.remove_listener("download", handler)


def download_fax(s: PortalSession, fax_id: str, expected_pages: int, dest_dir: Path,
                 max_attempts: int, wait_ms: int, on_attempt=None) -> Downloaded:
    dest_dir.mkdir(parents=True, exist_ok=True)
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        try:
            s.goto_fax(fax_id)  # (re)establish we are on the right fax every attempt
        except (PortalError, PWError) as e:  # transient page failure: count it as an attempt and retry
            last_err = f"could not open fax page: {e}"
            if on_attempt: on_attempt("download", "nav_error", last_err)
            time.sleep(min(0.5 * 2 ** (attempt - 1), 4))
            continue
        kind, payload = _trigger_download(s, fax_id, wait_ms)
        if kind == "ok":
            dl = payload
            if dl.suggested_filename != f"{fax_id}.pdf":
                # Matching invariant, Session A side: the file served must be for this fax ID.
                raise DownloadFailed(f"served file {dl.suggested_filename!r} is not {fax_id}.pdf")
            tmp = dest_dir / f".{fax_id}.{os.getpid()}.part"
            dl.save_as(str(tmp))  # must happen before the context closes
            data = tmp.read_bytes()
            try:
                n = validate_pdf(data, expected_pages)
            except ValueError as e:
                tmp.unlink(missing_ok=True)
                last_err = f"invalid payload: {e}"
                if on_attempt: on_attempt("download", "invalid_pdf", last_err)
            else:
                sha = hashlib.sha256(data).hexdigest()
                final = dest_dir / f"{fax_id}-{sha[:12]}.pdf"
                with tmp.open("rb+") as fh:
                    os.fsync(fh.fileno())
                os.replace(tmp, final)
                if on_attempt: on_attempt("download", "ok", f"sha256={sha[:12]} pages={n}")
                return Downloaded(final, sha, n, attempt)
        elif kind == "error_page":
            last_err = f"portal error page instead of PDF: {payload!r}"
            if on_attempt: on_attempt("download", "error_page", last_err)
        elif kind == "login":
            s.ensure_auth()
            last_err = "session expired during download; re-authenticated"
            if on_attempt: on_attempt("download", "reauth", last_err)
        else:
            last_err = f"no download within {wait_ms}ms"
            if on_attempt: on_attempt("download", "timeout", last_err)
        time.sleep(min(0.5 * 2 ** (attempt - 1), 4))
    raise DownloadFailed(f"{fax_id}: gave up after {max_attempts} attempts: {last_err}")
