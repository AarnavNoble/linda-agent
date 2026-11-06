# Eval report

- Results: `results/sample/results.json` (run `20261106T064018Z-192`, 12 faxes)
- Events: `results/sample/events.jsonl`
- Labels: `eval/labels.yaml` (labeller: Aarnav Noble)

**Headline:** 0 unsafe auto-routes, exact-route match 12/12 (100%), over-review 0/6 (0%), duplicate P/R 1.00/1.00.

## Accuracy and safety (vs. candidate hand labels)

| Metric | Value | Target |
|---|---|---|
| **Unsafe auto-routes** (label: review/duplicate/already routed; agent: auto-routed) | **0**  | 0 |
| Misroutes (both say routed, different destination) | 0  | 0 |
| Extra submissions on duplicates / already-routed | 0  | 0 |
| Exact-route match (action + all 3 fields + duplicate_of) | 12/12 (100%) | 100% |
| Action accuracy | 12/12 (100%) | 100% |
| Classification accuracy | 12/12 (100%) | 100% |
| Over-review rate (label routed, agent sent to review) | 0/6 (0%)  | low |
| Duplicate precision / recall | 1.00 / 1.00 (TP 1, FP 0, FN 0) | 1.00 / 1.00 |
| Failed (agent gave up) | 0  | 0 |

### Confusion matrix (rows = label, columns = agent)

| label \ agent | routed | needs_review | skipped_duplicate | already_routed | failed |
|---|---|---|---|---|---|
| routed | 6 | · | · | · | · |
| needs_review | · | 4 | · | · | · |
| skipped_duplicate | · | · | 1 | · | · |
| already_routed | · | · | · | 1 | · |

### Per fax

| Fax | Label action | Agent action | Label category | Agent category | Route match | Flags |
|---|---|---|---|---|---|---|
| FX-201 | routed | routed | referral | referral | yes | - |
| FX-202 | needs_review | needs_review | lab_result | lab_result | yes | - |
| FX-203 | routed | routed | imaging_report | imaging_report | yes | - |
| FX-204 | routed | routed | prior_authorization | prior_authorization | yes | - |
| FX-205 | routed | routed | refill_request | refill_request | yes | - |
| FX-206 | routed | routed | records_request | records_request | yes | - |
| FX-207 | routed | routed | eob | eob | yes | - |
| FX-208 | needs_review | needs_review | lab_result | lab_result | yes | - |
| FX-209 | needs_review | needs_review | unclear | unclear | yes | - |
| FX-210 | skipped_duplicate | skipped_duplicate | referral | referral | yes | - |
| FX-211 | already_routed | already_routed | imaging_report | imaging_report | yes | - |
| FX-212 | needs_review | needs_review | mixed | mixed | yes | - |

## Latency per stage

| Stage | n | p50 | max | What it measures |
|---|---|---|---|---|
| download | 12 | 210 ms | 310 ms | Session A click → validated PDF on disk (incl. 503 retries) |
| vision | 12 | 5.16 s | 10.66 s | live Claude vision call, recorded when the cache entry was produced |
| extract | 12 | 820 ms | 2.03 s | this run's extraction step (cache read in replay, + Tesseract cross-check) |
| route | 10 | 544 ms | 570 ms | fresh Session B login → navigate → fill → Review → OK response |
| verify | 12 | 288 ms | 292 ms | UI re-read of routing panel + Routed Documents after submit |

Run wall-clock (run_started → run_finished): 25.2 s for 12 faxes.

## Tokens and cost per fax (Claude vision, from the live run that produced the cache)

| Fax | Pages | Input tok | Output tok | Cost USD | Vision latency |
|---|---|---|---|---|---|
| FX-201 | 2 | 7768 | 658 | $0.0442 | 8.78 s |
| FX-202 | 1 | 5444 | 386 | $0.0295 | 5.71 s |
| FX-203 | 1 | 5444 | 382 | $0.0294 | 5.35 s |
| FX-204 | 1 | 5444 | 368 | $0.0291 | 6.32 s |
| FX-205 | 1 | 5444 | 390 | $0.0296 | 3.90 s |
| FX-206 | 2 | 7768 | 657 | $0.0442 | 6.38 s |
| FX-207 | 1 | 5444 | 365 | $0.0291 | 4.29 s |
| FX-208 | 1 | 5444 | 373 | $0.0292 | 3.90 s |
| FX-209 | 1 | 5444 | 374 | $0.0293 | 4.96 s |
| FX-210 | 1 | 5444 | 409 | $0.0300 | 4.28 s |
| FX-211 | 1 | 5444 | 368 | $0.0291 | 4.15 s |
| FX-212 | 3 | 10092 | 958 | $0.0595 | 10.66 s |
| **Total** | 16 | 74624 | 5688 | **$0.4123** | |

Mean cost: **$0.0344 per fax**, $0.0258 per page (12 faxes with recorded usage). Replay runs cost $0.

## Caveats

- Labels are the candidate's own reading of 12 synthetic faxes: n=12 is a smoke test, not a statistically meaningful accuracy estimate.
- A replay run scores the *cached* live extraction. It proves the decision/routing layer is deterministic; it does not re-measure vision accuracy (that needs a live run, `make run`).
- `vision` latency/cost are from the single live run that produced `cache/`; nondeterminism across live runs is not measured here.
