# Failure log

These are the real bugs hit while building and hardening the agent. Each entry gives the
symptom, the root cause, the fix, and the test or chaos case that now pins it. "Chaos"
means `uv run python -m harness.chaos` (matrix in `results/chaos_matrix.md`). Every chaos case
judges outcomes only through the portal UI (in a separate read-only browser session) and
through the agent's own `results.json` / `events.jsonl`.

When a fix is pinned by a chaos case, I checked that the case really catches the bug: I put
the old code back and confirmed the case fails. Those runs are quoted below.

---

## 1. Download listener crashed with `AttributeError`

- **Symptom.** The first download attempt crashed inside Playwright with an `AttributeError`
  before any file arrived.
- **Root cause.** `page.on("download", got.append)` passed a bound builtin method as the
  handler. Playwright tags handler callables with an attribute so that `remove_listener` can
  find them later, and a builtin method cannot take attributes.
- **Fix.** The handler is now a real nested function (`handler(d)`) in
  `agent/acquire.py::_trigger_download`, with a comment saying why.
- **Pinned by.** Every chaos case downloads all 12 faxes through this path.
  `download_503_once` also exercises the listener's add/remove cycle across a retry.

## 2. FX-208 sent to review because of a vague phrase the model filed as "incomplete"

- **Symptom.** In the first live run, FX-208 (a complete lab result) went to Needs Review.
  FX-203 and FX-211 have the same kind of wording ("see full report") and were not flagged.
- **Root cause.** The vision schema had only one field, `completeness_issues`. The model
  put "See values on following table" there for FX-208 but left out similar phrases on the
  imaging reports. Pointing to content outside the packet is not the same as the packet being
  incomplete. The footer's `Page x of y` markers are what tell us whether the packet is
  complete.
- **Fix.** Prompt/schema v4 adds a separate `references_to_absent_content` field.
  `decide.py` reports it but does not send the fax to review because of it. Only explicit
  incompleteness or a gap in the page markers triggers review. The v4 extraction cache is
  committed so that replay reproduces this.
- **Pinned by.** `tests/test_decide.py::test_reference_to_absent_content_alone_is_not_incomplete`.

## 3. Re-login stranded the tab on `/login` when the expired request was a download

- **Symptom.** If Session A's login expired between opening a fax and clicking **Download
  Fax PDF**, the fax was given up with `login failed: still on /login (bad credentials?)`,
  even though the credentials were fine.
- **Root cause.** The portal sends an expired request to `/login?next=/faxes/X/download`.
  After a successful sign-in it redirects to `next`. That URL is an attachment, so the
  browser downloads it and the tab never leaves `/login`. `ensure_auth` treated "still on
  /login" as failed credentials. Even if it hadn't, the tab would have been stuck on a page
  with no navigation, and `page.goto` is forbidden.
- **Fix** (`agent/browser.py::ensure_auth`). When the hidden `next` field points at a
  `/download` URL, it is pointed at the fax page instead. The caller then triggers the
  download again through the visible button. A login now counts as failed only when the
  portal says so (`error=1`).
- **Pinned by.** Chaos case `download_reauth`. It drops the session cookie right after the
  fax page loads, which looks the same to the browser as an expired login, and then asserts
  three things: the agent re-authenticated, got a valid PDF of the same fax, and is not left
  on `/login`. With the old `browser.py` restored, this case fails all 3 checks with the error
  quoted above.
  Also pinned by `tests/test_regressions.py::test_download_reauthenticates_after_session_expiry`
  (`goto_fax`, `context.clear_cookies()`, `download_fax` against a private portal).

## 4. `results.json` attributed downloads to the wrong portal session

- **Symptom.** In the `reset_mid_run_409` chaos run, 11 faxes reported
  `download_session_a: ["QlUcwwHJ"]`, but their `downloaded` events show they were downloaded
  in `Yt5X-pA3`. That breaks the per-fax evidence for the two-session rule.
- **Root cause.** `build_result` reported `self.sessions_a[-1]`, which is the Session A the
  run opened last. It is not the portal session the fax's download actually happened in. A
  re-login inside the same browser context (the portal session vanished) changes the session
  without opening a new context, so the value went stale.
- **Fix.** `acquire` saves `download_session` for each fax at download time (a new
  checkpoint column, added with an `ALTER TABLE` migration so older checkpoints still
  resume). `report.py` reports that value.
- **Pinned by.** A check that runs in every chaos case: "results attribute each download to
  the session it happened in". The first version of the suite passed with this bug present,
  so the check was added afterwards. With the old runner, `download_gives_up` fails this check
  for every fax.

## 5. A fax that could not be downloaded was still decided and routed, and so was its resend

- **Symptom.** In a run where the earliest original (FX-201) could not be downloaded:
  - FX-201 was still decided as "Needs Review: content could not be extracted" and sent to the
    router. It hit 409 three times, because no download existed, and ended `failed/unverified`.
  - FX-210, its resend, was routed as if it were the original. One source document then had
    the wrong fax routed and no skipped duplicate.
  - The run exited 0.
- **Root cause.** `Runner.decide` called `decide_all` for every record, including records
  with no PDF. So the runner's `not_processed` branch was never reached. Duplicate detection
  cannot see a resend when the original's content is unknown.
- **Fix** (`runner.py::decide`).
  - A fax with no downloaded content is not decided (`not_decided_no_content`).
  - Every later-received New fax is held for this run (`decision_held`), because any of them
    could be its resend. The portal is not touched for any of them, and they are reported
    `failed/not_processed` with the reason.
  - The run exits 1 (`completed_with_failures`).
- **Pinned by.** Chaos case `download_gives_up`. A harness-only wrapper makes every download
  of the earliest New fax give up in run 1. The case checks that nothing unread or possibly a
  resend is submitted, and that run 2 converges to the baseline. With the old runner it fails
  8 checks: `submitted=['FX-202', ..., 'FX-210', 'FX-212']`, `result=failed/unverified`,
  `rc=0`.

## 6. `failed` was terminal forever, even when the agent never acted

- **Symptom.** After a run where a fax failed only because its download gave up, rerunning
  the agent never retried it. The fax stayed `failed` until someone ran `--fresh`.
- **Root cause.** A `failed` result advances the fax to `DONE`, and `reconcile` had no case
  for retrying it.
- **Fix.** `reconcile` reopens `failed` results whose verification is `not_processed` or
  `unverified` (`reconcile_retry_failed`). If an intent already exists, the fax goes back to
  `INTENT`, so routing still re-reads the UI before any submit. Conflicts and rejected values
  stay terminal, because a human must look at those.
- **Pinned by.** Chaos case `download_gives_up`, run 2: "run 2 retried every failed fax" and
  "re-reported exactly the faxes whose outcome changed". With the old runner: `retried=[]`.

## 7. One Playwright error aborted the whole inbox

- **Symptom.** A Playwright `TimeoutError` (for example, clicking **Route / Assign Fax** when
  the portal doesn't render it) propagated out of `Runner.run`. The browser closed, the
  remaining faxes were never processed, `results.json` was not written, and the run was never
  marked finished. A mutated agent run in the chaos suite showed this:
  `PortalError: TimeoutError: Locator.click: Timeout 15000ms exceeded`.
- **Root cause.** The per-fax handlers caught only `PortalError` and `DownloadFailed`.
  Playwright's own `Error` / `TimeoutError` are not subclasses of those.
- **Fix.** `FAX_ERRORS = (PortalError, playwright Error)` is caught per fax in `acquire`, in
  `route_fax` and in the main loop. A fax that errors gets another pass in the same run, up
  to 3 passes. Its checkpoint stays at `INTENT` / `SUBMITTED_UNVERIFIED`, so the retry
  re-reads the UI before acting. A universal chaos check asserts "no unhandled per-fax
  errors".
- **Pinned by.** No portal fault produces a Playwright error deterministically, so the chaos
  suite's closest scenario is `reset_mid_run_409`.
  Pinned by `tests/test_regressions.py::test_transient_playwright_error_on_one_fax_does_not_sink_the_run`:
  it makes `read_status` raise a Playwright `TimeoutError` once for one fax, then asserts the
  run exits 0 with all 12 results and that fax verified on the next pass.

## 8. A second worker on the same work dir crashed with a traceback

- **Symptom.** Starting the agent while another run held `state/agent.lock` printed a raw
  `LockHeld` traceback and exited 1, which looks the same as a real crash.
- **Root cause.** `run_lock` raised correctly, but nothing caught the exception.
- **Fix.** `Runner.run` catches `LockHeld`, prints one line, and exits **75** (`EX_TEMPFAIL`).
  The lock is taken before `start_run`, so the refused worker leaves no run row and no events.
- **Pinned by.** Chaos case `baseline+lock`. While the first worker is running, it asserts the
  second worker exits 75 within 30 s, the first is unaffected, and only one run appears in the
  events. With the old runner: `rc=1 after 0.9s`.

## 9. `run_id` collision on a same-second restart (found by code review)

- **Symptom (latent).** `run_id` had one-second resolution. It is the primary key of the
  `runs` table and the name of the artifacts directory. A crashed run restarted within the
  same second would fail `INSERT INTO runs` with an `IntegrityError` and write into the
  crashed run's `events.jsonl`.
- **Fix.** `run_id` gets a `-<pid>` suffix.
- **Pinned by.** The kill/resume chaos cases restart immediately after a hard exit. A
  same-second collision is timing-dependent, so no case pins it deterministically.

## 10. A crash during the write could truncate `results.json` (found by code review)

- **Fix.** `results.json` is written to a temp file, fsynced, then moved into place with
  `os.replace`. The `reported` flags are set only after that. A crash in between causes a
  fax to be reported again (at-least-once), never lost.

---

## 11. OCR negation leaked across fields and hid a real urgency flag (found by the test suite)

- **Symptom:** `affirmative_flags("Fasting: No    STAT: Yes")` and `("Without delay - ASAP")` returned `[]`,
  so the Tesseract safety cross-check would miss a genuine STAT/ASAP marker on form-style lines.
- **Root cause:** the negation look-back scanned up to 20 characters before the marker regardless of
  field or clause boundaries, so a "No" belonging to an earlier field negated a later one.
- **Fix:** split the text before the marker on clause/field separators (`: ; , .`, ` - `, runs of spaces)
  and only treat it as negated if a negation word sits in the same clause within four words
  (`agent/extract/tesseract.py`). "No urgent flag", "Non-urgent" and "Not marked urgent or critical" stay negated.
- **Pinned by:** `tests/test_units.py::test_affirmative_flags_negation_does_not_leak_across_fields` and
  `test_affirmative_flags_negation_within_clause_still_suppresses`.

## 12. Console log line printed patient identifiers unredacted (found by the test suite)

- **Symptom:** `EventLog.emit(..., patient_name=...)` hashed the value in `events.jsonl` but printed it raw
  in the stderr summary line. Latent (no caller passed PHI at top level), but the contract was file-only.
- **Fix:** build both the JSONL record and the console line from the same redacted dict (`agent/events.py`).
- **Pinned by:** `tests/test_units.py::test_event_log_console_line_is_redacted`.

## 13. Footer check compared raw text, so a cheaper model's transcription looked like a wrong document (found by the model comparison)

- **Symptom:** in `eval/model_compare.py`, Haiku 5.5 sent FX-205/206/207 to review: "footer transmission
  TRANSMISSION FX-205 does not match portal fax FX-205".
- **Root cause:** the wrong-document check compared the footer field as a raw string. Opus happened to return
  just the ID; Haiku copied the label too. The invariant is about the *ID*, not the transcription.
- **Fix:** extract `FX-\d+` IDs from the footer field and require the portal fax ID among them (`agent/decide.py`).
  Haiku then agrees with the reference on 12/12 decisions.
- **Pinned by:** `tests/test_decide.py::test_footer_id_is_compared_as_an_id_not_as_raw_text`. Lesson: running
  the eval across models surfaced a brittleness in the *deterministic* layer that a single model hid.

## Fault handling that was verified and needed no fix

Each of these is asserted in the chaos matrix through events and the UI:

- **`download_503_once`.** The 503 HTML page is detected through the tab's URL (no 10 s
  timeout). The agent goes back to the fax through the inbox search, retries, and validates
  the PDF by its `%PDF-` header and portal page count. Exactly one error page occurs and
  nothing invalid is accepted.
- **`session_expire_on_route_once`.** The submit 303 goes to `/login`. The agent re-logs in
  inside the **same** Session B browser context: one B context, two portal sessions. It
  re-reads the status (still New), refills, submits once more, and verifies.
- **`route_commit_then_503_once`.** The 503 is classified as `unknown`. The agent re-reads the
  fax page and Routed Documents, finds the route committed with the intended values, and
  records `ui_verified`. Exactly one submit is made.
- **Kill / resume** at `after_download`, `before_click`, `after_click` and `after_click#4`.
  Across runs:
  - the Routed Documents table has exactly one correct row per fax;
  - there is no submit after a submit that may have committed;
  - each fax is reported exactly once;
  - a third run does nothing.
- **Sandbox reset under a stale checkpoint.** `reconcile` notices that every fax the agent had
  routed is New again. It re-downloads those faxes in a fresh Session A (the old downloads are
  gone, so this is the only way to avoid a 409), routes each once, and re-reports only those.
- **Genuine 409.** The sandbox is reset after the first download, which deletes that download.
  The agent's route gets a 409. It re-downloads in a new Session A, then routes from a new
  Session B. Session A's login, which also vanished, is re-established silently.

## Known gaps

- Holding back every later fax after one permanent download failure is conservative on
  purpose: no irreversible route is made while a resend can't be ruled out. The cost is that
  one broken fax blocks the rest of that run's inbox until the next run.
- Report-once is at-least-once if the process dies between writing `results.json` and setting
  the `reported` flags. The window is milliseconds.
