# Eval report

- Results: `results/sample/results.json` (run `20261106T053209Z`, 12 faxes)
- Events: `results/sample/events.jsonl`
- Labels: `not used (--ops-only)`

## Latency per stage

| Stage | n | p50 | max | What it measures |
|---|---|---|---|---|
| download | 12 | 222 ms | 363 ms | Session A click → validated PDF on disk (incl. 503 retries) |
| vision | 12 | 5.16 s | 10.66 s | live Claude vision call, recorded when the cache entry was produced |
| extract | 12 | 839 ms | 2.16 s | this run's extraction step (cache read in replay, + Tesseract cross-check) |
| route | 10 | 537 ms | 584 ms | fresh Session B login → navigate → fill → Review → OK response |
| verify | 12 | 289 ms | 371 ms | UI re-read of routing panel + Routed Documents after submit |

Run wall-clock (run_started → run_finished): 28.0 s for 12 faxes.

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
