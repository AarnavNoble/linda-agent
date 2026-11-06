"""Routing SOP (assignment §4). Exact portal dropdown labels — do not paraphrase."""
from __future__ import annotations

from typing import NamedTuple


class Route(NamedTuple):
    assigned_to: str
    doc_type: str
    folder: str


CATEGORIES = (
    "referral",
    "lab_result",
    "imaging_report",
    "prior_authorization",
    "refill_request",
    "records_request",
    "eob",
)

SOP: dict[str, Route] = {
    "referral": Route("Olivia Chen - Referrals", "Referral", "Referrals / New"),
    "lab_result": Route("Noah Patel - Clinical Review", "Lab Result", "Clinical / Labs"),
    "imaging_report": Route("Noah Patel - Clinical Review", "Imaging Report", "Clinical / Imaging"),
    "prior_authorization": Route("Ethan Brooks - Authorizations", "Prior Authorization", "Authorizations / Pending"),
    "refill_request": Route("Ava Wilson - Nursing / Medication", "Refill Request", "Nursing / Medication"),
    "records_request": Route("Mia Gomez - Medical Records", "Records Request", "Records / Requests"),
    "eob": Route("Liam Rivera - Billing", "EOB", "Billing / EOBs"),
}

NEEDS_REVIEW = Route("Jordan Ellis - Manual Review", "Needs Review", "Review / Unclassified")

# Result actions (README "useful result" enum).
ROUTED = "routed"
NEEDS_REVIEW_ACTION = "needs_review"
SKIPPED_DUPLICATE = "skipped_duplicate"
ALREADY_ROUTED = "already_routed"
FAILED = "failed"
ACTIONS = (ROUTED, NEEDS_REVIEW_ACTION, SKIPPED_DUPLICATE, ALREADY_ROUTED, FAILED)
