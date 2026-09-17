# 020 — Cerebras 429 → ledger reconciliation (hermetic)

## 1. Intent

Charter backlog item **B-6** ("429 → ledger reconciliation, never seen against
the live API") is half-discharged: mid-run reconciliation for Groq ships
(Iteration 9 T6), proven against a captured 429 body rather than a forced live
refusal, because forcing one costs roughly 196,552 tokens "to watch an error
handler work" (`specs/000-project.md` §8). That half stays open by design and
is not this iteration's concern.

This iteration asks the same question B-6 already asked for Groq, but for
**Cerebras**, added as a second provider in Iteration 13
(`016-second-llm-provider.md`) with its own header-parsing path
(`snapshot_from_cerebras_headers`) but, as far as this session's inspection
found, **zero reconciliation coverage**. `evals/ledger.py`'s reconciliation
path — `limit_from_message`, `used_tokens_from_refusals`,
`reconcile_from_refusals` — has only ever been built and tested against Groq's
captured body. Whether it recognizes anything Cerebras would actually send is
untested and, on the evidence gathered below, doubtful.

**Non-goals, stated up front:**
- No live Cerebras call is made or forced, for the same reason B-6's Groq live
  leg stays opportunistic — this project does not spend quota to manufacture a
  refusal on purpose.
- No change to Groq's reconciliation path or its tests.
- No change to which provider serves production traffic (stays Groq, per D-E
  of `016-second-llm-provider.md`).
- No frontend work. This session separately confirmed `/quota`,
  `CATEGORY_RATE_LIMITED`'s HTTP mapping, and the low-bucket banner in
  `api/web/app.js` already ship and already cover both providers via the
  shared `Bucket`/`RateLimitSnapshot` dataclasses in `api/llm/rate_limits.py`
  — that is charter item B-1, discharged 2026-09-04, not this iteration.

## 2. Measurements

**Groq's captured 429 body**, the only fixture reconciliation has ever been
tested against (`REAL_429`, `tests/test_rate_limit_telemetry.py`):

```
Error code: 429 - ... on tokens per day (TPD): Limit 200000, Used 199301, Requested 1279 ...
```

Plain text. `limit_from_message`'s regex (`api/llm/rate_limits.py`,
`_LIMIT_NAMED`) matches exactly this shape: `\(CODE\): Limit N, Used M`.

**Cerebras's only measured error envelope**, from a live call made in
Iteration 13 (`016-second-llm-provider.md` §2.1) — a 402, not a 429, because
that test account had no billing configured:

```json
{"message": "Payment required to access this resource. Visit your billing tab.",
 "type": "payment_required_error", "param": "quota", "code": "payment_required"}
```

This is OpenAI-compatible JSON — structurally different from Groq's plain
text. `016` §2.1 notes this envelope shape "matches the OpenAI-compatible
error format Cerebras's API otherwise documents," which is some evidence about
what a 429 from the same API would look like, but it is evidence about the
**402** case specifically, not a measurement of the 429 case.

**Cerebras's real 429 body remains unmeasured.** `016` §2.4 already flagged
Cerebras's rate-limit *headers* as "doc-sourced, third-party, lower
confidence," to be "re-measured against a real 429 as soon as billing
unblocks it" — nothing in this session's inspection shows that has happened.
The body text of an actual Cerebras rate-limit refusal (as opposed to the
402 payment error) has never been seen by this project.

**No test coverage exists.** A search of `tests/` for `Cerebras` alongside
`TPD`, `Limit`, or `reconcile` returns nothing — `test_daily_quota_guards.py`'s
B-6 section (lines 426–520) and `test_rate_limit_telemetry.py`'s `REAL_429`
constant are Groq-only.

**What is not yet known, and this spec cannot manufacture it without a live
call:** whether `limit_from_message` would match a real Cerebras 429 body. §3
below scopes acceptance criteria around what *can* be established hermetically
— whether the parser matches the best evidence currently available — not
around a claim of true Cerebras coverage, which would require the live call
this iteration explicitly declines to make.

## 3. Acceptance criteria

- **AC1**: A hermetic test fixture represents a plausible Cerebras daily-limit
  refusal, built from the one real measurement available (the 402 envelope
  shape in §2), and is clearly labeled as an assumption about the 429 case
  rather than a measurement of it.
- **AC2**: A test proves, one way or the other, whether
  `used_tokens_from_refusals`/`limit_from_message` currently recognizes that
  fixture. This must go red first if the answer is "no," per the charter's
  mutation-testing discipline — a test that was never shown to fail proves
  nothing.
- **AC3**: The outcome is acted on, not left open-ended — resolved per §7's
  open questions, either by defensively widening `limit_from_message` to also
  parse an OpenAI-compatible JSON body, or by explicitly filing the gap as new
  charter debt alongside B-6.
- **AC4**: Groq's existing reconciliation tests
  (`tests/test_daily_quota_guards.py`, `tests/test_rate_limit_telemetry.py`)
  pass unchanged — this iteration is additive to the Cerebras path only.
- **AC5**: `specs/000-project.md` §8 and `HANDOFF.md` are updated to state the
  true, measured outcome — including, explicitly, that Cerebras's real 429
  body remains unmeasured regardless of what this iteration decides, so a
  future reader does not mistake "the parser handles our best guess" for "this
  was verified against Cerebras."

## 4. Contracts this iteration must not break

- `LLMProvider.complete(system, user) -> str` stays one method. Nothing here
  touches the provider protocol.
- `last_rate_limit` stays a best-effort attribute read via `getattr`, exactly
  as `api/llm/rate_limits.py` already documents.
- `limit_from_message` stays what `003`/`B-1` already established: text
  matching used for diagnostics, never for control flow. Widening it (if
  Q-B below resolves that way) must not turn it into something branched on
  for anything but reconciliation, and a miss must still cost only "one line
  of diagnostics," per the existing docstring's own standard.

## 5. Verification

- All new tests are hermetic: no `needs_db`, no live provider call, run via
  `.venv/Scripts/python.exe -m pytest tests/test_daily_quota_guards.py tests/test_rate_limit_telemetry.py -q`.
- If the resolution is to widen `limit_from_message`: mutation-test by
  reverting the widening and confirming the new Cerebras test goes red, per
  the charter's mandatory mutation-testing rule.
- Full suite (`.venv/Scripts/python.exe -m pytest -m "not needs_db"`) run once
  at the end to confirm nothing else regressed.
- No live Cerebras or Groq call is made at any point in this iteration.

## 6. Decisions — RESOLVED 2026-09-17

- **Q-A — Build the fixture from the measured 402 envelope shape.** Modeled on
  the OpenAI-compatible JSON envelope, explicitly documented in the fixture and
  its test as doc-sourced/unverified for the 429 case specifically — it
  describes the 402 that was actually measured, not a Cerebras rate-limit
  refusal anyone has seen.
- **Q-B — Widen `limit_from_message` (option i).** Defensively extend it in
  `api/llm/rate_limits.py` to also parse the JSON envelope, additive only. The
  existing Groq plain-text path stays untouched and its tests stay green
  unmodified; the widening is mutation-verified (reverted, new Cerebras test
  confirmed red, restored).
- **Q-C — Account status: still 402, unconfigured.** Confirmed no billing has
  been added since `016` §2.1. This iteration stays strictly hermetic — no
  live probe, no live call, to either provider.

<details>
<summary>Original framing of all three, kept for the record</summary>

### Open questions

- **Q-A**: Build the hermetic fixture from the measured 402 envelope shape
  (JSON, OpenAI-compatible), explicitly labeled as an assumption about the 429
  case specifically? *Lean: yes* — it is the only real evidence available, and
  inventing a plain-text body instead would have zero basis at all.

- **Q-B**: If the test shows the parser does not recognize that shape, should
  this iteration (i) widen `limit_from_message` defensively to also parse an
  OpenAI-compatible JSON body, additive and without touching the Groq path, or
  (ii) document the gap as new backlog debt next to B-6, unfixed, on the
  reasoning that fixing blind against a body that has never actually been seen
  risks a false sense of coverage? *Lean: (i)*, built defensively and proven
  not to weaken the existing Groq-path tests — the fix is cheap and additive,
  and the cost of being wrong is bounded (the parser already tolerates
  mismatches by design, returning `None` rather than raising), whereas the
  cost of (ii) is indefinite silent non-coverage if a real Cerebras TPD
  refusal ever does arrive in the shape this spec predicts.

- **Q-C**: Is the Cerebras test account's billing status still unconfigured
  (still returning 402, per `016` §2.1) as of today? This costs nothing to
  answer and is informational only — it does not change this iteration's
  scope either way, since no live call is planned regardless, but it would
  tell us whether a future, separately-scoped iteration could even attempt a
  live measurement without first solving a billing problem.

</details>
