# 020 — Plan: Cerebras 429 → ledger reconciliation (hermetic)

## 1. Approach

Build two hermetic fixtures modeled on the one real measurement available —
Cerebras's 402 envelope (`016-second-llm-provider.md` §2.1) — and test
`limit_from_message`/`used_tokens_from_refusals` against both, per the
resolved Q-A/Q-B/Q-C in `020-cerebras-429-reconciliation.md` §6.

A local, hermetic check run while writing this plan (no network call — pure
regex against literal strings) found the actual shape of the problem is
narrower than Q-B's original framing assumed, and that finding drives the
task list below. See D-1.

## 2. The two fixtures

**Fixture A — JSON envelope, Groq-style wording embedded in `message`.**
Same structure as the measured 402 (`message`/`type`/`param`/`code`), with a
`message` value that happens to phrase the numbers the way Groq's body does:

```json
{"message": "Rate limit exceeded: on tokens per day (TPD): Limit 1000000, Used 999500, Requested 1200.",
 "type": "rate_limit_error", "param": "quota", "code": "rate_limit_exceeded"}
```

Verified locally: `limit_from_message()` on this string, and on the same
string wrapped the way `GroqProvider._safe_message`/`CerebrasProvider
._safe_message` render an exception (`"RateLimitError: Error code: 429 - " +
body`), **already returns `("TPD", 1000000, 999500)` with no code change**.
`_LIMIT_NAMED.search()` matches anywhere in a string; it does not care
whether the substring sits inside JSON, a Python dict repr, or plain text.

**Fixture B — JSON envelope, generic message, no embedded numbers.** The
shape the one real measurement (the 402) actually has, adapted to a
rate-limit `type`/`code`:

```json
{"message": "Rate limit exceeded for requests-per-day.",
 "type": "rate_limit_error", "param": "quota", "code": "rate_limit_exceeded"}
```

Verified locally: `limit_from_message()` on this returns `None`. There is no
number anywhere in the string for any regex to find.

Both fixtures are labeled in the test file as doc-sourced/unverified for the
429 case specifically (Q-A), exactly as `REAL_429` documents its own Groq
provenance.

## 3. D-1 — Decision surfaced while planning, not one of the spec's own open questions

Q-B's resolution was "widen `limit_from_message` ... if needed." The
fixtures above show the "if needed" splits into two outcomes rather than one:

- Fixture A needs no widening — it already passes.
- Fixture B **cannot be fixed by widening the text parser**, because there is
  no numeric information anywhere in the string to extract. A regex can be
  made more tolerant of punctuation it has never seen, but it cannot invent a
  daily-usage figure Cerebras never sent. Guessing at hypothetical
  formatting variants (a colon after "Limit", comma-grouped digits, a
  different field name) to make a fixture pass would mean tuning code
  against fixtures this plan itself invented with zero evidentiary basis —
  the exact shape of untrustworthy work `016 §2.4` already warned against
  ("doc-sourced, third-party, lower confidence ... written defensively
  rather than trusted at face value").

Per the standing rule that a plan surfacing its own decision still gets a
lean rather than a silent resolution (this is not one of the spec's
pre-answered questions — it is new information the plan uncovered):

*My lean:* make **no regex change**. Ship both fixtures as tests that
document the true, measured boundary — Fixture A proves the existing
text-matching approach already generalizes past plain-text bodies for free;
Fixture B proves, and documents, that a numberless refusal is unreconcilable
by construction, and files that as new backlog debt next to B-6 rather than
papering over it with an untested guess. This keeps faith with AC3 ("acted
on, not left open-ended") without violating the project's own
never-fabricate standard, and it costs nothing in quota or invented
complexity.

**If the user prefers to still add defensive punctuation tolerance** (e.g.
`Limit:\s*` with an optional colon, comma-grouped digits via
`[\d,]+`) as cheap insurance against a minor formatting difference — a
strictly smaller ask than the original "parse a JSON envelope" framing —
that is a mechanical five-line regex change or ships in the same task; it is
called out separately in the task table below rather than assumed.

## 4. Files and decomposition

| # | Task | Files | Depends on |
|---|---|---|---|
| T1 | Add Fixtures A and B (doc-sourced/unverified, labeled per Q-A) next to `REAL_429` | `tests/test_rate_limit_telemetry.py` | — |
| T2 | Test proving Fixture A already reconciles via `used_tokens_from_refusals` with **no production code change** (documents the free generalization found in §2) | `tests/test_daily_quota_guards.py` | T1 |
| T3 | Test proving Fixture B does **not** reconcile, and never can without invented data — the negative case, which per D-1 is the one that matters | `tests/test_daily_quota_guards.py` | T1 |
| T4 | *(only if the punctuation-tolerance variant in D-1 is wanted)* widen `_LIMIT_NAMED` for optional colon / comma-grouped digits; add a fixture proving the old Groq body and Fixture A still match unchanged (mutation check: revert, confirm the new tolerance test goes red, restore) | `api/llm/rate_limits.py`, `tests/test_rate_limit_telemetry.py` | T1–T3 |
| T5 | Update `specs/000-project.md` §8 (new debt entry next to B-6 for Fixture B's finding) and `HANDOFF.md`, stating plainly that Cerebras's real 429 body remains unmeasured regardless of T1–T4's outcome | T2, T3, (T4) |
| T6 | `.venv/Scripts/python.exe -m pytest tests/test_daily_quota_guards.py tests/test_rate_limit_telemetry.py -q`, then full `-m "not needs_db"` suite | T5 |

T1–T3 and T5–T6 proceed regardless of the D-1 lean. T4 is conditional on the
user's answer to D-1's second paragraph.

## 5. Test plan / mutation checks

- T2 and T3 are the acceptance test for AC1/AC2: both must be shown to
  reflect real behavior, not assumed behavior — run once before any other
  change to confirm A passes and B fails exactly as verified by hand in §2.
- If T4 ships: mutation-test by reverting the regex change and confirming the
  new punctuation-tolerance test goes red, then restoring it — the charter's
  standard mutation discipline, applied to the one piece of new production
  code this plan might add.
- AC4 (Groq unaffected): run `tests/test_daily_quota_guards.py`'s existing
  B-6 section and `tests/test_rate_limit_telemetry.py`'s existing Groq tests
  unchanged; they must stay green throughout.
- No live call to either provider at any point (Q-C).

## 6. Risks

- If the user chooses T4, the added tolerance is still speculative — it
  guards against formatting variants nobody has observed, not ones that are
  known to exist. The risk is scoped and named rather than hidden: T4's own
  test proves only that the *guessed* variant now matches, not that it
  matches anything Cerebras actually sends.
- Fixture B's finding becomes a new, permanent entry in `specs/000-project.md`
  §8 debt table (T5) rather than a closed row — consistent with how B-6 itself
  is recorded, but it does mean this iteration closes with one more named
  debt item than it started with, not fewer.
