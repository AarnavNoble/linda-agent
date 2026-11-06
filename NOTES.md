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
| Vision unavailable / refusal / wrong page count | No extraction | Not decided: the fax (and every later-received fax, which could be its resend) is held as `failed/not_processed` and retried next run. Never routed unread, never guessed |
| Bare error page (proxy 502), inbox page failure | No portal navigation | Re-enter through the base URL (the same entry point as login) and retry; discovery retries in a fresh Session A |

## Rule precedence

The first matching rule wins: already routed → duplicate (uncertain resend → review) → extraction unavailable / wrong document → explicit urgent flag → mixed categories → incomplete (explicit statement, page-marker gap, a positional reference to a page/table the packet does not contain, a cover-sheet enclosure beyond the last page) / illegible → unclear or low confidence → Tesseract contradiction → SOP row.

**Why duplicates come before exceptions.** A resend of an urgent original must be *skipped*, not filed into Manual Review a second time. Otherwise one critical result becomes two review tasks, and the second can be worked without the context of the first. If identity is uncertain (shared ID but a different patient, DOB or category, identical timestamps, or a resend marker with no matching original), the fax goes to review. If the original itself failed to route, its resend goes to review instead of being silently skipped. A resend that carries an urgency marker its original lacked is new information, so it goes to review rather than being skipped.

## The FX-208 call (and why it is not fitted to one fax)

FX-208 is a one-page lab report ("Page 1 of 1") that says *"Results: See values on following table"*; no table follows. On the first live run it went to review because the model filed that sentence as a completeness issue, while equivalent wording on FX-203/FX-211 ("See full radiology findings") was not. I first fixed the *inconsistency* by splitting `references_to_absent_content` from explicit `completeness_issues` (prompt v4), which auto-routed it. Review then pushed back, correctly: a lab result whose values are not in the transmission is the SOP's "important pages are missing" case. The final rule is general and deterministic, in `decide.py`, not in the prompt: only **positional** references count (following/next page or table, "page N", "continued"), and they are checked against the pages actually present. References to other documents ("see full report") and vague wording ("attached details") are not evidence of a missing page. On the 12 faxes this changes FX-208 only; FX-201 ("follows on the next page", page 2 present) and FX-206 ("included on page 2", present) still route. The cost is a possible false review when a table sits lower on the same page, which the vision pass would normally capture as values.

## Safety posture

- No clinical inference. The prompt forbids inferring urgency from values. `decide` only sees literal `explicit_flags` strings, and the review note quotes them with their page.
- Negated statements ("No urgent flag provided") go into a separate field. The Tesseract check is negation-aware, so it does not escalate on them, and it does escalate when vision missed an affirmative marker.
- When uncertain, route to Manual Review, and every review carries a reason note. The perturbation probe (`eval/perturb.py`) checks that damaged PDFs fail closed.

## Known gaps (honest)

- **Notes cannot be verified in the UI.** The portal never renders them. They are recorded as `note_sent` with `note_ui_verifiable: false`.
- **Dedup depends on printed IDs plus name and DOB.** A resend with a re-keyed ID and no "original ID" reference would be routed twice, so this needs a content-similarity check.
- **Single worker, sequential.** About 25 s for 12 faxes in replay, plus about 5 s of vision per fax live.
- **Vision is nondeterministic.** Mitigated by schema-constrained output, a deterministic decider and the sha-keyed cache. Measured on this inbox: two fresh live Opus runs gave identical decisions with 0 field diffs ([`eval/model_compare.md`](eval/model_compare.md)); two runs on 12 faxes is evidence of stability, not a guarantee.
- **Replay ≠ live accuracy.** Replay proves the browser and decision layers are deterministic (0 diffs across two fresh-portal runs). Only `make run` re-measures perception.
- **Labels are mine, and small.** I read every fax and wrote blind notes; 5 of the 12 labels, where my note was unsure or misread a definition, were settled by applying the SOP with AI assistance and are marked `resolved:` in [`eval/labels.yaml`](eval/labels.yaml). n=12 is a smoke test, not an accuracy estimate.
- **Holding is conservative.** One fax that cannot be downloaded or read holds every later-received fax for that run (they could be its resend). Nothing irreversible happens, and the next run converges, but one broken fax delays the rest of the inbox.
- The live damage probe ([`eval/perturbation_live.md`](eval/perturbation_live.md)) shows the model reported every blanked or dropped page and none of the 12 variants auto-routed. That is 12 variants of 6 faxes, not a guarantee.

## Production at Linda

- **Selector drift.** A canary run checks every locator and the matching invariant before routing; on drift, stop routing and queue for humans instead of mis-clicking.
- **Per-clinic SOP as config,** validated against the live dropdown options at startup; every human correction from Manual Review becomes a labelled regression example gating prompt/model/rule changes (unsafe auto-routes must stay 0; over-review tracked as cost).
- **Concurrency.** Replace the global lock with a per-fax lease queue keyed by the intent's idempotency key; respect portal session limits.
- **Cost.** Measured $0.034/fax on Opus 5.5. A cheaper first pass escalating on low confidence or a Tesseract contradiction, plus skipping byte-identical resends by sha, would cut it several-fold; the model comparison in `eval/` is the evidence to make that call.
