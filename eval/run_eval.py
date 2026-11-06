"""Score an agent run against the candidate's hand labels and write eval/report.md.

    uv run python -m eval.run_eval [--results results/results.json] [--labels eval/labels.yaml]
                                   [--events runs/<run_id>/events.jsonl] [--ops-only] [-o eval/report.md]

The agent never reads labels.yaml; it is only read here, after the run.
--ops-only skips accuracy scoring and reports latency / tokens / cost (needs no labels).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from . import metrics as M

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = Path(__file__).resolve().parent


def load_results(path: Path) -> tuple[dict, list[dict]]:
    doc = json.loads(path.read_text())
    return doc, doc.get("results") or []


def default_events(results_path: Path, run_id: str | None) -> Path | None:
    """events.jsonl next to results.json (committed sample), else <work_dir>/runs/<run_id>/events.jsonl."""
    cands = [results_path.parent / "events.jsonl"]
    if run_id:
        cands.append(results_path.resolve().parent.parent / "runs" / run_id / "events.jsonl")
    return next((p for p in cands if p.exists()), None)


def rel(p: Path | None) -> str:
    if p is None:
        return "none"
    try:
        return str(p.resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


def load_events(path: Path | None) -> list[dict]:
    if not path or not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _pct(num: int, den: int) -> str:
    return f"{num}/{den} ({100 * num / den:.0f}%)" if den else "n/a"


def _f(x: float | None, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def _ms(x: float | None) -> str:
    return "-" if x is None else f"{x / 1000:.2f} s" if x >= 1000 else f"{x:.0f} ms"


def accuracy_section(s: M.Scores) -> list[str]:
    L = ["## Accuracy and safety (vs. candidate hand labels)", ""]
    L += ["| Metric | Value | Target |", "|---|---|---|",
          f"| **Unsafe auto-routes** (label: review/duplicate/already routed; agent: auto-routed) | "
          f"**{len(s.unsafe_auto_routes)}** {', '.join(s.unsafe_auto_routes)} | 0 |",
          f"| Misroutes (both say routed, different destination) | {len(s.misroutes)} {', '.join(s.misroutes)} | 0 |",
          f"| Extra submissions on duplicates / already-routed | {len(s.extra_submissions)} "
          f"{', '.join(s.extra_submissions)} | 0 |",
          f"| Exact-route match (action + all 3 fields + duplicate_of) | {_pct(s.exact_route_match, s.n)} | 100% |",
          f"| Action accuracy | {_pct(s.action_accuracy, s.n)} | 100% |",
          f"| Classification accuracy | {_pct(s.classification_correct, s.n)} | 100% |",
          f"| Over-review rate (label routed, agent sent to review) | {_pct(len(s.over_review), s.label_routed)} "
          f"{', '.join(s.over_review)} | low |",
          f"| Duplicate precision / recall | {_f(s.dup_precision)} / {_f(s.dup_recall)} "
          f"(TP {s.dup_tp}, FP {s.dup_fp}, FN {s.dup_fn}) | 1.00 / 1.00 |",
          f"| Failed (agent gave up) | {len(s.failed)} {', '.join(s.failed)} | 0 |", ""]
    if s.dup_wrong_original:
        L += [f"Duplicates attributed to the wrong original: {', '.join(s.dup_wrong_original)}", ""]
    if s.missing_results:
        L += [f"Labelled faxes with no agent result (scored as failed): {', '.join(s.missing_results)}", ""]
    if s.unlabeled_results:
        L += [f"Agent results with no label (ignored): {', '.join(s.unlabeled_results)}", ""]

    cols = list(M.ACTIONS)
    L += ["### Confusion matrix (rows = label, columns = agent)", "",
          "| label \\ agent | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
    for la in M.LABEL_ACTIONS:
        L.append(f"| {la} | " + " | ".join(str(s.confusion[la][c] or "·") for c in cols) + " |")
    L += ["", "### Per fax", "",
          "| Fax | Label action | Agent action | Label category | Agent category | Route match | Flags |",
          "|---|---|---|---|---|---|---|"]
    for f in s.per_fax:
        rm = "yes" if f.expected_route == f.agent_route else f"no (expected {f.expected_route}, got {f.agent_route})"
        L.append(f"| {f.fax_id} | {f.label_action} | {f.agent_action} | {f.label_category} | {f.agent_category} | "
                 f"{rm} | {', '.join(f.flags) or '-'} |")
    return L + [""]


def ops_section(events: list[dict], results: list[dict]) -> list[str]:
    lat = M.stage_latencies(events, results)
    L = ["## Latency per stage", ""]
    if not events:
        L += ["_No events.jsonl found; only the recorded vision latency is available._", ""]
    L += ["| Stage | n | p50 | max | What it measures |", "|---|---|---|---|---|"]
    desc = {
        "download": "Session A click → validated PDF on disk (incl. 503 retries)",
        "vision": "live Claude vision call, recorded when the cache entry was produced",
        "extract": "this run's extraction step (cache read in replay, + Tesseract cross-check)",
        "route": "fresh Session B login → navigate → fill → Review → OK response",
        "verify": "UI re-read of routing panel + Routed Documents after submit",
    }
    for st in ("download", "vision", "extract", "route", "verify"):
        sm = M.summarize([d[st] for d in lat.values() if st in d])
        L.append(f"| {st} | {sm['n']} | {_ms(sm['p50'])} | {_ms(sm['max'])} | {desc[st]} |")
    run_ts = [e["ts"] for e in events if e["event"] in ("run_started", "run_finished")]
    if len(run_ts) >= 2:
        L += ["", f"Run wall-clock (run_started → run_finished): {run_ts[-1] - run_ts[0]:.1f} s "
                  f"for {len(results)} faxes."]
    rows, tot = M.cost_table(results)
    L += ["", "## Tokens and cost per fax (Claude vision, from the live run that produced the cache)", "",
          "| Fax | Pages | Input tok | Output tok | Cost USD | Vision latency |", "|---|---|---|---|---|---|"]
    for r in rows:
        cost = "-" if r["cost_usd"] is None else f"${r['cost_usd']:.4f}"
        L.append(f"| {r['fax_id']} | {r['pages']} | {r['input_tokens'] or '-'} | {r['output_tokens'] or '-'} | "
                 f"{cost} | {_ms(r['vision_latency_ms'])} |")
    L += [f"| **Total** | {tot['pages']} | {tot['input_tokens']} | {tot['output_tokens']} | "
          f"**${tot['cost_usd']:.4f}** | |", "",
          f"Mean cost: **${_f(tot['cost_per_fax'], 4)} per fax**, ${_f(tot['cost_per_page'], 4)} per page "
          f"({tot['faxes_with_cost']} faxes with recorded usage). Replay runs cost $0.", ""]
    return L


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="eval.run_eval", description=__doc__.splitlines()[0])
    ap.add_argument("--results", type=Path, default=ROOT / "results" / "sample" / "results.json")
    ap.add_argument("--labels", type=Path, default=EVAL_DIR / "labels.yaml")
    ap.add_argument("--events", type=Path, default=None, help="default: runs/<run_id>/events.jsonl next to results")
    ap.add_argument("--ops-only", action="store_true", help="latency/cost only; does not need labels")
    ap.add_argument("-o", "--out", type=Path, default=EVAL_DIR / "report.md")
    args = ap.parse_args(argv)

    if not args.results.exists():
        print(f"error: results file not found: {args.results}\n"
              f"Run the agent first (make demo / make run) or pass --results.", file=sys.stderr)
        return 2
    doc, results = load_results(args.results)
    events_path = args.events or default_events(args.results, doc.get("run_id"))
    events = load_events(events_path)

    scores = None
    if not args.ops_only:
        if not args.labels.exists():
            print(f"error: refusing to score: {args.labels} does not exist.\n"
                  f"  Copy eval/labels.template.yaml to eval/labels.yaml and fill every field by viewing each fax\n"
                  f"  in the portal UI yourself. Labels must come from a human, never from the agent's output.\n"
                  f"  (Use --ops-only for latency/cost without labels.)", file=sys.stderr)
            return 3
        labels = yaml.safe_load(args.labels.read_text()) or {}
        problems = M.validate_labels(labels)
        if problems:
            print(f"error: refusing to score: {args.labels} is incomplete or invalid "
                  f"({len(problems)} problem(s)):", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 3
        scores = M.score(results, labels)

    L = ["# Eval report", "",
         f"- Results: `{rel(args.results)}` (run `{doc.get('run_id')}`, {len(results)} faxes)",
         f"- Events: `{rel(events_path)}`",
         f"- Labels: `{rel(args.labels) if scores else 'not used (--ops-only)'}`"
         + (f" (labeller: {labels.get('labeller') or 'unspecified'})" if scores else ""), ""]
    if scores:
        L.append(f"**Headline:** {len(scores.unsafe_auto_routes)} unsafe auto-routes, exact-route match "
                 f"{_pct(scores.exact_route_match, scores.n)}, over-review "
                 f"{_pct(len(scores.over_review), scores.label_routed)}, duplicate P/R "
                 f"{_f(scores.dup_precision)}/{_f(scores.dup_recall)}.")
        L.append("")
        L += accuracy_section(scores)
    L += ops_section(events, results)
    L += ["## Caveats", "",
          "- Labels are the candidate's own reading of 12 synthetic faxes: n=12 is a smoke test, not a "
          "statistically meaningful accuracy estimate.",
          "- A replay run scores the *cached* live extraction. It proves the decision/routing layer is "
          "deterministic; it does not re-measure vision accuracy (that needs a live run, `make run`).",
          "- `vision` latency/cost are from the single live run that produced `cache/`; nondeterminism across "
          "live runs is not measured here.", ""]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(L))
    print("\n".join(L[:12]))
    print(f"\nwrote {args.out}")
    if scores and (scores.unsafe_auto_routes or scores.extra_submissions):
        return 1  # a safety regression should fail CI
    return 0


if __name__ == "__main__":
    sys.exit(main())
