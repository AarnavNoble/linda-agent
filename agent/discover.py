"""Enumerate the full inbox by paging through it with the visible Next button."""
from __future__ import annotations

from .browser import PortalError, PortalSession


def discover_inbox(s: PortalSession) -> list[dict]:
    s.open_inbox()
    seen: dict[str, dict] = {}
    _, last, total = s.inbox_summary()
    for _ in range(last + 2):  # bounded: never loop forever on a misbehaving pager
        for row in s.inbox_rows():
            seen.setdefault(row["fax_id"], row)
        if not s.inbox_next():
            break
    if len(seen) != total:
        raise PortalError(f"inbox reports {total} transmissions but {len(seen)} unique IDs were read")
    return list(seen.values())
