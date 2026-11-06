# Chaos matrix — PASS

Generated 2026-11-06 01:39:45 EST by `uv run python -m harness.chaos` in 150.4s. Each case runs against its own throwaway portal (fresh DB, own port) with the agent as a subprocess; outcomes are judged only through the portal UI (separate read-only session) and the agent's results.json / events.jsonl.

| Case | Scenario | Result | Checks | Time |
|---|---|---|---|---|
| `baseline+lock` | clean run; 2nd worker on same work dir; idempotent rerun | PASS | 17/17 | 33.4s |
| `download_503_once` | download returns HTML 503 once | PASS | 17/17 | 34.5s |
| `session_expire_on_route_once` | session deleted during submit | PASS | 16/16 | 34.4s |
| `route_commit_then_503_once` | route saved, response 503 | PASS | 16/16 | 34.0s |
| `kill@after_download` | hard exit after first download, resume | PASS | 16/16 | 38.3s |
| `kill@before_click` | hard exit after INTENT, before submit, resume | PASS | 16/16 | 42.2s |
| `kill@after_click` | hard exit after submit, before verify, resume | PASS | 17/17 | 41.4s |
| `kill@after_click#4` | hard exit after the 4th submit (3 faxes done, unreported), resume | PASS | 18/18 | 40.3s |
| `portal_reset_stale_checkpoint` | full run, sandbox reset, rerun same work dir | PASS | 17/17 | 51.2s |
| `reset_mid_run_409` | sandbox reset after first download: 409 + dead Session A | PASS | 17/17 | 33.9s |
| `download_gives_up` | one fax undownloadable in run 1, healthy run 2 | PASS | 19/19 | 41.1s |
| `download_reauth` | login expires right before Download click | PASS | 3/3 | 3.1s |
| `two_session_409` | same-session submit is refused and classified | PASS | 3/3 | 5.5s |

## Checks per case

### `baseline+lock` — PASS

- PASS first worker started
- PASS second worker on same work dir fails fast on the lock
- PASS second worker left no run behind
- PASS first worker exits 0
- PASS results.json written
- PASS every inbox fax has a result
- PASS no failed outcomes
- PASS at most one Routed Documents row per fax
- PASS UI matches every result (panel + Routed Documents)
- PASS no Routed Documents row the agent cannot account for
- PASS no submit after a possibly-committed submit (any run)
- PASS no unhandled per-fax errors
- PASS results attribute each download to the session it happened in
- PASS route session never equals the download session
- PASS every fax reported exactly once across runs
- PASS idempotent rerun: exits 0, reports nothing new, submits nothing
- PASS idempotent rerun leaves UI unchanged

### `download_503_once` — PASS
_fault_fax: FX-212_

- PASS agent exits 0
- PASS results.json written
- PASS every inbox fax has a result
- PASS no failed outcomes
- PASS at most one Routed Documents row per fax
- PASS UI matches every result (panel + Routed Documents)
- PASS no Routed Documents row the agent cannot account for
- PASS outcome identical to clean baseline (actions, fields, UI)
- PASS no submit after a possibly-committed submit (any run)
- PASS no unhandled per-fax errors
- PASS results attribute each download to the session it happened in
- PASS route session never equals the download session
- PASS every fax reported exactly once across runs
- PASS fault fired: a download returned an HTML error page
- PASS error page detected without waiting out a timeout
- PASS FX-212: re-navigated and retried to a valid PDF (pages + sha validated)
- PASS FX-212: no invalid PDF was ever accepted

### `session_expire_on_route_once` — PASS
_fault_fax: FX-201_

- PASS agent exits 0
- PASS results.json written
- PASS every inbox fax has a result
- PASS no failed outcomes
- PASS at most one Routed Documents row per fax
- PASS UI matches every result (panel + Routed Documents)
- PASS no Routed Documents row the agent cannot account for
- PASS outcome identical to clean baseline (actions, fields, UI)
- PASS no submit after a possibly-committed submit (any run)
- PASS no unhandled per-fax errors
- PASS results attribute each download to the session it happened in
- PASS route session never equals the download session
- PASS every fax reported exactly once across runs
- PASS fault fired: session expired on submit
- PASS FX-201: re-login happened in the SAME Session B browser context
- PASS FX-201: refilled, resubmitted once and verified

### `route_commit_then_503_once` — PASS
_fault_fax: FX-201_

- PASS agent exits 0
- PASS results.json written
- PASS every inbox fax has a result
- PASS no failed outcomes
- PASS at most one Routed Documents row per fax
- PASS UI matches every result (panel + Routed Documents)
- PASS no Routed Documents row the agent cannot account for
- PASS outcome identical to clean baseline (actions, fields, UI)
- PASS no submit after a possibly-committed submit (any run)
- PASS no unhandled per-fax errors
- PASS results attribute each download to the session it happened in
- PASS route session never equals the download session
- PASS every fax reported exactly once across runs
- PASS fault fired: route committed but response was 503
- PASS FX-201: exactly one submit (no second submit after the 503)
- PASS FX-201: re-read UI, found it routed and verified

### `kill@after_download` — PASS
_crashed_fax: FX-212, done_before_crash: 0_

- PASS run 1 hard-exited at after_download
- PASS resume run exits 0
- PASS run 1 submitted nothing
- PASS results.json written
- PASS every inbox fax has a result
- PASS no failed outcomes
- PASS at most one Routed Documents row per fax
- PASS UI matches every result (panel + Routed Documents)
- PASS no Routed Documents row the agent cannot account for
- PASS outcome identical to clean baseline (actions, fields, UI)
- PASS no submit after a possibly-committed submit (any run)
- PASS no unhandled per-fax errors
- PASS results attribute each download to the session it happened in
- PASS route session never equals the download session
- PASS every fax reported exactly once across runs
- PASS third (no-op) run exits 0 and submits nothing

### `kill@before_click` — PASS
_crashed_fax: FX-201, done_before_crash: 0_

- PASS run 1 hard-exited at before_click
- PASS resume run exits 0
- PASS FX-201: no submit before the crash; exactly one after resume
- PASS results.json written
- PASS every inbox fax has a result
- PASS no failed outcomes
- PASS at most one Routed Documents row per fax
- PASS UI matches every result (panel + Routed Documents)
- PASS no Routed Documents row the agent cannot account for
- PASS outcome identical to clean baseline (actions, fields, UI)
- PASS no submit after a possibly-committed submit (any run)
- PASS no unhandled per-fax errors
- PASS results attribute each download to the session it happened in
- PASS route session never equals the download session
- PASS every fax reported exactly once across runs
- PASS third (no-op) run exits 0 and submits nothing

### `kill@after_click` — PASS
_crashed_fax: FX-201, done_before_crash: 0_

- PASS run 1 hard-exited at after_click
- PASS resume run exits 0
- PASS FX-201: submitted before the crash, NOT resubmitted on resume
- PASS FX-201: resume re-read the UI and verified the existing route
- PASS results.json written
- PASS every inbox fax has a result
- PASS no failed outcomes
- PASS at most one Routed Documents row per fax
- PASS UI matches every result (panel + Routed Documents)
- PASS no Routed Documents row the agent cannot account for
- PASS outcome identical to clean baseline (actions, fields, UI)
- PASS no submit after a possibly-committed submit (any run)
- PASS no unhandled per-fax errors
- PASS results attribute each download to the session it happened in
- PASS route session never equals the download session
- PASS every fax reported exactly once across runs
- PASS third (no-op) run exits 0 and submits nothing

### `kill@after_click#4` — PASS
_crashed_fax: FX-204, done_before_crash: 3_

- PASS run 1 hard-exited at after_click#4
- PASS run 1 finished some faxes before dying (they are unreported until resume)
- PASS resume run exits 0
- PASS FX-204: submitted before the crash, NOT resubmitted on resume
- PASS FX-204: resume re-read the UI and verified the existing route
- PASS results.json written
- PASS every inbox fax has a result
- PASS no failed outcomes
- PASS at most one Routed Documents row per fax
- PASS UI matches every result (panel + Routed Documents)
- PASS no Routed Documents row the agent cannot account for
- PASS outcome identical to clean baseline (actions, fields, UI)
- PASS no submit after a possibly-committed submit (any run)
- PASS no unhandled per-fax errors
- PASS results attribute each download to the session it happened in
- PASS route session never equals the download session
- PASS every fax reported exactly once across runs
- PASS third (no-op) run exits 0 and submits nothing

### `portal_reset_stale_checkpoint` — PASS

- PASS first run exits 0
- PASS portal really was reset (only the pre-routed fax is Routed)
- PASS rerun after reset exits 0
- PASS reconcile noticed every fax we had routed is New again
- PASS every such fax was routed again (one submit each)
- PASS results.json written
- PASS every inbox fax has a result
- PASS no failed outcomes
- PASS at most one Routed Documents row per fax
- PASS UI matches every result (panel + Routed Documents)
- PASS no Routed Documents row the agent cannot account for
- PASS outcome identical to clean baseline (actions, fields, UI)
- PASS no submit after a possibly-committed submit (any run)
- PASS no unhandled per-fax errors
- PASS results attribute each download to the session it happened in
- PASS route session never equals the download session
- PASS reset outcomes re-reported (new portal world), nothing else

### `reset_mid_run_409` — PASS
_fault_fax: FX-212_

- PASS agent downloaded a first fax
- PASS agent exits 0
- PASS FX-212: portal rejected the route with 409 (download lost in reset)
- PASS FX-212: re-downloaded in a new Session A, then routed from a new Session B
- PASS Session A re-authenticated after its login vanished in the reset
- PASS results.json written
- PASS every inbox fax has a result
- PASS no failed outcomes
- PASS at most one Routed Documents row per fax
- PASS UI matches every result (panel + Routed Documents)
- PASS no Routed Documents row the agent cannot account for
- PASS outcome identical to clean baseline (actions, fields, UI)
- PASS no submit after a possibly-committed submit (any run)
- PASS no unhandled per-fax errors
- PASS results attribute each download to the session it happened in
- PASS route session never equals the download session
- PASS every fax reported exactly once across runs

### `download_gives_up` — PASS
_failed_fax: FX-201, held: 10_

- PASS run 1 exits 1 (completed with failures), not a crash
- PASS FX-201: undownloadable fax reported failed/not_processed, never decided or submitted
- PASS faxes received after it (possible resends) were held, not submitted
- PASS faxes received before it were unaffected
- PASS portal untouched for the failed and held faxes
- PASS run 2 (download healthy) exits 0
- PASS run 2 retried every failed fax
- PASS results.json written
- PASS every inbox fax has a result
- PASS no failed outcomes
- PASS at most one Routed Documents row per fax
- PASS UI matches every result (panel + Routed Documents)
- PASS no Routed Documents row the agent cannot account for
- PASS outcome identical to clean baseline (actions, fields, UI)
- PASS no submit after a possibly-committed submit (any run)
- PASS no unhandled per-fax errors
- PASS results attribute each download to the session it happened in
- PASS route session never equals the download session
- PASS run 2 re-reported exactly the faxes whose outcome changed

### `download_reauth` — PASS

- PASS expiry fired before the Download click
- PASS re-authenticated and downloaded a valid PDF of the same fax
- PASS tab returned to the fax page (not stranded on /login)

### `two_session_409` — PASS
_fax: FX-212_

- PASS same-session submit classified as two_session (HTTP 409)
- PASS refused submit created no route
- PASS Routed Documents unchanged (pre-routed fax only)
