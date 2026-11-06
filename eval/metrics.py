"""Pure scoring functions: agent results vs. human labels. No I/O, so they are unit-testable.

Terminology
  label action   what the human says should have happened (routed / needs_review /
                 skipped_duplicate / already_routed)
  agent action   results.json `action` (same enum plus `failed`)

Safety metrics (the ones that matter for a fax router):
  unsafe_auto_routes   label says the fax must NOT be auto-filed (review, duplicate, already routed)
                       but the agent auto-routed it to a work queue. Headline metric; target 0.
  misroutes            label and agent both say `routed`, but to a different destination.
  extra_submissions    label says do not submit (duplicate / already routed) but the agent submitted
                       any route at all (creates a second routing record).
  over_review_rate     label says auto-route, agent sent it to Manual Review. The counterweight:
                       an agent that reviews everything is "safe" but useless.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from statistics import median

ACTIONS = ("routed", "needs_review", "skipped_duplicate", "already_routed", "failed")
LABEL_ACTIONS = ACTIONS[:4]
SUBMITTING = ("routed", "needs_review")
NO_AUTO_ROUTE = ("needs_review", "skipped_duplicate", "already_routed")
CATEGORIES = ("referral", "lab_result", "imaging_report", "prior_authorization", "refill_request",
              "records_request", "eob")
LABEL_CATEGORIES = CATEGORIES + ("mixed", "unclear")
ROUTE_KEYS = ("assigned_to", "doc_type", "folder")

# Transcribed independently from the brief's SOP table (not imported from agent/sop.py), so a typo in
# the agent's table shows up as a route mismatch instead of being silently shared by agent and oracle.
ORACLE_SOP = {
    "referral": ("Olivia Chen - Referrals", "Referral", "Referrals / New"),
    "lab_result": ("Noah Patel - Clinical Review", "Lab Result", "Clinical / Labs"),
    "imaging_report": ("Noah Patel - Clinical Review", "Imaging Report", "Clinical / Imaging"),
    "prior_authorization": ("Ethan Brooks - Authorizations", "Prior Authorization", "Authorizations / Pending"),
    "refill_request": ("Ava Wilson - Nursing / Medication", "Refill Request", "Nursing / Medication"),
    "records_request": ("Mia Gomez - Medical Records", "Records Request", "Records / Requests"),
    "eob": ("Liam Rivera - Billing", "EOB", "Billing / EOBs"),
}
ORACLE_REVIEW = ("Jordan Ellis - Manual Review", "Needs Review", "Review / Unclassified")


class LabelError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------
def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def resolve_route(label: dict) -> tuple[str, str, str] | None:
    """Expected submitted route as a tuple, or None when nothing should be submitted.
    `expected_route: auto` derives it from the oracle SOP table above."""
    action, route = label["expected_action"], label["expected_route"]
    if isinstance(route, str) and route.strip().lower() == "none":
        return None
    if isinstance(route, str) and route.strip().lower() == "auto":
        if action == "needs_review":
            return ORACLE_REVIEW
        if action == "routed":
            return ORACLE_SOP[label["expected_category"]]
        return None
    return tuple(str(route[k]).strip() for k in ROUTE_KEYS)


def validate_labels(labels: dict) -> list[str]:
    """Return a list of problems; empty means the labels are complete and well-formed."""
    problems: list[str] = []
    faxes = labels.get("faxes") if isinstance(labels, dict) else None
    if not isinstance(faxes, dict) or not faxes:
        return ["labels file has no `faxes:` mapping"]
    valid_route_values = {v for row in list(ORACLE_SOP.values()) + [ORACLE_REVIEW] for v in row}
    for fid, lab in faxes.items():
        if not isinstance(lab, dict):
            problems.append(f"{fid}: entry is not a mapping")
            continue
        a, c, r = lab.get("expected_action"), lab.get("expected_category"), lab.get("expected_route")
        if _blank(a):
            problems.append(f"{fid}: expected_action is blank")
        elif a not in LABEL_ACTIONS:
            problems.append(f"{fid}: expected_action {a!r} not in {LABEL_ACTIONS}")
        if _blank(c):
            problems.append(f"{fid}: expected_category is blank")
        elif c not in LABEL_CATEGORIES:
            problems.append(f"{fid}: expected_category {c!r} not in {LABEL_CATEGORIES}")
        if a == "skipped_duplicate" and _blank(lab.get("expected_duplicate_of")):
            problems.append(f"{fid}: skipped_duplicate needs expected_duplicate_of")
        if _blank(r):
            problems.append(f"{fid}: expected_route is blank (use `auto`, `none`, or the three fields)")
        elif isinstance(r, str):
            if r.strip().lower() not in ("auto", "none"):
                problems.append(f"{fid}: expected_route {r!r} must be `auto`, `none`, or a mapping")
            elif r.strip().lower() == "auto" and a == "routed" and c not in CATEGORIES:
                problems.append(f"{fid}: `auto` route for action routed needs a concrete category, got {c!r}")
        elif isinstance(r, dict):
            for k in ROUTE_KEYS:
                if _blank(r.get(k)):
                    problems.append(f"{fid}: expected_route.{k} is blank")
                elif str(r[k]).strip() not in valid_route_values:
                    problems.append(f"{fid}: expected_route.{k} {r[k]!r} is not an exact portal dropdown value")
        else:
            problems.append(f"{fid}: expected_route has unsupported type {type(r).__name__}")
        if a in ("skipped_duplicate", "already_routed") and isinstance(r, dict):
            problems.append(f"{fid}: {a} means no route is submitted; set expected_route: none")
    return problems


# ---------------------------------------------------------------------------
# Agent predictions
# ---------------------------------------------------------------------------
def predicted_category(res: dict) -> str:
    """Map an agent result onto the label category space (7 SOP categories + mixed + unclear)."""
    reasons = " ".join(res.get("reasons") or [])
    if res.get("action") == "needs_review" and reasons.startswith("Mixed packet"):
        return "mixed"
    t = res.get("extracted_type")
    return t if t in CATEGORIES else "unclear"


def submitted_route(res: dict) -> tuple[str, str, str] | None:
    if res.get("action") not in SUBMITTING:
        return None
    sel = res.get("selected_fields") or {}
    return tuple(sel.get(k) for k in ROUTE_KEYS)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
@dataclass
class FaxScore:
    fax_id: str
    label_action: str
    agent_action: str
    label_category: str
    agent_category: str
    expected_route: tuple | None
    agent_route: tuple | None
    expected_dup_of: str | None
    agent_dup_of: str | None
    exact: bool
    flags: list[str] = field(default_factory=list)  # unsafe_auto_route, misroute, extra_submission, over_review


@dataclass
class Scores:
    n: int
    per_fax: list[FaxScore]
    exact_route_match: int
    action_accuracy: int
    classification_correct: int
    unsafe_auto_routes: list[str]
    misroutes: list[str]
    extra_submissions: list[str]
    over_review: list[str]
    label_routed: int
    failed: list[str]
    dup_tp: int
    dup_fp: int
    dup_fn: int
    dup_wrong_original: list[str]
    confusion: dict[str, Counter]
    missing_results: list[str]
    unlabeled_results: list[str]

    @property
    def over_review_rate(self) -> float | None:
        return len(self.over_review) / self.label_routed if self.label_routed else None

    @property
    def dup_precision(self) -> float | None:
        d = self.dup_tp + self.dup_fp
        return self.dup_tp / d if d else None

    @property
    def dup_recall(self) -> float | None:
        d = self.dup_tp + self.dup_fn
        return self.dup_tp / d if d else None


def score(results: list[dict], labels: dict) -> Scores:
    faxes = labels["faxes"]
    by_id = {r["fax_id"]: r for r in results}
    missing = sorted(set(faxes) - set(by_id))
    unlabeled = sorted(set(by_id) - set(faxes))
    per: list[FaxScore] = []
    confusion: dict[str, Counter] = {a: Counter() for a in LABEL_ACTIONS}
    unsafe, misroute, extra, over, failed, wrong_orig = [], [], [], [], [], []
    tp = fp = fn = 0

    for fid in sorted(faxes):
        lab = faxes[fid]
        res = by_id.get(fid) or {"fax_id": fid, "action": "failed", "reasons": ["no result for this fax"]}
        la, aa = lab["expected_action"], res.get("action") or "failed"
        exp_route, ag_route = resolve_route(lab), submitted_route(res)
        exp_dup = (lab.get("expected_duplicate_of") or None) if la == "skipped_duplicate" else None
        ag_dup = res.get("duplicate_of") if aa == "skipped_duplicate" else None
        exact = la == aa and exp_route == ag_route and exp_dup == ag_dup
        fs = FaxScore(fid, la, aa, lab["expected_category"], predicted_category(res), exp_route, ag_route,
                      exp_dup, ag_dup, exact)
        confusion[la][aa] += 1

        if la in NO_AUTO_ROUTE and aa == "routed":
            unsafe.append(fid); fs.flags.append("unsafe_auto_route")
        if la == "routed" and aa == "routed" and exp_route != ag_route:
            misroute.append(fid); fs.flags.append("misroute")
        if la in ("skipped_duplicate", "already_routed") and aa in SUBMITTING:
            extra.append(fid); fs.flags.append("extra_submission")
        if la == "routed" and aa == "needs_review":
            over.append(fid); fs.flags.append("over_review")
        if aa == "failed":
            failed.append(fid); fs.flags.append("failed")

        if aa == "skipped_duplicate" and la == "skipped_duplicate":
            tp += 1
            if exp_dup != ag_dup:
                wrong_orig.append(fid)
        elif aa == "skipped_duplicate":
            fp += 1
        elif la == "skipped_duplicate":
            fn += 1
        per.append(fs)

    return Scores(
        n=len(per), per_fax=per,
        exact_route_match=sum(f.exact for f in per),
        action_accuracy=sum(f.label_action == f.agent_action for f in per),
        classification_correct=sum(f.label_category == f.agent_category for f in per),
        unsafe_auto_routes=unsafe, misroutes=misroute, extra_submissions=extra, over_review=over,
        label_routed=sum(f.label_action == "routed" for f in per), failed=failed,
        dup_tp=tp, dup_fp=fp, dup_fn=fn, dup_wrong_original=wrong_orig,
        confusion=confusion, missing_results=missing, unlabeled_results=unlabeled,
    )


# ---------------------------------------------------------------------------
# Latency / cost (from events.jsonl + results.json; independent of labels)
# ---------------------------------------------------------------------------
def stage_latencies(events: list[dict], results: list[dict]) -> dict[str, dict[str, float]]:
    """Per-fax milliseconds for each stage.

    download   `downloaded.ms` (includes any 503 retries)
    extract    `extracted.ms` (local work: cache read or vision call + Tesseract cross-check)
    vision     live Claude call latency recorded in the cache meta when the extraction was produced
    route      Session B open + login + navigate + fill + submit: intent_written (or first B session)
               -> last submit_* event
    verify     last submit_* -> fax_done (UI re-read of routing panel + Routed Documents);
               for non-submitting faxes, V session start -> fax_done
    """
    out: dict[str, dict[str, float]] = {}
    per: dict[str, list[dict]] = {}
    for e in events:
        if e.get("fax_id"):
            per.setdefault(e["fax_id"], []).append(e)
    for fid, evs in per.items():
        d: dict[str, float] = {}
        for e in evs:
            if e["event"] == "downloaded" and "ms" in e:
                d["download"] = d.get("download", 0) + e["ms"]
            elif e["event"] == "extracted" and "ms" in e:
                d["extract"] = e["ms"]
        start = next((e["ts"] for e in evs if e["event"] == "intent_written"), None)
        if start is None:
            start = next((e["ts"] for e in evs if e["event"] == "session_started" and e.get("label") == "B"), None)
        submits = [e["ts"] for e in evs if e["event"].startswith("submit_")]
        done = next((e["ts"] for e in reversed(evs) if e["event"] == "fax_done"), None)
        if start is not None and submits:
            d["route"] = (submits[-1] - start) * 1000
            if done is not None:
                d["verify"] = (done - submits[-1]) * 1000
        elif done is not None:
            v = next((e["ts"] for e in evs if e["event"] == "session_started" and e.get("label") == "V"), None)
            if v is not None:
                d["verify"] = (done - v) * 1000
        out[fid] = d
    for r in results:
        ms = (r.get("extraction") or {}).get("vision_latency_ms")
        if ms is not None:
            out.setdefault(r["fax_id"], {})["vision"] = ms
    return out


def summarize(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"n": 0, "p50": None, "max": None}
    return {"n": len(values), "p50": median(values), "max": max(values)}


def cost_table(results: list[dict]) -> tuple[list[dict], dict]:
    rows = []
    for r in results:
        ex = r.get("extraction") or {}
        rows.append({"fax_id": r["fax_id"], "pages": r.get("pages"), "input_tokens": ex.get("input_tokens"),
                     "output_tokens": ex.get("output_tokens"), "cost_usd": ex.get("cost_usd"),
                     "vision_latency_ms": ex.get("vision_latency_ms"), "cache_hit": ex.get("cache_hit")})
    known = [x for x in rows if x["cost_usd"] is not None]
    tot = {
        "faxes_with_cost": len(known),
        "input_tokens": sum(x["input_tokens"] or 0 for x in known),
        "output_tokens": sum(x["output_tokens"] or 0 for x in known),
        "cost_usd": round(sum(x["cost_usd"] for x in known), 5),
        "pages": sum(x["pages"] or 0 for x in known),
    }
    tot["cost_per_fax"] = round(tot["cost_usd"] / len(known), 5) if known else None
    tot["cost_per_page"] = round(tot["cost_usd"] / tot["pages"], 5) if tot["pages"] else None
    return rows, tot
