# Linda Mini fax-routing agent

A Playwright worker that logs into the Linda Mini portal, downloads every inbound fax (Session A), reads every page with Claude vision plus an independent Tesseract cross-check, applies the routing SOP with a pure, tested decider, routes each fax from a fresh login (Session B), and **confirms every outcome by reading the portal UI back**. It is crash-safe: a durable write-ahead checkpoint means a restart, timeout or 503 never causes a second submit.

## Run it

```bash
# terminal 1: the supplied portal (expects ../linda-mini; override with PORTAL_DIR=...)
make setup        # uv sync + Chromium + the portal's own venv if missing
make portal       # http://127.0.0.1:8000

# terminal 2
make demo         # full browser run, extraction replayed from the committed cache (no API key)
make run          # same, with live Claude vision on cache misses (ANTHROPIC_API_KEY in .env)
make report       # results/report.html for the last run
```

Without `uv`: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium`, then `.venv/bin/python -m agent run --replay` from the repo root (add `pip install pytest` for the tests). For a clean slate, stop the portal, run `python reset_sandbox.py` in `linda-mini/`, restart it, and add `--fresh`. Tesseract is optional (`brew install tesseract`); without it the run says so and the summary warns that the cross-check is off.

## Result

Clean run against the supplied portal on `:8000` ([`results/sample/`](results/sample/): `results.json`, `results.csv`, `report.html`, `events.jsonl`, one Session B Playwright trace):

| Fax | Read as | Action | Why |
|---|---|---|---|
| FX-201 | referral RF-3001 | routed → Olivia Chen - Referrals | cover sheet + referral |
| FX-202 | lab result LAB-8302 | needs_review | printed flag "CRITICAL RESULT - CALLING PROVIDER" (quoted in the note) |
| FX-203 | imaging report IMG-4003 | routed → Noah Patel / Imaging | |
| FX-204 | prior auth PA-4004 | routed → Ethan Brooks | same sender and timestamp as FX-203, different document |
| FX-205 | refill request RX-4005 | routed → Ava Wilson | |
| FX-206 | records request MR-4006 | routed → Mia Gomez | page 2 is its signed release, not a second document |
| FX-207 | EOB EOB-4007 | routed → Liam Rivera | |
| FX-208 | lab result LAB-4008 | needs_review | 1-page report says values are "on following table"; no table follows (see [NOTES](NOTES.md#the-fx-208-call-and-why-it-is-not-fitted-to-one-fax)) |
| FX-209 | unreadable | needs_review | "[SCAN CUT OFF]", continuation page missing |
| FX-210 | referral, resend of RF-3001 | skipped_duplicate of FX-201 | same ID, patient, DOB and category; received later |
| FX-211 | imaging report | already_routed | Routed before the agent ran; never touched |
| FX-212 | lab + imaging | needs_review | mixed packet |

Every routed and needs_review fax is `ui_verified`: its routing panel **and** its Routed Documents row match the intended values.

## Evidence

**Reliability**
- `make chaos` ([`results/chaos_matrix.md`](results/chaos_matrix.md)): 13 cases, all passing, each against its own throwaway portal and judged only through the UI. Covers every fault mode (download 503, session expiry on submit, commit-then-503), `kill -9` at four checkpoints followed by a resume, a sandbox reset under a stale checkpoint, a genuine 409, an undownloadable fax, a download-time re-login, and a second worker on the same checkpoint.
- [`results/sample-fault-commit-then-503/`](results/sample-fault-commit-then-503/): the portal saved FX-201's route but answered 503. The agent re-read the UI and verified the route, with exactly one submit.
- `make test`: 130 tests. 24 run against a real portal, including a negative two-session test (routing in the download session gets the portal's 409).
- [`FAILURES.md`](FAILURES.md): 13 real bugs found while building, by the test suite, by adversarial testing and by the model comparison. Each lists root cause, fix and the test that now pins it.

**Perception (live Claude, measured)**

| Model (live, all 12 faxes) | Same routing decisions as the reference | Unsafe auto-routes | $/fax | Vision p50 |
|---|---|---|---|---|
| Opus 5.5, run 1 / run 2 | 12/12 / 12/12 (0 field diffs) | 0 | $0.034 | 4.4–4.9 s |
| Sonnet 5.5 | 12/12 | 0 | $0.017 | 3.4 s |
| Haiku 5.5 | 12/12 | 0 | $0.0009 | 2.0 s |

From [`eval/model_compare.md`](eval/model_compare.md). This measures agreement and repeatability against the committed Opus extraction, not accuracy against human labels.
- **Damaged PDFs, live vision** ([`eval/perturbation_live.md`](eval/perturbation_live.md)): every auto-routed fax was re-sent with a page blanked or dropped. All 12 variants were detected and sent to review; 0 were auto-routed.
- **Accuracy vs. human labels** ([`eval/report.md`](eval/report.md)): I read all 12 faxes in the portal UI and wrote blind notes; 7 labels are my direct call, and 5 where I was unsure or wrong were settled by applying the SOP with AI assistance (each is marked, with my original note, in [`eval/labels.yaml`](eval/labels.yaml)). Scored against the sample run: **0 unsafe auto-routes, exact-route match 12/12, over-review 0/6, duplicate precision/recall 1.00/1.00.** n=12 is a smoke test, not an accuracy estimate; FX-208 is the one label that is a policy call rather than a reading (see NOTES). Over-review is reported next to unsafe auto-routes so that sending everything to review can't look "safe".

## How it works

See [`NOTES.md`](NOTES.md) for the design, the decision for each fault mode, and the known gaps.
- **Perception and policy are split.** The model only transcribes: verbatim flags, IDs, page markers and completeness statements, in a strict schema. `agent/decide.py` is a pure, ordered rule set. Rules can be tested without a browser or API key, and every decision can be replayed.
- **Rule order:** already routed → duplicate → unreadable / wrong document → printed urgency flag → mixed → incomplete → unclear → OCR contradiction → SOP table. Duplicates come before exceptions, so a resend of an urgent fax doesn't become a second review task.
- **Idempotency:** a write-ahead INTENT is stored before any click. Every attempt, including after a crash, starts by reading the fax's status in the UI, and a submit response is never trusted on its own. An `flock` stops two workers sharing a checkpoint. Outcomes are reported once.

## Boundaries

- **Navigation:** the only `goto` is the portal's base URL (start, and re-entry after a stranded login or an error page). Everything else is clicked. `tests/test_boundaries.py` enforces this, along with no `__admin`, no portal DB, no seeded files and no hard-coded fax IDs.
- **Extraction input** is always the PDF downloaded through the portal, identified by its sha256. The PDFs have no text layer, and each run checks for one.
- **No clinical inference:** only *printed* flags count, and they are quoted verbatim in the note. When something is uncertain, the fax goes to review with a reason.
- **The chaos harness** (`harness/`) launches its own throwaway portals with their own `LINDA_DB_PATH` and deletes those temp DBs to simulate resets. That is evaluator tooling, never an input to the worker.

## Configuration

`.env` (gitignored) or the environment; all settings are in `agent/config.py`.
- `ANTHROPIC_API_KEY`: needed only for live vision.
- `LINDA_BASE_URL`: default `http://127.0.0.1:8000`.
- `LINDA_USERNAME` / `LINDA_PASSWORD`: default to the training account.
- `LINDA_OCR_MODE`: `vision` or `replay`.
- `LINDA_VISION_MODEL`: default `claude-opus-5-5`.
- `LINDA_WORK_DIR`: where `state/`, `downloads/`, `results/` and `runs/` go.

Outputs per run:
- `results/results.json`: per fax, the extracted type, IDs, evidence, action, reasons, selected fields, the note sent, verification, sessions A/B, submits and recoveries, and tokens/cost.
- `results/results.csv` and `results/summary.md`.
- `runs/<run_id>/events.jsonl` (patient name and DOB hashed) and Playwright traces.

The portal never displays routing notes, so they're recorded as sent but can't be verified in the UI.
