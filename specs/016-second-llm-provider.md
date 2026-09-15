# 016 — Iteration 13: Second LLM provider (Cerebras)

## 1. What this iteration is for

Backlog item B-4 has been deferred since the close of Iteration 5, when the
user shelved a provider switch as "an independent architectural milestone
with its own re-baselining protocol." Two reasons were on record: a
model/provider change retires every `EVALS.md` number at once, and the
original motivation — escaping Groq's free-tier ceiling — was diagnosed as a
per-minute burst limit rather than daily exhaustion, so a bigger daily bucket
elsewhere might not even have fixed the real problem.

This iteration opens that milestone. It adds **Cerebras** as a second
`LLMProvider` behind the existing one-method `complete(system, user) -> str`
contract (`api/llm/base.py`), proves the swappable-provider commitment holds
for a real second vendor, and records one comparable eval run against it.
It deliberately does **not** switch default or production traffic — that
stays Groq, per decision D-E below.

## 2. Measurements

### 2.1 The probe account cannot complete a chat call today — 402, not a rate limit

A live call was made against `https://api.cerebras.ai/v1/chat/completions`
using the `CEREBRAS_API_KEY` now in `.env`, with a QueryPilot-shaped
system/user prompt pair. It returned:

```
status: 402
body: {"message": "Payment required to access this resource. Visit your
       billing tab.", "type": "payment_required_error",
       "param": "quota", "code": "payment_required"}
headers of note: x-should-retry: false
```

No `x-ratelimit-*` header appeared on this response — the request was
rejected before reaching the rate-limit layer. This account needs a payment
method added before any further live measurement (usage-object shape, a
real 429, real rate-limit headers) is possible. See Risk in §7.

The error envelope shape (`message` / `type` / `param` / `code`) is the one
piece of real, measured data this call yielded, and it matches the
OpenAI-compatible error format Cerebras's API otherwise documents.

### 2.2 What did work: `GET /v1/models` — real, measured model ids

```
status: 200
model ids: ["qwen-3.8-27b", "gpt-oss-120b"]
```

These are the two models this account can address today; no
`x-ratelimit-*` header appeared here either (a plain, unmetered listing
call).

### 2.3 Doc-sourced free-tier limits (official docs, unverified by call)

From `inference-docs.cerebras.ai/support/rate-limits`, for both models
listed above:

| limit | capacity |
|---|---|
| requests per minute | 5 |
| uncached tokens per minute | 30,000 |
| total tokens per minute | 90,000 |
| tokens per hour | 1,000,000 |
| **tokens per day** | **1,000,000** |

This is Cerebras's own published page, but it was not corroborated by a
live 429 from this account (blocked by the 402 in §2.1), so it is
doc-sourced, not measured, and should be re-verified once billing is added.

### 2.4 Doc-sourced header names and 429 behavior (third-party, lower confidence)

No official Cerebras page enumerating exact header names was found. A
third-party course site (`theneuralbase.com`, not an official Cerebras
source) describes:

- `x-ratelimit-limit-requests-day`, `x-ratelimit-limit-tokens-minute`,
  `x-ratelimit-remaining-requests-day`, `x-ratelimit-remaining-tokens-minute`,
  `x-ratelimit-reset-requests-day`, `x-ratelimit-reset-tokens-minute`
- a `Retry-After` header on 429 responses
- these headers require Cerebras SDK ≥ 1.2 to appear — this is directly
  relevant here, since `groq_provider.py` wraps the official `groq` SDK
  client (`groq.Groq(...)`), not raw HTTP (`api/llm/groq_provider.py:60`).
  Whether `cerebras_provider.py` should likewise wrap an official
  `cerebras-cloud-sdk` client, versus calling the REST API directly the way
  this measurement probe did, is a design decision the plan surfaces (see
  `016-second-llm-provider-plan.md`, D-G) rather than one assumed here.

This is flagged explicitly as **unverified, third-party, doc-sourced** —
`api/llm/rate_limits.py`'s Cerebras counterpart must be written defensively
(missing-header-tolerant) and re-measured against a real 429 as soon as
billing unblocks it, rather than trusted at face value.

### 2.5 The per-minute comparison the Iteration 5 swap-policy memory asked for

The standing rule requires diagnosing whether a new provider actually
relieves the *per-minute burst* constraint that caused Groq's refusals,
not just comparing daily totals. Measured/documented figures:

| | Groq (measured, CLAUDE.md) | Cerebras (doc-sourced, §2.3) |
|---|---|---|
| tokens per minute | 8,000 | 30,000 uncached / 90,000 total |
| tokens per day | 200,000 | 1,000,000 |

Cerebras's doc-sourced free tier is larger on both axes, not only the daily
total — so, unlike the situation the swap-policy memory warned against, this
is not obviously a case of a bigger bucket that reproduces the same
per-minute failure elsewhere. That said, this is still doc-sourced, not
measured, and per D-E this iteration proves the provider without acting on
this comparison by changing production traffic.

One more continuity note: Cerebras's doc-sourced daily figure (1,000,000) is
almost exactly five times Groq's measured 200,000 — the same "roughly five
times" figure the Iteration 5 memory recorded when Cerebras was first
considered and shelved. That prior consideration is consistent with what
was just re-measured now.

### 2.6 What remains unmeasured

Blocked on the account's 402 in §2.1: the real usage-object shape (does
Cerebras's chat-completion response populate token counts the way
`TokenUsage` expects), a real 429 body and its exact headers, and whether
`retry_after_seconds` is actually present and named the way `pacing.py`
expects. These get re-measured once billing is enabled (§7 risk); until
then, D-F (graceful degradation) governs how the provider module handles
absent or partial usage data.

## 3. Acceptance criteria

- `api/llm/cerebras_provider.py` exists, is the only module (besides tests)
  importing anything Cerebras-specific, and implements `complete(system,
  user) -> str` satisfying the existing `LLMProvider` Protocol test
  (`tests/test_llm_provider.py:37-48`).
- `api/llm/factory.py` gains a `"cerebras"` branch, selectable via
  `QUERYPILOT_LLM_PROVIDER=cerebras`; the default provider is unchanged
  (`"groq"`, per D-E).
- A `CEREBRAS_API_KEY` / `CEREBRAS_API_KEY_FILE` secret pair follows
  `config.py`'s existing `_FILE`-indirection pattern exactly, fails fast
  like Groq's key does.
- Errors map to the existing `LLMError` / `RateLimitError` types, with
  API-key scrubbing equivalent to `_safe_message`.
- `tests/test_single_shot.py`'s vendor-import guard
  (`test_ac2_no_vendor_sdk_outside_the_llm_package`) is converted to real
  `ast.walk`-based import detection against an explicit vendor allow-list
  and passes for both providers (D-B).
- At least one dev-split eval entry (single-shot) and one loop-strategy
  entry are recorded in `EVALS.md` against Cerebras, each carrying the new
  `Provider` field (D-C), explicitly marked not comparable to the Groq
  baseline.
- A live smoke test proves one real Cerebras round trip — **gated on the
  billing blocker in §7** being resolved first.

## 4. Non-goals

- Not switching the default or production provider to Cerebras (D-E).
- Not building a general provider registry/plugin system — a second `if`
  branch in `factory.py` is sufficient for two providers.
- Not generalizing `evals/ledger.py`'s daily-token accounting across
  providers — a Cerebras-specific rate-limit module is acceptable for now
  (D-D).
- Not re-running historical `EVALS.md` entries under Cerebras, and not
  running the held-out `test` split against it.
- Not resolving whether Cerebras should eventually become the default —
  that stays a separate, later decision if ever made.

## 5. Contracts this iteration must not break

- The `LLMProvider` Protocol stays exactly one method
  (`tests/test_llm_provider.py:37-48`); `model`, `last_usage`,
  `last_rate_limit` remain best-effort `getattr` reads, never promoted onto
  the Protocol.
- No SQL execution path is touched. This is strictly a change at the LLM
  provider boundary; the one recorded safety exemption in
  `specs/000-project.md` §4 remains the only one.
- `PacedProvider` (`api/llm/pacing.py`) continues to wrap any
  `LLMProvider` generically via `getattr` pass-through — it must not grow
  Cerebras-specific branches.
- `EVALS.md` stays append-only; no existing entry is rewritten to add the
  new `Provider` field retroactively.

## 6. Decisions

These arrived pre-answered in one batch (the user's stated reason:
eliminating conversational round trips under quota pressure), matching the
precedent set at the start of Iteration 12. Per that precedent, they are
recorded here as decisions rather than posed as open questions, and this
spec proceeds straight into `016-second-llm-provider-plan.md` without a
separate approval pause in between.

- **D-A — re-baselining depth.** Dev-split only: one `single-shot` run and
  one `loop`-strategy run against Cerebras, explicitly marked non-comparable
  to the Groq baseline in `EVALS.md`.
- **D-B — the vendor-import structural test.** Converted to real
  `ast.walk`-based import detection against an explicit vendor allow-list,
  not a second hardcoded string.
- **D-C — `EVALS.md` schema.** A `Provider` field is added to the entry
  template going forward; existing entries are left untouched.
- **D-D — ledger/rate-limit provider-parameterization.** Cerebras-specific
  rate-limit constants live in `api/llm/rate_limits.py` as their own
  functions/constants; `evals/ledger.py`'s daily-token accounting is not
  generalized this iteration.
- **D-E — prove vs. switch.** Cerebras is proven to work; Groq remains the
  runtime default regardless of what §2.5's comparison shows. Switching
  production traffic is out of scope here.
- **D-F — usage-object fidelity.** If Cerebras's usage reporting turns out
  partial or absent relative to Groq's, ledger/pacing accuracy for this
  provider degrades gracefully rather than blocking the iteration.

## 7. Risks

- **Billing blocker (headline risk).** The `CEREBRAS_API_KEY` currently in
  `.env` cannot complete a chat-completion call (§2.1's 402). Every
  acceptance criterion that requires a real round trip — the live smoke
  test, the two `EVALS.md` entries, re-verifying §2.3/§2.4's doc-sourced
  numbers — is blocked until a payment method is added to that Cerebras
  account. The provider module itself (contract, error mapping, config
  wiring, the structural-test fix) can be built and unit-tested against
  mocked responses in the meantime, but nothing here should be reported as
  "proven" until a real call succeeds.
- **Doc-sourced numbers may be stale or wrong.** §2.3 is Cerebras's own
  current page; §2.4 is third-party and explicitly lower-confidence. Both
  need re-measurement once billing unblocks live calls, before any of them
  are relied on operationally (e.g. before `pacing.py`-style throttling is
  tuned for Cerebras).
- **Two models, ambiguous default.** `qwen-3.8-27b` and `gpt-oss-120b` are
  both available; the plan must pick one as the `factory.py` default and
  say why, since nothing in this account's response distinguishes them by
  capability.
