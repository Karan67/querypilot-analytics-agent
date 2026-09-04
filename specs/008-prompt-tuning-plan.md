# 008 — Prompt Tuning: Implementation and Test Plan

> **STATUS: APPROVED — D-1, D-2 and D-3 settled 2026-09-03.** T1 is
> implemented. Spec: [`008-prompt-tuning.md`](008-prompt-tuning.md), approved
> with Q-A and Q-B resolved.
>
> **Resolved:** D-1 — a best-effort `last_usage` on the concrete provider, read
> with `getattr`, with a local count as fallback; `complete()` is untouched.
> D-2 — the A/B rule is pre-registered exactly as §10 proposes and now lives in
> the spec at §7 Q-E. D-3 — `--reveal-test-failures`, recorded in `EVALS.md`.
>
> **Two token instruments, with distinct roles** (settled alongside D-1):
> `tiktoken`/`o200k_base` locally for design decisions, prompt-size tests and
> the T7 A/B; **Groq's own reported `usage`** for the AC8 budget abort and for
> anything written to `EVALS.md` as a spend. Each figure is then produced by
> the instrument that suits it — a local count is reproducible in CI without a
> key, and the daily ceiling is denominated in the provider's count, not ours.
>
> **§2's net token table is superseded and is being corrected task by task.**
> T1 established that the spec's §2.1 figures were `chars // 4` rather than
> measurements; the same heuristic produced §2 below. See the warning there.
>
> **§4 contained a false claim, disproved by experiment at T1** — removing the
> `TOOLS` entry does *not* prevent dispatch. See the correction in §4.

---

## 1. Approach

```
questions.yaml  ──split──> dev (30) ─── tuning happens only here
       │                   test (20) ── looked at once, at the end
       │
       └── expert tier ──> naive_sql vs gold_sql ── must differ (AC12)

build_loop_system(schema, mode, rendering, glossary)
       │            │                   │         └── +220 tokens, always on
       │            │                   └── ddl | compact | compact-abbrev
       │            └── full | withheld
       └── sample_rows gone from TOOLS and from the prompt
```

---

## 2. The net token picture — SUPERSEDED, these are estimates

> **This section's numbers are `chars // 4`, not measurements.** T1 identified
> the heuristic in the spec's §2.1 (three of four components matched it
> exactly) and the 1030 baseline below is the same figure — measured, the
> current prompt is **944**. Every row here is therefore ~9% high.
>
> The section is **not** rewritten here, for the reason §2.2 of the spec gives:
> the compact renderings and the glossary do not exist yet, so there is nothing
> to tokenize. T2 (renderings) and T3 (glossary) each re-measure their own row
> with `tiktoken` as they build it, and the net table is reconstructed from
> measurements before T7 applies D-2's rule.
>
> The conclusion below — that the glossary spends most of what compaction saves
> — is a ratio and survived correction in §2.1's case. It is still a prediction
> until the rows are real.

The spec's §2.2 quoted compaction at −37% and −54%. Those are the schema block
in isolation. **Once the glossary is added, the prompt does not shrink nearly
that much:**

| Configuration | System prompt | Change | Per 40-question pass |
|---|---|---|---|
| Today | 1030 | — | 41,200 |
| Compact, full types, + glossary | 895 | **−13%** | 35,800 |
| Compact, abbreviated, + glossary | 782 | **−24%** | 31,280 |

The glossary is **220 tokens for 8 terms** (~27 each), and it is charged on
every call under resolved Q-D. Removing `sample_rows` returns 22.

So the honest summary is: **compaction buys 13–24%, not 37–54%**, because this
iteration is spending tokens and saving them at the same time. That is still
worth having — 31,280 against a 200,000 daily ceiling leaves room for the
withheld harness, which today does not fit — but it does not solve the token
problem on its own. §2.3 of the spec remains the real answer: the cheapest
question is one answered on the first call.

---

## 3. Held-out split mechanics

### Why the split is written down rather than computed

Two mechanisms were considered, and the measurement rules one out.

**Hash of the question id** (`sha256(id) % 5 in {3,4}`) is stable when questions
are inserted, which matters — an index-based rule would silently move existing
questions between splits every time a question is added, invalidating every
earlier comparison. But measured on the current 40 questions it is badly
unbalanced:

```
overall : dev 24 / test 16
easy    : dev  6 / test  8
medium  : dev 13 / test  3     <-- one question is 33% of this tier
hard    : dev  5 / test  5
```

Three medium test questions cannot support a per-tier number. **An unstratified
test set is not a test set.**

So the split is **assigned once, stratified, and frozen into the file** — which
gets balance and stability together, at the cost of being editable. That cost is
handled the same way retired ids are handled: a test pins the exact membership,
and a fingerprint goes into the record.

### The assignment

`split: dev | test` becomes a required field, loaded as strictly as every other
(`006` AC6). Initial assignment: within each tier, ordered by id, 60/40.

> **FROZEN at T4, 2026-09-04, with one deliberate deviation.**
>
> The rule as written put **both** `invoice_totals` questions in test — that
> relation is covered by exactly two — leaving the dev split, the only split
> tuning may look at, never exercising the view. `medium-018`, the precision
> question this plan nominates as `compact-abbrev`'s canary, is one of them. A
> canary that only sings in the room nobody may enter is not a canary.
>
> Two swaps inside the easy tier fix it without changing any tier count:
> **`easy-012` into dev, `easy-005` into test.** Measured result: every one of
> the 12 relations is now exercised by both splits, where the unmodified rule
> left `invoice_totals` absent from dev and `media_type` absent from test.
>
> Interleaving was measured too, and is **worse** — it drops test coverage to
> 9 of 12 relations. Tier stratification balances difficulty and says nothing
> about coverage; that had to be checked rather than assumed.
>
> Frozen membership is pinned question-by-question in `tests/test_splits.py`,
> and only `split:` lines were added to `questions.yaml` — verified against
> `git diff` as 40 insertions, 0 deletions, every one matching
> `split: dev|test`. No question text, gold query, `ordered` or `covers` value
> was touched.
>
> **Current totals are 24 dev / 16 test**, not the 30/20 tabled below: the
> expert tier does not exist until T6. One test question is therefore **6.2%**
> today and 5% once T6 lands.

| Tier | dev | test |
|---|---|---|
| easy (14) | 8 | 6 |
| medium (16) | 10 | 6 |
| hard (10) | 6 | 4 |
| expert (10) | 6 | 4 |
| **total (50)** | **30** | **20** |

### Three guards, because one is not enough

1. **A pinning test** names the exact test-split membership. Moving a question
   between splits fails it, the way moving a retired id fails today.
2. **A split fingerprint** — a hash of the sorted `(id, split)` pairs — is
   recorded in `EVALS.md` beside every number. A number taken under a different
   split is visibly a different number, derived not declared (`006` AC24).
3. **`--split {dev,test,all}`, defaulting to `dev`.** Tuning runs cannot touch
   the test set by omission; reaching it takes typing it.

### Reading test failures leaves a trace

Per-question failure detail for a `test` run is withheld unless
`--reveal-test-failures` is passed, and passing it is **recorded in `EVALS.md`**.

The point is not to forbid looking — sometimes a genuine bug needs diagnosing.
It is that question-level overfitting requires knowing *which* questions failed,
so obtaining that becomes an auditable act rather than an invisible one. The
aggregate and per-tier numbers are always shown; only the case list is gated.

### The resolution limit, stated plainly

Twenty test questions means **one question is 5%**. A tuning change worth less
than 5% is not measurable on this split, and the plan should not pretend
otherwise. `--repeat` reduces noise, not granularity.

---

## 4. Removing `sample_rows` (resolved Q-B)

> **CORRECTED at T1 — the claim below was false, and disproved by experiment.**
>
> *"`TOOLS` loses its entry, so the action cannot dispatch even if
> hallucinated"* is wrong. The orchestrator never consulted `TOOLS` for this
> action: it imported `sample_rows` directly from `api.db.sampling` and
> dispatched it from an `elif` branch. The probe removed **only** the registry
> entry, scripted a provider to emit `ACTION: sample_rows track`, and the loop
> **returned rows from the database** — `step.ok is True`, observation
> `Observation from sample_rows("track") …`.
>
> Had the plan been followed as written, T1 would have shipped a live database
> path reachable by a hallucinated action, hidden behind a prompt convention,
> with a fully green suite. This is now spec **AC4b**.
>
> The plan also said "three edits". The real surface is **six sites across four
> modules**: `tools.py` (import, wrapper, registry entry), `prompts.py`
> (`LOOP_ACTIONS`, `EXCLUDED_ACTIONS`, `ACTION_DESCRIPTIONS`,
> `frame_sample_rows`), `orchestrator.py` (two imports, `_run_sample_rows`, the
> dispatch branch, a `RETRY_POLICY` entry), and a comment in `protocol.py`.

Four edits, and one invariant that has to change shape:

- `TOOLS` loses its entry **and** the orchestrator loses its dispatch branch —
  neither alone is sufficient, and the branch is the load-bearing half.
- `LOOP_ACTIONS` loses it, which is where the 19 tokens actually are.
- `EXCLUDED_ACTIONS` gains it, with the measured reason.
- `api/db/sampling.py` and `tests/test_sampling.py` are **untouched**.

The current test asserts `LOOP_ACTIONS | EXCLUDED_ACTIONS == set(TOOLS)`. With a
capability excluded that is no longer registered, that becomes two directional
rules:

- every `TOOLS` entry is offered or excluded — nothing silently unavailable;
- every `LOOP_ACTIONS` entry is in `TOOLS` — **nothing offered that cannot
  dispatch**, which is the new failure this change makes possible.

`EXCLUDED_ACTIONS` may then name retired capabilities, which is what it now is.

---

## 5. Schema compaction

```python
SCHEMA_RENDERINGS = ("ddl", "compact", "compact-abbrev")
```

```
album(album_id int, title str, artist_id int) PK[album_id] FK[artist_id->artist]
```

Points that need care rather than cleverness:

- **Abbreviation loses precision.** `NUMERIC(10, 2)` becomes `num`, and
  `medium-018` asks for an average *rounded to two decimal places*. That is
  exactly the kind of question a lost scale could break, which is why
  `compact` (full types, −13% net) and `compact-abbrev` (−24%) are separate
  candidates rather than one.
- **Views drop out of the problem.** The compact form carries no nullability, so
  `001` AC4's "never claim NOT NULL on a view" cannot be violated by
  construction.
- **Determinism is preserved**: relation order comes from `Schema`, which is
  already sorted, and nothing re-sorts.
- **Every rendering changes the fingerprint** (AC7), so no two renderings can be
  compared by accident.

> **RUN at T7, 2026-09-04 - `compact` is adopted.** Two arms, two passes each,
> 30 dev questions, `openai/gpt-oss-120b`, glossary on in both, split
> fingerprint `ec65d5ba81d6`.
>
> | arm | pass 1 | pass 2 | mean | schema fp |
> |---|---|---|---|---|
> | `compact` | 30/30 | 30/30 | **100.0%** | `e0b31c713530` |
> | `ddl` | 29/30 | 30/30 | **98.3%** | `f289a58e7ef7` |
>
> D-2 applied mechanically: `compact - ddl = +0.5 questions per pass`, which
> is inside the one-question tolerance, so **the most compact surviving
> rendering is adopted**. The branch that would have adopted a lower-scoring
> rendering was never exercised - `compact` scored higher.
>
> **The decision is robust to the one reading that could change it.** `ddl`'s
> single miss was `rate_limited`, not a model error. Excluding it under AC9
> leaves `ddl` 59/59 and `compact` 60/60 - a delta of zero, and the same
> adoption. Reported both ways because the raw figure is not accuracy, and
> Iteration 4 was misread for weeks on exactly that distinction.
>
> **The token axis is half-measured, and that is a defect of this task.**
> `compact` billed 69,572 tokens (61,058 prompt + 8,514 completion) over 60
> calls - 1,017.6 prompt tokens a call. **`ddl`'s billed total is
> unavailable**: it ran before the terminal cost line existed (see section 8),
> and the day's remaining quota did not allow a re-run. The measured,
> reproducible part of the axis is the system block itself - `ddl` 1,104
> tokens against `compact` 916, a difference of **188 tokens a call** - and
> the rest is stated as missing rather than reconstructed by arithmetic.
>
> **One call per question, measured.** 60 provider calls for 60 question
> attempts in the `compact` arm. The worst-case projection assumes three, so
> it overstates a healthy run by 3x - which is why a ceiling had to be raised
> for a run that then cost a third of its own projection.
>
> **The adoption is effective, not merely recorded.** `ADOPTED_RENDERING` in
> `prompts.py` is the single place the decision lives; `build_loop_system`,
> `orchestrator.answer` and the runner all default to it, so the deployed
> agent renders `compact` and T8 will score the configuration that ships. A
> test pins `ADOPTED_RENDERING == SCHEMA_COMPACT` separately from the tests
> that pin the defaults to the constant, because the latter hold for any
> value the constant takes and would not notice a reverted decision.
>
> **Two things the adoption broke, both caught by the suite:**
>
> 1. **`single-shot` cannot render anything but DDL** (AC1 freezes its
>    template), and `build_strategy` refuses rather than ignoring the flag -
>    so a blanket default made the documented no-argument invocation raise a
>    ValueError from its own default. This is the glossary problem of T3
>    exactly, and it is fixed the same way: `resolve_rendering(strategy,
>    rendering)` holds the two-branch rule in one place, shared by the CLI,
>    `build_strategy`, `run_pass` and `format_report`.
> 2. **Five tests pinned the old default.** Each is re-stated with the
>    rendering made explicit rather than relaxed, and the property the
>    deleted `test_the_default_rendering_is_ddl` was really protecting - that
>    Iteration 4's prompt is still reproducible - is now asserted directly,
>    where it does not depend on any default. It could not have been left
>    implicit: after T3 and T7, *neither* DDL nor glossary-off is a default
>    any more, so the guarantee had nothing holding it up.
>
> The four corners of the prompt-size matrix are now pinned together in
> `test_ac1_the_measured_saving_is_19_tokens`, all measured with `o200k_base`:
> DDL 925 / 1,104 and compact 737 / 916, glossary off / on. The deployed
> prompt is the last of those.
>
> **Nothing was recorded in `EVALS.md`.** The `compact` arm is clean and
> recordable; the `ddl` comparator is not, because one `rate_limited` case
> makes it unrecordable under T5's rule and its token total is unavailable.
> Filing half an A/B was judged worse than filing none, and T8's held-out
> run is what that file is for.

---

## 6. Glossary injection

```python
GLOSSARY = {
    "active customer": "a customer with at least one invoice dated within 12 "
                       "months of the most recent invoice_date in the database",
    "support representative": "an employee who appears as customer.support_rep_id "
                              "for at least one customer; other employees are not",
    ...
}
```

Rendered as its own block, always present (resolved Q-D), and measured both ways
for AC13.

> **MEASURED at T3.** The block is **178 tokens** — 168 for the eight
> definitions plus an 11-token header — against the 220 (~27 a term) estimated
> below. The estimate was the `chars // 4` heuristic again.
>
> **The glossary very nearly cancels compaction.** At 40 questions it costs
> 7,160 tokens a pass; T2 measured compaction as saving 7,520. Net for
> `compact` + glossary against today's DDL prompt: **916 against 925, a 1%
> saving** — not the −13% projected in §2. Only `compact-abbrev` retains a real
> margin at 814 (−12%).
>
> That is the honest state of the token argument, and it was reconfirmed as
> acceptable rather than discovered late: resolved Q-D stands, because a
> glossary injected only for the questions that declare a term would tell the
> model which questions are the ambiguous ones — information no deployment has.
> The saving was never the reason for the glossary; **AC13 is**, and accuracy is
> the axis it has to win on.
>
> **T6 owes this task one test.** The reverse consistency check — that no
> definition goes unused — cannot run until questions declare terms, so T3 ships
> only the forward direction. `tests/test_glossary.py` says so in its module
> docstring; the obligation is recorded here so it is not carried in memory.

> **AC13 IS DEFERRED, not met — spec debt, opened 2026-09-04 at T7.**
>
> AC13 asks for accuracy **with and without** the glossary, and calls the
> difference *the only evidence that the glossary does anything*. The
> glossary-off control arm was dropped from the T7 matrix so that D-2 could be
> settled inside one day's 200,000-token quota. That was the right call on the
> day — D-2 gated an architectural decision and the control measured something
> already believed — but it leaves this iteration reporting the treatment
> without its control.
>
> **What is owed:** `--strategy loop --schema full --rendering ddl
> --no-glossary --split dev`, at the same pass count as the arm it is compared
> against. `--no-glossary` exists precisely so this run reproduces Iteration
> 4's prompt byte for byte, which is what makes it a control rather than
> merely a cheaper run; `test_iteration_4s_prompt_is_still_reproducible`
> guards that. Projected worst case 258,390 at three passes, 172,260 at two.
>
> **What exists instead:** a three-question pre-flight probe in which
> `expert-001` was correct with the glossary and `wrong_result` without it.
> That is n=1 and is not evidence for AC13; it is recorded so the gap is not
> mistaken for a null result.
>
> **Why this is debt rather than a closed decision.** The 178-token glossary
> is on every call the deployed agent makes, forever. Nothing currently
> measures whether it pays for itself — T8's held-out `expert` 4/4 is
> suggestive but was run with the glossary on, so it cannot separate the
> glossary's contribution from the model's. Until the control runs, *the
> glossary improves accuracy* is a design belief and should not be written
> down as a finding.

**Every term is a population definition** (spec §2.4). The measurements ruled out
the metric-definition terms: `sum(invoice.total)` and
`sum(line.unit_price * quantity)` are both 2328.60, so "what is revenue" cannot
be asked here at all.

Two tests keep the block honest:

- every term a question declares exists in `GLOSSARY`;
- every term in `GLOSSARY` is declared by at least one question — a definition
  nothing uses is 27 tokens of prompt on every call, forever.

---

## 7. The expert tier

Ten questions, each built on a measured-discriminating term:

| id | Term | Naive | Conventional |
|---|---|---|---|
| expert-001 | active customer | 59 | 46 |
| expert-002 | support representative | 8 | 3 |
| expert-003 | sold track | 3503 | 1984 |
| expert-004 | charting artist | 275 | 165 |
| expert-005 | active genre | 25 | 24 |
| expert-006 | curated playlist | 18 | 14 |
| expert-007 | average order value | 5.6519 | 1.0396 |
| expert-008 | credited track | 3503 | 2526 |
| expert-009 | two terms combined | — | — |
| expert-010 | two terms combined | — | — |

**`naive_sql` is a required field on this tier and is never scored.** It exists
so a test can prove the question discriminates — the direct analogue of `006`'s
duplicate-row check. A question whose naive and conventional readings return the
same rows is a free point, and it would inflate the number while measuring
nothing.

`expert-002` is deliberately the ambiguity that got `medium-008` retired. It was
unfair without a stated convention and is fair with one, and having both in the
record is a better argument for the glossary than any amount of prose.

**AC16 still applies**: given the glossary, exactly one answer is defensible.
"Realistic" is not a licence to reintroduce genuine ambiguity.

---

## 8. Token accounting

> **IMPLEMENTED at T5, 2026-09-04.** Three decisions were taken here that the
> plan did not specify:
>
> 1. **`RateLimitError(LLMError)`** carries AC9's distinction. Typed, never
>    text-matched — `003` established that message wording is not a contract,
>    and an SDK rephrasing would otherwise reclassify every rate-limited run as
>    a model failure. `groq.RateLimitError` is a real SDK type, so the mapping
>    needs no guessing.
> 2. **Two budget guards, not one.** The pre-flight projection is counted
>    locally with `tiktoken` and refuses to start (AC8 as written). An in-flight
>    check against Groq's *billed* usage stops a run whose projection was wrong.
>    The second should never fire: it guards against the two instruments
>    disagreeing, which is a risk D-1 created by using two. Only *measured*
>    spend polices the ceiling — letting a local estimate enforce a quota it is
>    not denominated in would be the wrong instrument doing the wrong job.
> 3. **A rate-limited run cannot be recorded.** The number still prints; what is
>    refused is filing it beside comparable ones. AC18 already said no prompt
>    change is accepted on such a run, and this makes that enforceable rather
>    than remembered.
>
> **The projection has already found something.** Worst case, per pass on the
> 24-question dev split with the glossary on: `ddl` 81,792 tokens, `compact`
> 68,256, `compact-abbrev` 60,912 — 41%, 34% and 30% of the 200,000 daily
> ceiling respectively.
>
> **T7's A/B as planned does not fit in a day.** Three renderings x three
> passes on dev projects **632,880 tokens, 316% of the quota**, and AC8 will
> refuse to start it. That figure is worst-case — every question burning all
> three calls — and the realistic cost is far lower, since Iteration 3 measured
> ~97% first-call success. But the guard aborts on the worst case by design.
>
> **Resolved 2026-09-04: `compact-abbrev` is dropped from the T7 matrix**, which
> becomes 2 x 3. The reasoning is T2's own measurement rather than the budget:
> abbreviation buys 92 tokens over `compact` and destroys every VARCHAR length
> and NUMERIC scale to do it, making it the weakest candidate on the axis that
> matters. The rendering itself **stays in the codebase**, built and tested — it
> is the A/B arm that is dropped, not the capability.
>
> Where the projection still exceeds the ceiling, T7 may **raise the pre-flight
> ceiling deliberately**, relying on the in-flight billed guard to catch a real
> breach. That is a sound trade only because the two guards are independent: the
> projection is a local worst case and the in-flight check is the provider's
> actual bill.

> **T8 FINDING 2026-09-04: the limit that stopped us is probably not the one
> this section is denominated in.** Both multi-pass held-out runs were refused
> for rate limits, at **120,000 and 165,000 of a 200,000-token daily budget** —
> that is, with a fifth to a third of the day still unspent. Single passes
> earlier the same day were clean; the failures appeared *inside* sustained
> multi-pass runs and got worse as the runs got longer (2 rate limits in 3
> passes, then 3 in 2 passes).
>
> That signature is a **burst / per-minute** ceiling, not daily exhaustion.
> Every guard in this section polices a *daily* quantity, so none of them can
> see it, and no amount of budget arithmetic will.
>
> **This is why B-1 was pulled forward** (charter §8). Groq returns
> `x-ratelimit-remaining-tokens`, `x-ratelimit-reset-tokens` and `retry-after`
> on every response, and reading them settles TPM-versus-TPD in one run. It is
> also why the provider migration was **not** taken: a larger daily bucket
> does not fix a per-minute ceiling, and switching would have retired three
> iterations of baseline comparability to chase the wrong limit.

> **AMENDED at T7, 2026-09-04 - one knob could not drive two guards.** The
> authorisation this section anticipated - raise the pre-flight ceiling, keep
> the billed guard - turned out to be inexpressible. `--token-budget` fed
> both checks, and the two are denominated differently: the pre-flight
> compares against a worst case over the whole invocation (204,480 at two
> passes), the in-flight against one pass's bill (~35,000). Clearing the
> first meant setting a value the second could never reach, so raising the
> ceiling silently deleted the guard it was supposed to be traded against.
>
> **`--max-projection` now governs the pre-flight ceiling alone**, leaving
> `--token-budget` to mean what its name says. `--token-budget` on its own
> keeps its old behaviour, so every invocation recorded before T7 still reads
> correctly. A raised ceiling prints a line naming the breaker that still
> applies, because a deliberate act should not be inferable only from shell
> history.
>
> **Three reporting defects, all found by running it rather than reading it.**
> Each shipped green at T5, and each was invisible to the fake-provider tests
> because each lives in the path between a real run and a human reader:
>
> 1. **`print_report` printed no cost at all.** The figures went only into
>    `format_report`, which runs under `--record` - and no tuning run is ever
>    recorded. So every run this iteration actually makes reported nothing,
>    which is the same blindness Iteration 4 described as discovering the
>    ceiling through 31 provider errors. It cost this task the `ddl` arm's
>    token figure permanently.
> 2. **Both A/B arms printed an identical header.** Rendering, schema
>    fingerprint and glossary flag were all absent from the terminal report,
>    and the two arms share a prompt fingerprint because only the schema
>    block differs. A comparison whose output cannot say which configuration
>    produced it is not a comparison.
> 3. **`format_report` recorded the first pass's tokens** under an unqualified
>    `Tokens` heading three rows above `Passes`, under-reporting any repeated
>    run by (passes - 1) / passes - permanently, in an append-only file. Now
>    `Tokens (all passes)`, summed.
>
> A fourth was found by a mutation aimed somewhere else: the recorded
> `Glossary` row, the identifying field of AC13's entire comparison, was read
> by no test and could have been hardcoded either way without going red.

- `TokenUsage` per call, aggregated per question and per run, reported in the
  terminal and recorded in `EVALS.md` (AC5).
- **Abort before spending** if `questions × max_calls × prompt_tokens` exceeds
  `--token-budget` (AC8). Worst-case projection, deliberately: an
  optimistic projection that lets a run die at question 31 wastes the 30 that
  worked and produces a non-number.
- **`rate_limited` becomes its own breakdown category** (AC9), split out of
  `provider_error`. Iteration 4's 82.5% was 33 correct and 7 rate-limited, and
  the run that reports those identically invites exactly the wrong reading.

---

## 9. Files

| File | Status | Contents |
|---|---|---|
| `api/agent/tools.py` | edit | drop the `sample_rows` entry |
| `api/agent/prompts.py` | edit | renderings, `GLOSSARY`, injection |
| `api/agent/glossary.py` | **new** | the term definitions, kept out of the prompt-mechanics file |
| `evals/dataset.py` | edit | `split`, `naive_sql`, `glossary`, `expert` tier |
| `evals/questions.yaml` | edit | splits on 40; 10 new expert questions |
| `evals/run_evals.py` | edit | `--split`, `--rendering`, `--token-budget`, usage reporting |
| `evals/scoring.py` | edit | `TokenUsage`, `rate_limited` category |
| `api/llm/groq_provider.py` | edit | expose `last_usage` (D-1) |
| `tests/test_glossary.py` | **new** | terms used, terms defined |
| `tests/test_splits.py` | **new** | pinning, fingerprint, stratification |
| `tests/test_eval_questions.py` | edit | discrimination tests for `expert` |

---

## 10. Decisions — all three resolved 2026-09-03

**D-1 — How does token usage reach the runner?** `complete()` returns a string,
and `base.py` is emphatic that the interface stays one method. Options:
(i) widen `complete` to return a result object — clean, but reopens a settled
decision and touches every provider; (ii) a best-effort `last_usage` property on
the concrete provider, read with `getattr` — exactly the precedent already set by
`GroqProvider.model` and `describe_model()`; (iii) estimate locally at ~4
characters per token, no interface change and no vendor dependency.

*My lean: (ii), falling back to (iii) when a provider does not report.* Real
counts where available, an estimate where not, and the interface untouched.

> **Resolved: (ii), and option (iii) is rejected outright.**
> `complete(system, user) -> str` stays exactly as `base.py` declares it.
> `GroqProvider` gains a best-effort `last_usage`, read by the runner with
> `getattr`, following the precedent of `GroqProvider.model` and
> `describe_model()`.
>
> **The fallback is a real tokenizer, not the 4-character estimate the option
> proposed.** T1 is why: that estimate is exactly what produced the spec's
> incorrect §2.1 table, and writing it into the runner would have industrialised
> the mistake. Two instruments, with roles that do not overlap:
>
> | Instrument | Used for | Why |
> |---|---|---|
> | `tiktoken`, `o200k_base` | design decisions, prompt-size tests, the T7 A/B | reproducible in CI with no API key; matches the deployed `gpt-oss` family |
> | Groq's reported `usage` | the AC8 budget abort, spends in `EVALS.md` | the 200k/day ceiling is denominated in *the provider's* count, not ours |
>
> Where the two disagree, the provider's number wins for anything describing
> money or quota, and the local number wins for anything that must be
> recomputable offline. A figure recorded in `EVALS.md` states which instrument
> produced it, because a local count and a billed count are different
> quantities and silently mixing them would make the record unreadable.
>
> `tiktoken` is added to `api/requirements-dev.txt`, not to the runtime
> requirements: like `evals/`, it is measurement apparatus and is not shipped in
> the API image. It is a tokenizer rather than a provider SDK, so it does not
> engage the §5 swappability constraint — the structural test guards `groq`
> imports under `api/`, and nothing here changes that.

**D-2 — Pre-register the A/B decision rule, before running it.** Q-E left "what
if compaction wins on tokens and loses on accuracy" to judgement, and judgement
applied *after* seeing the numbers is how a result gets rationalised. Proposed
rule, fixed now: **adopt the most compact rendering whose dev-split accuracy is
within one question of DDL; otherwise keep DDL.** One question is the smallest
difference the split can resolve.

*My lean: adopt exactly that, written into the spec before T7 runs.* It is the
ordering discipline again — decide the criterion while the outcome is still
unknown.

> **Resolved: pre-registered as written, and recorded in the spec at §7 Q-E**
> — with the spec, not the plan, because that is where the acceptance criteria
> it governs live. The rule is fixed before T7 produces a single number:
> *adopt the most compact rendering whose dev-split accuracy is within one
> question of DDL; otherwise keep DDL.* T7 applies it mechanically and reports
> the arithmetic, including any case where the rule selects a rendering that
> scored lower.
>
> **T2 owes this rule a measured token axis.** The rule trades accuracy against
> a saving, and §2.2's savings are still `chars // 4`.

**D-3 — Is `--reveal-test-failures` enough of a guard?** It makes reading test
failures auditable rather than impossible. The stricter alternative is to
withhold them entirely until the iteration closes; the weaker one is to print
them always and rely on discipline.

*My lean: auditable, as described.* A hard block would eventually be worked
around by someone querying the database by hand, and an unlogged bypass is worse
than a logged look.

> **Resolved: the auditable flag.** Aggregate and per-tier numbers always print
> for a `test` run; the per-question case list requires
> `--reveal-test-failures`, and using it is recorded in `EVALS.md` beside the
> number it was used on.
>
> This does not prevent overfitting, and the plan should not claim it does. It
> makes the act that *enables* question-level overfitting leave a trace, which
> is the most a guard at this layer can honestly do.

---

## 11. Risks

| Risk | Handling |
|---|---|
| **Tuning overfits the dev split** | The test split exists for exactly this, and D-3 makes reading it auditable. It does not prevent dev-split overfitting — that is what the final test number measures |
| 20 test questions resolve only 5% steps | Stated in §3 rather than discovered later. Small wins will not be provable |
| The glossary eats the compaction saving | Measured in §2; net is 13–24%, and the plan says so up front |
| Abbreviated types break precision questions | Two candidate renderings, and `medium-018` is the canary |
| An expert question is a free point | `naive_sql` plus the discrimination test (AC12) |
| The withheld harness still exceeds the daily budget | AC8 aborts before spending. Compaction alone may not be enough, and that is a finding, not a failure |
| `sample_rows` turns out to be needed by the expert tier | The module survives (resolved Q-B); restoring the action is a one-line change |

---

## 12. Proposed decomposition

| # | Task | Verified by |
|---|---|---|
| **T1** | Remove `sample_rows` from `TOOLS` and the prompt | Both directional invariants of §4 |
| **T2** | Selectable schema renderings + fingerprint wiring | Rendering tests, fingerprint distinctness |
| **T3** | `GLOSSARY`, injection, both keep-honest tests | §6 |
| **T4** | **Split mechanics, frozen before any tuning** | Pinning test, fingerprint, stratification |
| **T5** | Token accounting, budget abort, `rate_limited` category | Fake-provider usage tests, zero-budget abort |
| **T6** | **Author 10 expert questions + `naive_sql`** | Discrimination tests. **No tuning run yet** |
| **T7** | Run the A/B on the **dev split only**, apply D-2's rule | Four configurations, decision applied mechanically |
| **T8** | **One test-split run**, recorded | A number in `EVALS.md` with its split fingerprint |

> **T8 CLOSED 2026-09-04. Filed: 100.0% on the held-out split, one pass.**
>
> `compact`, glossary on, `--split test`, split fingerprint `ec65d5ba81d6`,
> schema fingerprint `e0b31c713530`, `openai/gpt-oss-120b`, 20/20, zero rate
> limits, 24,276 tokens billed over 20 calls. `--reveal-test-failures` was
> **not** passed. Every tier 100%, including `expert` 4/4 on questions nothing
> was tuned against.
>
> **That headline is a point estimate, and eight passes say the point moves.**
> Five runs of the identical configuration on the identical 20 questions, all
> at temperature 0:
>
> | run | passes | genuine wrong | rate limited | filed |
> |---|---|---|---|---|
> | 2026-09-03 late | 1 | 2 | 1 | no — AC18 |
> | 2026-09-04 #1 | 1 | 1 (`expert-010`) | 0 | no — D-3 leak, see below |
> | 2026-09-04 #2 | 1 | **0** | 0 | **yes** |
> | 2026-09-04 #3 | 3 | 2 of 60 (pass 1 was 20/20), spread 90.0–100.0% | 2 | no — AC18 |
> | 2026-09-04 #4 | 2 | 1 of 40, spread 85.0–95.0% | 3 | no — AC18 |
>
> **Six genuine wrong answers across eight passes — 0 to 2 per pass, and
> `007`'s measured 0.0% spread across three passes does not survive this.**
> That claim is what AC18 leans on when it says *a difference of one question
> is a real difference*. On a 20-question split the noise is at least two.
>
> **This reaches back into D-2.** `compact` was adopted over `ddl` on a
> 0.5-question dev margin — comfortably inside the noise band measured here.
> The rule was pre-registered and applied mechanically, and the adoption
> stands: `compact` was never *worse*, and it is 188 measured tokens a call
> cheaper. But **the accuracy half of that comparison is not supported**, and
> anywhere this plan reads as though `compact` is more accurate, it is
> overclaiming. It is cheaper and not worse. That is the whole claim.
>
> **The first attempt at this run leaked held-out detail into the record.**
> `format_report` wrote every failing question's text and generated SQL into
> `EVALS.md` unconditionally, three rows below the `Test failures revealed |
> no` that the same block asserted — `print_report` had always gated it and
> the recorded path never did. The append was uncommitted and was reverted,
> the gate was added, and the run was repeated through the corrected path.
> **The assistant had by then seen that `expert-010` was the failing
> question**; nothing about the prompt, the glossary or the dataset was
> changed in response, and this is recorded so a later reader can weigh it.
>
> **Superseded note — the original T8 attempt:** The held-out run
> happened and produced a number; `--record` was refused by T5's rule because
> one of the 20 questions was rate limited, which makes the figure a floor
> rather than a measurement. `EVALS.md` is unchanged and the iteration stays
> open until a clean pass replaces this.
>
> `compact`, glossary on, `--split test`, split fingerprint `ec65d5ba81d6`,
> schema fingerprint `e0b31c713530`, `openai/gpt-oss-120b`, one pass,
> 22,712 tokens billed over 19 calls. `--reveal-test-failures` was **not**
> passed.
>
> | | dev (T7) | test (T8, floor) |
> |---|---|---|
> | overall | 100.0% | **85.0%** (17/19 = 89.5% excluding the rate limit) |
> | easy | 8/8 | 5/6 |
> | medium | 10/10 | 6/6 |
> | hard | 6/6 | **2/4** |
> | expert | 6/6 | **4/4** |
>
> **The dev split's 100% did not generalise, and this is the number that
> matters.** Section 11 listed *tuning overfits the dev split* as the risk the
> held-out split exists to detect, and it detected it. Two `wrong_result`
> failures plus one rate limit, concentrated in `hard` with one in `easy`.
> Which tier the rate limit landed in is not stated here because determining
> it means reading per-question detail on the held-out split, and the flag
> that permits that was deliberately not passed.
>
> **`expert` scored 4/4 on questions nothing was tuned against.** That is the
> one claim this iteration set out to support, and it is the one tier where
> dev and test agree. It rests on four questions, so it is evidence and not
> proof.
>
> **Do not read 85.0% as an accuracy.** It is a floor: the rate-limited
> question is scored as a failure and would very likely have passed. That is
> exactly the misreading Iteration 4's 82.5% caused for weeks, and it is why
> the guard refused to file it.

**T4 and T6 both land before T7**, and that ordering is the whole protection.
The split has to be frozen before anything is tuned against it, and the expert
questions have to be written before their score is known — the same discipline
that caught three broken questions in Iteration 3, applied to the thing this
iteration is most likely to fool itself about.
