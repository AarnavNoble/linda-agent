# Engineering notes

## Architecture

```
 Session A (one context)                      pure, no I/O                 Session B (new context per fax)
 ┌──────────────────────────┐   PDF+sha   ┌──────────────────┐  Decision  ┌──────────────────────────────┐
 │ discover: page inbox,    │────────────▶│ extract          │──────────▶ │ read_status (Routed? stop)   │
 │  count unique IDs        │             │  vision (Claude, │  decide    │ fill 3 dropdowns by label,   │
 │ acquire: click Download, │             │  schema output)  │  (global,  │  read back, Review, assert   │
 │  validate %PDF/pages/    │             │  + Tesseract     │  ordered   │  modal summary, OK           │
 │  filename, atomic write  │             │  cross-check     │  rules)    │ verify: routing panel +      │
 └──────────────────────────┘             │  + sha cache     │            │  Routed Documents vs INTENT  │
                                          └──────────────────┘            └──────────────────────────────┘
        └──────────── SQLite WAL checkpoint (synchronous=FULL) + flock, written after every transition ───────────┘
```

Perception and policy are split on purpose. The model only transcribes and labels: verbatim flags, IDs, page markers and completeness statements. `decide.py` is a pure, table-driven function over those fields, so every routing decision can be replayed and unit-tested without a browser or API key.

## State machine and idempotency

`DISCOVERED → DOWNLOADED → EXTRACTED → DECIDED → INTENT → SUBMITTED_UNVERIFIED → DONE`

- **Write-ahead INTENT.** The exact route, note and an idempotency key (`fax:sha12`) are made durable *before* Session B opens. After a crash the agent resumes the same intent and never re-decides.
- **Verify before retry.** Every attempt, including the first after a resume, starts with `read_status`. If the fax is already Routed, the agent compares the routing panel and the Routed Documents row with the intent: a match counts as success, a mismatch is a `failed:conflict` and is left untouched. The agent never submits twice.
- **Report once.** `reported` is set only after `results.json` is written durably. Outcomes from earlier runs are re-emitted in the artifact but not counted again as new.
- **Reconcile on portal reset.** On discovery the checkpoint is compared with live UI status. If a fax we marked done shows New again (sandbox reset), it is reopened. Each run re-downloads in a fresh Session A every fax not yet done, so the two-session rule holds even with a stale checkpoint.
- **Single worker.** `fcntl.flock` on `state/agent.lock` stops two runs from sharing one checkpoint.

## Decision per fault mode

| Fault | Observed in UI/network | Decision |
|---|---|---|
| `download_503_once` | Click navigates to an HTML 503 and no download event fires | Treat as transient. Never save the body. Re-navigate with `goto_fax`, retry with backoff (max 3). The PDF must pass `%PDF-`, pypdf, page-count and filename checks |
| `session_expire_on_route_once` | Submit redirects to `/login?next=…`; nothing saved | Re-login in the *same* B context (it is still after the download), go back to the fax, re-check status, reload the form (never reuse a stale CSRF token), resubmit |
| `route_commit_then_503_once` | 503 after the route was saved | Outcome is `unknown`. Verify first: the UI shows Routed with matching values, so record `ui_verified` with no resubmit |
| Timeout on submit | No response | Same as the 503 case |
| 409 two-session | Portal rejects the route | Re-download in a new Session A, open a new B, retry once |
| 400 invalid | Field rejected | `failed`. This is a bug, so do not retry blindly |
| Vision unavailable / refusal / wrong page count | No extraction | `needs_review` with the reason. Never crash, never guess |

## Rule precedence

The first matching rule wins: already routed → duplicate (uncertain resend → review) → extraction unavailable / wrong document → explicit urgent flag → mixed categories → incomplete / illegible → unclear or low confidence → Tesseract contradiction → SOP row.

**Why duplicates come before exceptions.** A resend of an urgent original must be *skipped*, not filed into Manual Review a second time. Otherwise one critical result becomes two review tasks, and the second can be worked without the context of the first. If identity is uncertain (shared ID but a different patient, DOB or category, identical timestamps, or a resend marker with no matching original), the fax goes to review. If the original itself failed to route, its resend goes to review instead of being silently skipped.

## Safety posture

- No clinical inference. The prompt forbids inferring urgency from values. `decide` only sees literal `explicit_flags` strings, and the review note quotes them with their page.
- Negated statements ("No urgent flag provided") go into a separate field. The Tesseract check is negation-aware, so it does not escalate on them, and it does escalate when vision missed an affirmative marker.
- When uncertain, route to Manual Review, and every review carries a reason note. The perturbation probe (`eval/perturb.py`) checks that damaged PDFs fail closed.

## Known gaps (honest)

- **Notes cannot be verified in the UI.** The portal never renders them. They are recorded as `note_sent` with `note_ui_verifiable: false`.
- **Dedup depends on printed IDs plus name and DOB.** A resend with a re-keyed ID and no "original ID" reference would be routed twice, so this needs a content-similarity check.
- **Single worker, sequential.** About 28 s for 12 faxes in replay, plus about 5 s of vision per fax live.
- **Vision is nondeterministic.** This is mitigated by schema-constrained output, a deterministic decider and the sha-keyed cache, but not measured across repeated live runs.
- **Replay ≠ live accuracy.** Replay proves the browser and decision layers are deterministic (0 diffs across two fresh-portal runs). Only `make run` re-measures perception.
- **Labels are the candidate's own.** n=12 is a smoke test, not an accuracy estimate.
- The probe shows the decider fails closed on *reported* damage. Whether the live model reports a blank or dropped page needs `eval.perturb --mode vision` with a key.

## Production at Linda

- **Selector-drift canaries.** A scheduled run against a canary fax checks every locator and the matching invariant (h1 + Fax ID). On drift it alerts and stops routing, falling back to queue-only, rather than mis-clicking.
- **Per-clinic SOP config.** The SOP table and dropdown labels become versioned per-tenant config, validated against the live dropdown options at startup. The eval's independent SOP oracle is the model for this check.
- **HITL corrections become labels.** Every Manual Review resolution and every human re-route is captured as a labelled example. These form a regression set that gates prompt, model and rule changes using the metrics here. Unsafe auto-routes must stay at 0 and over-review is tracked as a cost.
- **Concurrency.** A queue of fax IDs with one lease per fax replaces the global lock. The idempotency key goes on the intent, and Session B pools respect the portal's session limits.
- **Model fallback.** The cheaper model goes first, escalating to Opus on low confidence or a Tesseract contradiction. Outages fail closed to review rather than stalling.
- **PHI handling.** Logs are redacted or hashed as here. PDFs stay encrypted at rest with a TTL, the model provider is under a BAA with zero data retention, and traces are access-controlled because they contain page images.
- **Cost at volume.** At the measured **$0.034 per fax** (Opus, about 6.2k input tokens per fax), 10k faxes a day costs about $340 a day. Prompt caching of the system prompt, a cheaper first-pass model and skipping resends identified by sha before vision would cut that several-fold. Latency (p50 about 5 s per fax) is not the bottleneck, since the portal round-trips are under 1 s per stage.
