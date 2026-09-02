# 006 — Evaluation Suite: Implementation and Test Plan

> **STATUS: IMPLEMENTED — T1 through T7 complete, 2026-09-03.**
> Spec: [`006-evals.md`](006-evals.md). Result: [`EVALS.md`](../EVALS.md).
>
> **D-1 resolved: `--record`.** Unflagged runs print to stdout only.
> **D-2 resolved: `expect:` on the `easy` tier only**, rejected by the loader
> anywhere else.
>
> ### What the plan got wrong
>
> Recorded because a plan that is only ever read forwards teaches nothing.
>
> 1. **"`bool` before `int`" was necessary but not sufficient.** §3 said
>    ordering the type checks fixes the boolean trap. It does not:
>    `Decimal("1.000000") == True` is also `True`, and their hashes agree, so
>    `Counter` merges them regardless of check order. Booleans are wrapped in a
>    distinct type (`_Bool`) instead.
> 2. **The `Decimal(str(x))` test in §6 did not discriminate.** `0.1 + 0.2`
>    quantises to `0.300000` under both constructions, so the mutation survived
>    a green suite. `4.0000005` is one of 388 values under 400 where they
>    genuinely disagree.
> 3. **Three questions were ambiguous or non-discriminating**, found by gold
>    verification before any scored run — see §2's ordering discipline paying
>    for itself. Details in `evals/questions.yaml`.
> 4. **§6's gold checklist was incomplete.** "Parses, validates, executes,
>    returns ≥1 row" misses ties in an ordered gold, truncation at the row cap,
>    and duplicate identical rows. All three are now asserted.
> 5. **The baseline is 92.5%, not the "mediocre" number this iteration
>    predicted.** The reading, and its caveats, are in `EVALS.md`.

---

## 1. Approach

```
questions.yaml ──load & validate──> Question[]        AC1-AC6
      │
      ├── gold_sql ──> execute_sql() ──> reference rows   AC7, AC11, AC20
      │
      └── question ──> answer_question() ──> AnswerResult AC19
                              │
                        normalise + compare               AC8-AC10
                              ▼
                        CaseResult  ──aggregate──> EvalReport ──> EVALS.md
```

Nothing new touches the database or the model directly. The runner is a
consumer of Iteration 1 and 2 machinery, which is what makes AC19 ("runs the
real path") cheap to honour rather than something to engineer around.

---

## 2. Coverage, computed rather than chosen

Your Q-D answer says coverage picks the questions. Measured from the live
schema:

- **12 relations** (11 tables + 1 view)
- **11 foreign-key join paths**, including one self-reference
  (`employee.reports_to → employee`) and two two-key relations
  (`invoice_line`, `playlist_track`)
- **date/numeric columns** exist on `employee`, `invoice`, `invoice_line`,
  `track` and `invoice_totals` — so date-filter and aggregate questions have
  somewhere to live

### Proposed matrix — 40 questions

| Tier | Count | Shape | Coverage obligation |
|---|---|---|---|
| `easy` | 14 | single relation, no join | **every one of the 12 relations at least once**, plus 2 spare for count/filter variety |
| `medium` | 16 | 1–2 joins, `GROUP BY`, `HAVING`, `ORDER BY … LIMIT` | **every one of the 11 FK paths at least once**, plus aggregate and date-filter cases |
| `hard` | 10 | 3+ joins, subqueries, self-join, window function, set operation | one per SQL feature category |

Feature categories for the `hard` tier, one question each: correlated subquery,
scalar subquery, `self-join` (the `employee.reports_to` path), window function,
set operation (`UNION`/`EXCEPT` — which also exercises `002` AC25 end to end),
`HAVING` with an aggregate predicate, three-table join, `CASE` expression,
`DISTINCT` with aggregation, and a date-range aggregate.

**Coverage is asserted by a test, not by intention.** `covers:` on each question
names the relations it touches; a test unions them and fails if any relation
from `get_schema()` is missing. Same for FK paths. That way a schema change that
adds a relation breaks the eval suite until someone writes a question for it —
which is the correct pressure.

### The ordering discipline

This matters more than the matrix. **All 40 questions and their gold SQL are
written and verified before the eval is run even once.**

The risk your Q-D answer names is that I write questions the prompt happens to
handle. Deriving from coverage helps, but the decisive protection is not looking
at the score while authoring. Concretely:

1. write all questions + gold SQL from the schema;
2. verify every gold query parses, validates, executes, returns ≥1 row;
3. **only then** run `answer_question` over the set;
4. record whatever number comes out, including if it is bad.

If step 3 produces something embarrassing, the fix belongs in Iteration 5, not in
step 1. `EVALS.md` records that the set was agent-authored and human-reviewed, so
the number carries its caveat (your Q-D).

---

## 3. Comparison, in detail

```python
def normalise(value):
    if isinstance(value, bool):            # bool is an int subclass — must
        return value                       # come first or True becomes 1.000000
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)).quantize(SIX_PLACES, rounding=ROUND_HALF_UP)
    return value
```

Three details that are easy to get wrong:

- **`bool` before `int`.** `isinstance(True, int)` is `True` in Python, so a
  boolean column would be normalised into `Decimal("1.000000")` and compare
  equal to the integer 1.
- **`Decimal(str(x))`, not `Decimal(x)`.** Constructing from a float directly
  carries the binary representation — `Decimal(2328.6)` is
  `2328.599999999999909...`, and quantising that is a coin flip at the boundary.
- **`Counter`, not `sorted`, for unordered comparison.** Rows can mix `None`,
  `str` and `Decimal`, and Python 3 refuses to sort those. Every normalised
  value is hashable, so a multiset comparison works where a sort raises.

Ordered questions compare row lists positionally; unordered compare
`Counter(rows)`. Column names are dropped before comparison (AC8); column order
is significant (your Q-B).

---

## 4. Files

| File | Status | Contents |
|---|---|---|
| `evals/questions.yaml` | exists, empty | The 40 questions |
| `evals/__init__.py` | **new** | package marker, so tests can import the runner |
| `evals/dataset.py` | **new** | loading + shape validation (AC6) |
| `evals/scoring.py` | **new** | `normalise`, `results_match`, metric aggregation |
| `evals/run_evals.py` | exists, empty | CLI, orchestration, `EVALS.md` writing |
| `EVALS.md` | exists, empty | the log |
| `api/requirements-dev.txt` | edit | add `pyyaml` |
| `tests/test_eval_dataset.py` | **new** | gold queries validate + execute + non-empty; coverage |
| `tests/test_eval_scoring.py` | **new** | pure comparison tests |
| `tests/test_eval_runner.py` | **new** | full pass against a fake provider |
| `tests/test_validator.py` | edit | **replace the AC19 skip with a real test** |

`pyyaml` goes in **dev** requirements, not runtime: `evals/` is not shipped in
the API image, and adding a parser to the production image for a benchmark that
never runs there would be wrong.

---

## 5. `EVALS.md` format

Append-only in spirit (AC25). One block per run:

```markdown
## 2026-09-03 14:02 — baseline

| | |
|---|---|
| Model | `openai/gpt-oss-120b` |
| Temperature | 0.0 |
| Prompt fingerprint | `a3f19c8b2e04` |
| Dataset | `questions.yaml` v1, 40 questions |
| Authorship | agent-derived from schema coverage, human-reviewed |
| Passes | 3 (`--repeat 3`) |

**Execution accuracy: 62.5% ± 2.5%** (25/40, spread 24–26 across 3 passes)

| Tier | Accuracy |
|---|---|
| easy (14) | 92.9% |
| medium (16) | 62.5% |
| hard (10) | 20.0% |

| Failure | Count |
|---|---|
| wrong_result | 9 |
| database_error | 4 |
| rejected | 2 |

Notes: …
```

The **prompt fingerprint is a hash of `SYSTEM_TEMPLATE`** (AC24), not a version
someone bumps. A number recorded against the wrong prompt version is worse than
one recorded against none, and the failure mode of a manual constant is
forgetting it exactly when a change matters.

---

## 6. Test plan

**Pure** — `tests/test_eval_scoring.py`, no database, no model. Every §2
measurement from the spec becomes a case:
- `Decimal('2328.60')` matches `2328.6`
- column names ignored; column order significant
- `True` does not match `1` (the `bool`/`int` trap)
- `None` compares equal to `None` and unequal to `0`
- unordered comparison handles rows mixing `None`, `str`, `Decimal`
- ordered questions reject a permuted row order; unordered accept it
- duplicate rows are preserved — `[(1,), (1,)]` ≠ `[(1,)]`

**Database only** — `tests/test_eval_dataset.py`:
- every `gold_sql` **passes `validate_sql()`** — this is `002` AC26/AC19
- every `gold_sql` executes and returns **≥1 row** (a zero-row gold makes any
  empty wrong answer score correct)
- ids unique and stable-looking; tiers valid; `covers` names real relations
- coverage: every relation and every FK path is exercised
- the dataset fingerprint matches the live database (AC12)

**Fake provider** — `tests/test_eval_runner.py`: a scripted provider returning a
mix of correct SQL, wrong-but-valid SQL, rejected SQL and a refusal, asserting
the report's accuracy, per-tier split and breakdown are all right. Runs with no
API key.

**Mutation checks:**

| Mutation | Expected |
|---|---|
| Drop numeric normalisation | a `Decimal`/`float` case goes red |
| Ignore the `ordered` flag | an ordered question stops discriminating |
| Compare column names too | an aliased-gold case goes red |
| Use `set` instead of `Counter` | the duplicate-rows case goes red |
| Skip the fingerprint check | a seeded-data-changed test goes red |

---

## 7. Decisions I need from you

**D-1 — Does the runner write `EVALS.md` by default, or only with `--record`?**
*My lean: `--record`.* Development runs and debugging passes should not append
to a file whose value is that every entry is real. The baseline run then uses
`--record` deliberately. The counter-argument is that a forgotten flag means a
real result goes unlogged.

**D-2 — What happens when a gold query is wrong?** Reference SQL silently
defines truth, so a mistake in my gold makes a correct answer score as wrong —
or worse, a wrong answer score as right. Beyond the §6 checks (validates,
executes, ≥1 row), I can add a **sanity assertion per question**: an optional
`expect:` field carrying something cheap and independently known — a row count,
or a value that must appear.

*My lean: add it for the `easy` tier only*, where the answer is knowable by
inspection (`track` has 3503 rows), and leave `medium`/`hard` to the gold query
alone. Full expected-row snapshots would be Q-A's rejected option arriving by
the back door.

---

## 8. Risks

| Risk | Handling |
|---|---|
| **The benchmark flatters the system** | §2's ordering discipline: questions and gold written and verified before the first scored run. Authorship recorded in `EVALS.md` |
| A wrong gold query defines wrong truth | AC26 + executes + ≥1 row, plus D-2's `expect:` on easy questions |
| The number is noisy and gets over-read | `--repeat 3` for the baseline, spread recorded alongside the mean |
| Data drift makes runs incomparable | Fingerprint asserted before scoring (AC12); the run aborts rather than producing a number |
| Column-order strictness under-credits | Accepted, your Q-B. It biases the baseline *down*, which is the safe direction for a number everything else is a delta from |
| 40 live LLM calls per pass, 120 with `--repeat 3` | ~0.7s each measured, so ~90s. Free tier is fine |

---

## 9. Proposed decomposition

| # | Task | Verified by |
|---|---|---|
| **T1** | `evals/scoring.py` — normalisation and matching, pure | All of §6's pure cases + mutation checks |
| **T2** | `evals/dataset.py` — loading and shape validation | AC6, malformed-dataset tests |
| **T3** | Author the 40 questions + gold SQL; verify each | AC1–AC5, AC26, coverage tests. **No scored run yet** |
| **T4** | Replace the `002` AC19 skip with a real test | The skip is gone |
| **T5** | `evals/run_evals.py` — orchestration and metrics | Fake-provider runner tests |
| **T6** | `EVALS.md` writing + CLI flags | Format test |
| **T7** | **Run the baseline** with `--repeat 3` and record it | A number exists in `EVALS.md` |

T3 before T5 is the ordering discipline made structural: the runner does not
exist when the questions are written, so the score cannot influence them.
