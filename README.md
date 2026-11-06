# Linda Mini fax-routing agent

A Playwright worker that discovers, downloads, reads (Claude vision + Tesseract cross-check), routes and **UI-verifies** all 12 faxes in the Linda Mini portal, using two separate sessions, a crash-safe checkpoint and an eval harness that counts unsafe auto-routes.

## Quickstart

```bash
# terminal 1: the supplied portal (sibling dir ../linda-mini, set PORTAL_DIR= otherwise)
make setup && make portal

# terminal 2: full browser run in replay mode (committed vision cache, no API key needed)
make demo
make report RESULTS=results/results.json   # HTML report for that run -> results/report.html
```

`make run` is the live run with Claude vision. It needs `ANTHROPIC_API_KEY` in `.env` (copy `.env.example`). For a clean demo, stop the portal, run `python reset_sandbox.py` in `../linda-mini`, restart it, and run `uv run python -m agent run --replay --fresh`.

## Results (replay run, `results/sample/`)

These are from a replay run on a fresh portal: a real browser, real downloads, and real routing and verification, with extraction served from the committed vision cache. Open [`results/sample/report.html`](results/sample/report.html) for evidence, reasons and timelines.

| Fax | Extracted type | Source ID | Action | Assigned To | Verification | Retries |
|---|---|---|---|---|---|---|
| FX-201 | referral | RF-3001 | routed | Olivia Chen - Referrals | ui_verified | 0 |
| FX-202 | lab_result | LAB-8302 | needs_review (`CRITICAL RESULT - CALLING PROVIDER`) | Jordan Ellis - Manual Review | ui_verified | 0 |
| FX-203 | imaging_report | IMG-4003 | routed | Noah Patel - Clinical Review | ui_verified | 0 |
| FX-204 | prior_authorization | PA-4004 | routed | Ethan Brooks - Authorizations | ui_verified | 0 |
| FX-205 | refill_request | RX-4005 | routed | Ava Wilson - Nursing / Medication | ui_verified | 0 |
| FX-206 | records_request | MR-4006 | routed | Mia Gomez - Medical Records | ui_verified | 0 |
| FX-207 | eob | EOB-4007 | routed | Liam Rivera - Billing | ui_verified | 0 |
| FX-208 | lab_result | LAB-4008 | routed | Noah Patel - Clinical Review | ui_verified | 0 |
| FX-209 | (unclear) | - | needs_review (scan cut off, continuation page missing) | Jordan Ellis - Manual Review | ui_verified | 0 |
| FX-210 | referral | (refs RF-3001) | skipped_duplicate of FX-201 | - (not submitted) | ui_status_new | 0 |
| FX-211 | imaging_report | IMG-4111 | already_routed | - (untouched) | preexisting_route_observed | 0 |
| FX-212 | lab_result + imaging_report | LAB-4012A | needs_review (mixed packet) | Jordan Ellis - Manual Review | ui_verified | 0 |

**Headline metrics:** _TODO(candidate): fill after `make eval` with your own labels: unsafe auto-routes = ?, exact-route match = ?/12, over-review = ?, duplicate P/R = ?/?._

**Chaos matrix:** see [`results/chaos_matrix.md`](results/chaos_matrix.md) (`make chaos`: each fault mode on a fresh portal DB, plus kill/resume at 3 checkpoints).

**Cost and latency** (measured on the live run that produced `cache/`; full table in [`eval/report.md`](eval/report.md)): **$0.034 per fax** ($0.41 for all 12, 16 pages, about 75k input and 5.7k output tokens, `claude-opus-5-5`). Vision p50 is 5.2 s and max 10.7 s (the 3-page packet). Browser stages from the replay run: download p50 0.22 s, route p50 0.54 s, verify p50 0.29 s. The whole 12-fax replay run took 28 s. Replay runs cost $0.

Two replay runs on fresh portals produce identical deterministic fields (`uv run python -m eval.determinism a.json b.json`: 0 diffs). The perturbation probe (`make perturb`) damages every auto-routed PDF: 35 variants, 0 auto-routed ([`eval/perturbation.md`](eval/perturbation.md)).

---

## Requirements

| | |
|---|---|
| Python | 3.12+ (`requires-python >= 3.12`; dependencies pinned in `uv.lock`) |
| [uv](https://docs.astral.sh/uv/) | dependency and venv management (`make setup` runs `uv sync`) |
| Chromium | `uv run playwright install chromium` (part of `make setup`) |
| Tesseract | optional, used for the cross-check: `brew install tesseract`. If it is absent, the cross-check is skipped and logged (`crosscheck.ran=false`) |
| Portal | the supplied `linda-mini/` with its own venv (`pip install -r requirements.txt`) |

PDF rasterisation uses `pypdfium2`, which is pip-only and does not need poppler.

## Credentials

- Portal login defaults to the training account `agent.demo` / `demo-password-123`. Override it with `LINDA_USERNAME` / `LINDA_PASSWORD` in `.env` or the environment.
- `ANTHROPIC_API_KEY` is needed only for live vision (`make run`). It goes in `.env`, which is gitignored. The key is read by the Anthropic SDK and is never logged or written to results.
- `events.jsonl` hashes patient name and DOB, even though all data is synthetic.

## Configuration

All settings come from the environment or `.env` (`agent/config.py`).

| Variable | Default | Meaning |
|---|---|---|
| `LINDA_BASE_URL` | `http://127.0.0.1:8000` | Portal URL. It is the only URL the agent `goto`s; everything else is clicked |
| `LINDA_USERNAME` / `LINDA_PASSWORD` | `agent.demo` / `demo-password-123` | Portal credentials |
| `LINDA_OCR_MODE` | `vision` | `vision` = live Claude call on a cache miss; `replay` = cache only (a miss means extraction is unavailable and the fax goes to `needs_review`) |
| `LINDA_VISION_MODEL` | `claude-opus-5-5` | Vision model; part of the cache key |
| `LINDA_HEADED` | `0` | `1` shows the browser (same as `run --headed`) |
| `LINDA_WORK_DIR` | repo root | Holds `state/` (checkpoint + lock), `downloads/`, `results/`, `runs/<run_id>/` |
| `LINDA_CACHE_DIR` | `./cache` | Extraction cache (committed) |
| `LINDA_NAV_TIMEOUT_MS` | `15000` | Playwright navigation timeout |
| `LINDA_DOWNLOAD_WAIT_MS` | `10000` | How long to wait for the download event before treating the click as failed |
| `LINDA_SUBMIT_TIMEOUT_MS` | `20000` | Wait for the route submit response. A timeout counts as an *uncertain* outcome, so the agent verifies before any retry |
| `LINDA_AGENT_CRASH_AT` | unset | Test hook for chaos kill/resume: `after_download`, `before_click` or `after_click` hard-exits the process |

CLI: `uv run python -m agent run [--replay] [--headed] [--fresh]` and `uv run python -m agent status`.

## OCR and vision tools

1. **Claude vision (primary), `agent/extract/vision.py`.** All pages of a packet go out in one request as rendered PNGs, with schema-constrained structured output (`agent/extract/schema.py`, Pydantic → `messages.parse`). The model transcribes and labels. It never routes. Flags, IDs, names and completeness statements are verbatim quotes or closed enums. Refusal, `max_tokens` or a page-count mismatch all raise `VisionUnavailable`, and the fax then goes to Needs Review with that reason.
2. **Tesseract (independent cross-check), `agent/extract/tesseract.py`.** Tesseract never classifies. It can only escalate to review, and only on a *positive contradiction*: an affirmative urgency marker vision missed (negation-aware, so "No urgent flag" does not count), a footer transmission ID that differs from the portal ID, or a source ID absent from well-read text.
3. **Replay cache, `cache/`.** Entries are keyed by `sha256(downloaded PDF)[:16] + model + prompt version`. The PDF is still downloaded through the portal on every run, and its hash must match. Each entry stores the live run's token counts, cost and latency.

## Outputs

| Path | Contents |
|---|---|
| `results/results.json` | Per fax: ID, extracted type, source ID, evidence quotes, flags, action, reasons, duplicate_of, selected fields, note sent, verification (status + UI panel + Routed Documents row), session IDs A/B, attempts, retries, errors, tokens/cost/latency |
| `results/results.csv`, `results/summary.md` | Flat and table views of the same data |
| `runs/<run_id>/events.jsonl` | Structured event log (PHI-hashed), the source for the latency metrics |
| `runs/<run_id>/traces/*.zip` | Playwright traces per session (`npx playwright show-trace <zip>`); screenshots on error |
| `results/report.html` | `make report`: self-contained HTML report (light and dark) |
| `eval/report.md` | `make eval`: accuracy and safety vs. your labels, plus latency and cost |

## How the brief maps to code

| Brief requirement | Where |
|---|---|
| Session A login + UI navigation, multi-page inbox | `agent/browser.py` (`PortalSession`, single `goto` to base URL), `agent/discover.py` (Next-paging, unique-ID count check) |
| Download via button; never treat an HTML error page as a PDF | `agent/acquire.py` (download event raced against navigation; `%PDF-` + pypdf parse + page count + filename checks; atomic write) |
| OCR/vision on every page of the image-only PDF | `agent/extract/` (vision over all pages, page-count assertion, Tesseract cross-check) |
| SOP routing + exceptions (urgent, mixed, incomplete, unclear) | `agent/sop.py` (exact dropdown labels), `agent/decide.py` (pure, ordered rules) |
| Resends: source ID + context, earliest original only | `agent/decide.py::find_duplicates` (shared ID + same name + DOB + category; ties and partial matches go to review) |
| Already routed: do not modify | `decide.py` rule 1 (from UI status at discovery) + `route.py::read_status` re-check before every submit |
| Fresh Session B after download, same fax ID | `runner.py::route_fax` (new browser context per fax), `browser.py::goto_fax` (exact-text link + h1 + Fax ID assertion) |
| Fill 3 dropdowns, Review, OK; review reason in notes | `agent/route.py::fill_and_review` / `click_submit` (select by label, read back, assert modal summary) |
| Verify in UI, not from HTTP | `agent/route.py::verify` (routing panel + Routed Documents row vs. intent) |
| Transient download failure | `acquire.py` retry with re-navigation |
| Login expiry → re-auth, return to fax | `browser.py::ensure_auth` / `goto_fax`, `runner.py` (`expired` outcome) |
| Commit-then-503 / timeout: re-check before retry | `runner.py::route_fax` (`unknown` outcome → `verify` first) |
| Restart: durable checkpoint, report once | `agent/state.py` (SQLite WAL, write-ahead INTENT, flock), `report.py::write_results` (`reported` flag) |
| Per-fax JSON/CSV | `agent/report.py`, `results/` |
| Tests / replay demo | `tests/` (`make test`), `make demo`, `make chaos` (`agent/chaos.py`) |
| Engineering notes | [`NOTES.md`](NOTES.md), [`FAILURES.md`](FAILURES.md) |
| Evals (beyond the brief) | `eval/` (`make eval`, `make perturb`, `eval.determinism`) |

## Evaluating against your own labels

```bash
cp eval/labels.template.yaml eval/labels.yaml   # fill it in by reading each fax in the portal UI
make eval                                        # refuses to score while any field is blank
```

The labels must come from a human. The agent never reads them. Metrics are exact-route match, classification accuracy, **unsafe auto-routes** (headline, target 0), misroutes, extra submissions on duplicates, over-review rate, duplicate precision/recall and the confusion matrix, plus per-stage latency and $/fax. `make eval` exits non-zero on any unsafe auto-route, so it can gate CI.

All patient data in the sandbox is synthetic.
