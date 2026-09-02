# 008 — Prompt Tuning: Implementation and Test Plan

> **STATUS: AWAITING YOUR APPROVAL.** No application code exists against this.
> Spec: [`008-prompt-tuning.md`](008-prompt-tuning.md), approved with Q-A and
> Q-B resolved.
>
> **§2 revises the headline saving downward.** The glossary costs most of what
> compaction saves, and the plan says so before proposing to build either. §3
> covers the split mechanics you asked about, and §10 asks for three decisions —
> **D-2 pre-registers the A/B decision rule**, which is the part that stops this
> iteration rationalising its own result.

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

## 2. The net token picture, measured

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

Three edits, and one invariant that has to change shape:

- `TOOLS` loses its entry, so the action cannot dispatch even if hallucinated.
- `LOOP_ACTIONS` loses it, which is where the 22 tokens actually are.
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

## 10. Decisions I need from you

**D-1 — How does token usage reach the runner?** `complete()` returns a string,
and `base.py` is emphatic that the interface stays one method. Options:
(i) widen `complete` to return a result object — clean, but reopens a settled
decision and touches every provider; (ii) a best-effort `last_usage` property on
the concrete provider, read with `getattr` — exactly the precedent already set by
`GroqProvider.model` and `describe_model()`; (iii) estimate locally at ~4
characters per token, no interface change and no vendor dependency.

*My lean: (ii), falling back to (iii) when a provider does not report.* Real
counts where available, an estimate where not, and the interface untouched.

**D-2 — Pre-register the A/B decision rule, before running it.** Q-E left "what
if compaction wins on tokens and loses on accuracy" to judgement, and judgement
applied *after* seeing the numbers is how a result gets rationalised. Proposed
rule, fixed now: **adopt the most compact rendering whose dev-split accuracy is
within one question of DDL; otherwise keep DDL.** One question is the smallest
difference the split can resolve.

*My lean: adopt exactly that, written into the spec before T7 runs.* It is the
ordering discipline again — decide the criterion while the outcome is still
unknown.

**D-3 — Is `--reveal-test-failures` enough of a guard?** It makes reading test
failures auditable rather than impossible. The stricter alternative is to
withhold them entirely until the iteration closes; the weaker one is to print
them always and rely on discipline.

*My lean: auditable, as described.* A hard block would eventually be worked
around by someone querying the database by hand, and an unlogged bypass is worse
than a logged look.

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

**T4 and T6 both land before T7**, and that ordering is the whole protection.
The split has to be frozen before anything is tuned against it, and the expert
questions have to be written before their score is known — the same discipline
that caught three broken questions in Iteration 3, applied to the thing this
iteration is most likely to fool itself about.
