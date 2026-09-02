# 006 — Evaluation Suite and Baseline

> **STATUS: IMPLEMENTED 2026-09-03.** All 26 acceptance criteria are covered by
> tests; the baseline is recorded in [`EVALS.md`](../EVALS.md).
> Plan: [`006-evals-plan.md`](006-evals-plan.md), whose header lists what the
> plan got wrong.
>
> **AC26 closes the last outstanding item from Iteration 1.** The deliberate
> skip in `tests/test_validator.py` is now a real test that loads the dataset
> and asserts Gate 2 accepts all 40 reference queries. The suite has no skips.
>
> **§7 Q-D is not a technical question and it is the most important one here.**
> The rest of this spec decides how a number is computed; Q-D decides whether the
> number means anything.
>
> §2 is measured against the running database.

Iteration: 3 (Evals) · Inherits: [`000-project.md`](000-project.md)
Depends on: [`005-single-shot-generation.md`](005-single-shot-generation.md) —
there is now something to score.

---

## 1. Intent

Produce **one number**, honestly, and the machinery to reproduce it.

> **Iteration 3 — Done when:** you have a number. It will be mediocre — that's
> the point; it's what you improve against.

Everything after this iteration is a delta from a baseline measured here. That
gives the suite an unusual property: **its job is to be trusted, not to look
good.** A benchmark that flatters the system is worse than no benchmark, because
it converts an unknown into a false certainty and every later claim inherits it.

This is also where the project stops being able to fool itself. Up to now
"correct" meant "passes its own tests". From here it means "returns the rows a
hand-written query returns", which is a standard the implementation does not get
to define.

### Why it decomposes failure, not just counts it

A bare *"61% accuracy"* is nearly useless for improving anything. The other 39%
is the backlog, and it matters enormously whether a failure was:

- rejected by Gate 2 before running (a prompt problem),
- a database error (a schema-grounding problem),
- a clean run returning the wrong rows (a reasoning problem), or
- a refusal or provider failure (not a SQL problem at all).

Those have different fixes, and Iteration 5's plan comes directly out of the
breakdown. So the runner reports the split, not only the headline.

---

## 2. What the comparison has to survive, measured

Three findings from running variant queries against the live database. Each one
rules out a naive scorer.

**Column names differ for identical results.**

```
SELECT count(*) FROM track            -> columns ('count',)
SELECT count(t.track_id) AS n FROM track t -> columns ('n',)
rows equal: True
```

So names are ignored. Comparing them would fail correct answers on aliasing.

**Numeric types differ for identical values.**

```
SELECT sum(total) FROM invoice          -> Decimal('2328.60')
SELECT sum(total)::float FROM invoice   -> 2328.6
Decimal('2328.60') == 2328.6            -> False
```

A cast the model chose freely breaks equality. **Numeric normalisation is
mandatory, not a refinement** — without it the suite would score correct answers
as wrong and the baseline would be meaningless in the pessimistic direction.

**Column order can be permuted with the same information.**

```
SELECT g.name, count(*) ... -> (('Rock', 1297), ('Latin', 579), ('Metal', 374))
SELECT count(*), g.name ... -> ((1297, 'Rock'), (579, 'Latin'), (374, 'Metal'))
equal as tuples          : False
equal as sets of values  : True
```

Both answer the question. Whether the second counts as correct is a policy
decision, not a technical one — **Q-B**.

**Row order is sometimes part of the answer.** `ORDER BY genre_id LIMIT 3` and
`ORDER BY name LIMIT 3` return genuinely different rows, so a "top N" question
cannot be scored order-insensitively. But "list every genre" can. That is a
per-question property, not a global one.

---

## 3. Acceptance criteria

### The dataset

- **AC1** — `evals/questions.yaml` holds 30–50 questions across three tiers:
  `easy` (single table), `medium` (one or two joins, grouping), `hard`
  (multi-join, subqueries, or multi-step reasoning).
- **AC2** — Every question has a **stable id** that never changes meaning. A
  case that gets harder is a new id, not an edited one, or the history in
  `EVALS.md` stops being comparable.
- **AC3** — Ground truth is a **hand-written reference SQL query**, not a literal
  row snapshot (**Q-A**).
- **AC4** — Every question declares whether row order is part of the answer.
- **AC5** — The dataset covers every relation in the schema at least once,
  including the view, and every foreign-key join path at least once. Coverage is
  derived from `get_schema()`, not from taste.
- **AC6** — Loading the dataset validates its shape and fails loudly on a
  missing field, an unknown tier, or a duplicate id.

### Ground truth and matching

- **AC7** — A candidate is correct when its result set matches the reference
  query's result set, both executed through the same path.
- **AC8** — Column **names are ignored** (measured §2).
- **AC9** — Numeric values are normalised before comparison, so `Decimal('2328.60')`
  and `2328.6` match (measured §2). Tolerance per **Q-C**.
- **AC10** — Row order is compared only when the question says it matters (AC4).
- **AC11** — Both sides run through `execute_sql()`, so both are capped and
  truncated identically. A comparison where one side was capped and the other
  was not would silently mis-score large results.
- **AC12** — A **dataset fingerprint** is asserted before scoring — e.g. `track`
  has 3503 rows. If the database is reseeded differently, the run aborts rather
  than producing a number that is not comparable to previous ones.

### Metrics

- **AC13** — **Execution accuracy** — the headline: correct ÷ total.
- **AC14** — **Gate 2 pass rate** — generated SQL that passed validation.
- **AC15** — **Execution rate** — SQL that ran without a database error.
- **AC16** — A **failure breakdown by category**: `rejected`, `database_error`,
  `timeout`, `no_sql_returned`, `provider_error`, and `wrong_result` — the last
  being the only one that means the model reasoned incorrectly rather than
  failed mechanically.
- **AC17** — Per-tier accuracy, so "89%" cannot hide 100% easy and 20% hard.
- **AC18** — Every failing case is recorded with its question, generated SQL,
  category and error — this is Iteration 5's backlog, and a run that only
  printed a percentage would throw it away.

### The runner

- **AC19** — Runs the **real path**: `answer_question()`, unmodified. No test
  harness that skips the prompt, the provider, or the safety layer.
- **AC20** — **The runner does not bypass the validator.** Reference queries go
  through `execute_sql()` like everything else — the standing rule in
  [`000-project.md` §4](000-project.md) names `run_evals.py` explicitly.
- **AC21** — A provider failure on one question does not abort the run; it is
  recorded as a failure of that question.
- **AC22** — The runner is deterministic in everything except the model: the
  same dataset in the same order, no shuffling, no sampling.

### Recording

- **AC23** — Each run appends to `EVALS.md` with: timestamp, **model id**,
  **prompt fingerprint**, **temperature**, dataset version and question count,
  headline accuracy, per-tier accuracy, and the failure breakdown.
- **AC24** — The **prompt fingerprint is derived, not declared** — a hash of the
  template rather than a version constant someone must remember to bump. A
  number recorded against the wrong prompt version is worse than one recorded
  against none.
- **AC25** — `EVALS.md` is **append-only in spirit**: bad numbers stay. A
  regression that gets quietly deleted destroys the value of the entire record
  ([`000-project.md` §7](000-project.md)).

### Resolving `002` AC19

- **AC26** — Every reference query in the dataset passes `validate_sql()`. This
  is the criterion currently sitting as a deliberate skip in
  `tests/test_validator.py`:

  > *If this gate rejects a query the benchmark depends on, the gate is wrong,
  > not the benchmark.*

  The skip is replaced by a real test that loads the dataset. **A gold query
  Gate 2 rejects is a Gate 2 bug**, and this is the first thing in the project
  that can detect one.

---

## 4. Non-goals

| Excluded | Why |
|---|---|
| LLM-as-judge scoring | The whole point of Text-to-SQL is that correctness is machine-checkable. Introducing a model to grade a model would give up the one property that makes this benchmark trustworthy |
| Partial credit, fuzzy matching | A query is right or it is not. Partial credit is a knob that can be turned until the number looks good |
| Retries or self-correction in the runner | Iteration 4 adds those *to the agent*, and the improvement must show up as a delta. A runner that retried would measure something other than what ships |
| Improving accuracy | Explicitly not this iteration. Measure first, then Iteration 5 |
| CI integration | Iteration 8 |
| Latency or cost tracking | Iteration 7. Interesting, but not what this number is about |
| Comparing against Spider/BIRD | Different schemas and different rules. A number that is not comparable to a public leaderboard is fine; one that is silently *incomparable while looking comparable* is not |

---

## 5. Contracts

```yaml
# evals/questions.yaml
version: 1
dataset: chinook
fingerprint:
  track: 3503          # AC12 — abort if the data underneath changed
questions:
  - id: easy-001
    tier: easy
    question: How many tracks are there?
    gold_sql: SELECT count(*) FROM track
    ordered: false
    covers: [track]
```

```python
# evals/run_evals.py
@dataclass(frozen=True)
class CaseResult:
    id: str; tier: str; question: str
    generated_sql: str; correct: bool
    category: str          # "" when correct; else the failure kind
    error: str

@dataclass(frozen=True)
class EvalReport:
    accuracy: float
    gate2_pass_rate: float
    execution_rate: float
    per_tier: dict[str, float]
    breakdown: dict[str, int]
    cases: tuple[CaseResult, ...]
    model: str; prompt_fingerprint: str; temperature: float
```

`wrong_result` is a scorer-assigned category, not one `execute_sql` produces —
it is the case where everything worked mechanically and the answer was still
wrong. Keeping it distinct from `database_error` is what separates "the model
cannot reason about this schema" from "the model does not know this column
exists", and those have different fixes.

---

## 6. Verification

- Scorer tests are **pure**: comparison logic against synthetic result sets, no
  database and no LLM. Every case in §2 becomes a test.
- Dataset tests need only the database: every `gold_sql` parses, validates
  (AC26), executes, and returns at least one row. **A gold query returning zero
  rows is a broken question**, not a hard one — it makes any wrong answer that
  also returns nothing score as correct.
- The runner is tested against a fake provider with scripted responses, so a
  full scoring pass runs with no API key.
- **Mutation checks**: break numeric normalisation and a known-correct case must
  go red; ignore the `ordered` flag and an ordered question must stop
  discriminating.

---

## 7. Decisions — RESOLVED 2026-09-03

- **Q-A — Reference SQL.** Standard practice, self-maintaining, and it formally
  verifies Gate 2 against the benchmark, closing `002` AC19.
- **Q-B — Column order matters.** Positional tuple comparison; set-of-values
  comparison introduces row-deduplication bugs.
- **Q-C — Normalise to `Decimal`, compare to 6 places.** Absorbs float casting
  without losing precision.
- **Q-D — Systematic schema-derived coverage, then human review.** Every
  relation, every FK join path, and every SQL clause category. Authorship stated
  in `EVALS.md`.
- **Q-E — Single pass by default, `--repeat N` supported.** The baseline is run
  with `--repeat 3` so run-to-run variance is documented rather than assumed.

<details>
<summary>Original framing of all five, kept for the record</summary>

### Open questions

- **Q-A — Reference SQL, or literal expected rows?** *My lean: reference SQL.*
  It is self-maintaining, it is what Spider and BIRD do, and hand-computing 50
  expected result sets would introduce more errors than it catches. The cost is
  that a wrong gold query silently defines wrong truth — mitigated by AC26 and
  by the zero-rows check in §6.

- **Q-B — Does column order matter?** Measured in §2: `(name, count)` versus
  `(count, name)` are unequal as tuples, equal as sets of values. *My lean:
  column order matters* — compare row tuples positionally. Set-of-values
  comparison loses duplicate values within a row, so `(5, 5)` and `(5,)` would
  match, which is worse than being slightly strict. But it will under-credit
  genuinely correct answers, and that shows up in the baseline as a lower number
  than the model deserves. Your call which error you prefer.

- **Q-C — Numeric tolerance?** `Decimal` versus `float` is measured and must be
  handled. *My lean: normalise to `Decimal` and compare to 6 decimal places.*
  Enough to absorb float representation, tight enough that a genuinely wrong
  aggregate still fails.

- **Q-D — Who writes the questions, and how do we stop the benchmark flattering
  the system?** **The most important question in this spec, and it is not
  technical.**

  If I write both the prompt and the eval set, I will unconsciously write
  questions the prompt handles. The number would then measure agreement between
  two things I wrote, which is exactly the failure mode this iteration exists to
  prevent.

  *My lean:* derive the set **systematically from the schema** — every relation,
  every FK join path, each SQL feature (aggregate, group, having, subquery,
  self-join, date filter, set operation) — so coverage decides the questions
  rather than intuition. Then **you review and add**, particularly hard cases.
  Worth stating in `EVALS.md` who wrote the set, so the number carries its own
  caveat.

- **Q-E — One pass, or repeat for variance?** Determinism measured at 1 distinct
  SQL over 5 identical calls, but that was one question. A 40-question run is
  ~30 seconds, so three passes is affordable. *My lean: single pass by default,
  with `--repeat N` reporting the spread*, and the first baseline recorded with
  a repeat so `EVALS.md` documents how noisy the metric actually is.

---

</details>

---

## 8. What this unblocks

Iteration 4 replaces single-shot with the agent loop, and its done-when is
*"eval accuracy jumps measurably"* — which is only checkable if this iteration
produced a trustworthy number first.

It also closes the last outstanding item from Iteration 1: the deliberate skip in
`tests/test_validator.py` becomes a real assertion (AC26).
