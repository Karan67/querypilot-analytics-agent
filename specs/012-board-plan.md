# 012 — Iteration 9 plan: The board

Status: **DRAFT — presented 2026-09-11**, D-1 through D-7 open · Created: 2026-09-11

Implements [`012-board.md`](012-board.md), whose §7 is resolved. This document
says *how*, and surfaces the decisions the design itself raised.

> **Resolved by the spec, and binding here.** The schema cache is **retired**
> under the AC5 fingerprint gate · the request path **reads the schema once**,
> and threading is **filed rather than forced** if it compromises an interface
> boundary · the API's spend is **documented, never plumbed** — no mount, no
> `evals/` in the runtime image · a live 429 is **not provoked**; mid-run
> refusals reconcile from the captured fixture · B-13 gets **equalised
> diagnostics, a preserved local report, no retry**, and a budget of **20
> consecutive clean CI runs** · charter §6 gains a row for Iteration 9.

---

## 1. Approach

Two of the five answers change the request path and three change records. The
ordering below is driven by one constraint that is not obvious from the spec:

**The measuring instrument has to exist before the thing it measures moves.**
AC3 asks for the round-trip cost of a request to be asserted end to end. If that
test is written *after* the cache is removed, it pins whatever the code ended up
doing, and `HANDOFF.md` §6 already records the version of this mistake that cost
real time — nineteen green tests of `cached_schema()` did not notice the request
path had stopped calling it, because every one of them called it directly. So
the instrument is T2, it is written against **today's** behaviour, and the two
tasks that follow are judged by moving a number it already pins.

After that the order is:

1. **Read the schema once** (Q-B), while the cache still exists. It is a
   behaviour-preserving refactor, it is the task most likely to be *filed*
   rather than done, and finding that out before a deletion is cheaper than
   after one.
2. **Retire the cache** (Q-A). Last among the code tasks because AC5 can stop it
   dead, exactly as AC9 could stop Iteration 8 T5.
3. **Then the record tasks**, which touch no request path.

**The two path tasks compose, and the arithmetic is worth stating in advance so
the measurement can contradict it.** Round trips for one `/ask`, where a miss is
schema + schema + the generated `SELECT`:

| | miss | answer-cache hit |
|---|---|---|
| today, measured | **9** | **3** |
| after T3 (one read, cache kept) | 6 | 3 |
| after T4 (one read, no cache) | **12** | **9** |
| *(T4 without T3, for contrast)* | *21* | *9* |

In time, from §2.1's medians: a miss's schema cost goes 10.6ms → ~20ms (+9.4ms)
and a hit's 5.3ms → ~20ms, taking the measured 6ms hit to about 21ms. **Those
are predictions, and AC4 requires them measured.** They are here so that a
measurement which disagrees is visible as a disagreement rather than absorbed.

**Nothing in this iteration changes the prompt, the dataset or the scorer.**

---

## 2. Q-B and Q-A: one schema read, and then no cache

### 2.1 The instrument (T2)

A test that counts statements the database was actually asked to run, **through
the endpoint**, not through a module. The counting harness already exists —
`tests/test_schema_cache.py`'s `counting_round_trips()` listens on SQLAlchemy's
`before_cursor_execute`. It moves to a home that survives T4, since the file it
lives in is deleted there (D-2).

It pins two numbers against today's code, a miss and a hit, and it names the
schema reads it expects rather than only totalling them — a total alone goes
green if one read is removed and another added.

**Its mutation:** re-introduce a second schema read, and the miss assertion goes
red. That mutation is run at T2 against unmodified code, so the instrument is
proven to discriminate *before* it is trusted at T3 and T4.

### 2.2 Reading the schema once (T3)

`_deployed_fingerprints()` reads the schema to build the answer-cache key, and
`answer()` reads it again to build the prompt. The schema read in `api/agent/`
is threaded to the one in `api/agent/`; `api/main.py` only carries the value
between them.

```
api/agent/fingerprints.py   deployed_fingerprints() -> (schema, schema_fp, prompt_fp)
api/main.py                 passes `schema` through, never naming its type
api/agent/orchestrator.py   answer(..., schema=None) uses it or reads its own
```

**The boundary this must not cross is already asserted, and it survives.**
`tests/test_ask_endpoint.py::test_ac4_the_only_database_call_in_the_module_is_execute_sql`
requires `api/main.py` to import nothing from `api.db` except `execute_sql`, and
`::test_ac4_no_endpoint_touches_the_database_directly` forbids calling
`get_engine`, `text`, `connect` or `engine` there. Passing an opaque value
through does neither: main.py gains no import and makes no new call. Verified by
reading both tests, not assumed — the docstring on `_deployed_fingerprints`
records that an earlier attempt widened this test instead, and calls that *"how
a rule acquires its first undocumented exception"*.

**If that turns out to be wrong, this is the task that gets filed** (the user's
instruction, and §7's D-5 names the shape it would be filed as). The evidence
that it is wrong would be a signature change reaching outside `api/agent/`, or
either AC4 test needing a widening.

`answer(schema=...)` is additive and keyword-only, so every existing caller —
the eval runner, ~37 loop tests, the withheld-schema harness — is untouched.

### 2.3 Retiring the cache (T4)

`api/db/schema_cache.py` is deleted. There are exactly **two** production
importers, confirmed by walking every import in the repository:

| | today | after |
|---|---|---|
| `api/agent/fingerprints.py:56` | `cached_schema` | `get_schema` |
| `api/agent/orchestrator.py:53` | `cached_schema` | `get_schema` |
| `tests/conftest.py:317` | the sixth autouse isolator | deleted |
| `tests/test_schema_cache.py` | 22 tests, 518 lines | see D-2 |

The module's helpers have no other consumer: `fingerprint_of`,
`catalog_fingerprint`, `CATALOG_SIGNATURE_SQL` and its local `FINGERPRINT_LENGTH`
are used only inside it and its own tests. `evals/run_evals.py` imports
`FINGERPRINT_LENGTH` from `api/agent/fingerprints.py`, which is a different
constant that stays.

**One structural test must be updated and must not be deleted.**
`tests/test_cache.py::test_the_schema_cache_is_shared_with_the_eval_runner_deliberately`
asserts `"api.db.schema_cache.cached_schema"` is among the orchestrator's
imports. Its *purpose* is the asymmetry it pins — the answer cache is
deliberately not shared with `evals/`, the schema path deliberately is — and
that asymmetry survives retirement unchanged. It asserts
`api.db.introspection.get_schema` instead (D-3). Deleting it to make a suite
green is the failure §6 of the spec names.

The suite goes from **1,220 collected** to about **1,198** before this
iteration's additions. `ci/require_executed_tests.py`'s floor is **1,000**
executed, and CI executes all but the 3 live tests, so the floor is not
approached. Stated because a task that deletes 22 tests should check the guard
that counts them rather than discover it.

### 2.4 The stop condition (AC5)

**A gate, not a goal**, in the same words Iteration 8 T5 used. The rendered
schema must stay byte-identical and the recorded prompt fingerprints
`0d280c367c5e`, `91036a089282` and `f971d8787f0c` must still reproduce.

This is cheaper to satisfy here than it was at T5, and the reason is worth
stating so the check is not treated as ceremony: **T4 does not touch how a
schema is read or rendered.** `cached_schema()` returns whatever `get_schema()`
returned; removing it changes *when* the read happens, not what it produces.
`tests/fixtures/rendered_schema_compact.txt` and `rendered_schema_ddl.txt`
already exist from T5 and are the byte-level check.

So the expected outcome is that AC5 passes trivially — which is precisely why it
must actually be run. A gate that is expected to pass and is therefore not
executed is not a gate.

### 2.5 A closed iteration said the opposite, and that gets recorded

**`010-hardening.md` AC9 reads: *"The schema is introspected once and reused
(§2.4), and the reuse is invalidated on a schema change rather than trusted
forever."*** T4 makes that false. An iteration closed on that criterion.

It is amended in place, the way `011` amended charter §6 for deployment and the
demo video — **original text kept, dated note beneath** — because, in that
amendment's own words, *a commitment quietly edited to match what was built is
the failure `EVALS.md`'s append-only rule exists to prevent*. The note carries
§2.1's measurement and points at the B-14 entry. D-7 asks whether that is the
right instrument.

Worth recording alongside it: **the cost of this operation has been measured
three times and has been different each time** — 143ms at `010` §2.4, 99ms in
the module's own docstring, 19.98ms today. Each was correct when taken. The
criterion did not rot; the thing underneath it moved.

---

## 3. Q-E: B-13 keeps its evidence (T5)

Three changes, none of which is a retry.

**The message asymmetry.** `test_every_gold_query_executes` interpolates id,
category and error; `test_every_gold_query_returns_at_least_one_row`
interpolates ids only. They fail together today, so the category is always
available from the first — but only because they fail together, which is an
accident the pair should not depend on. The second is brought up to the first.

The message is built by a small named helper so that **it can be tested
directly**, against a constructed `gold_results` containing a failure. Asserting
on a test's own failure text is otherwise only possible by making a test fail,
and a test that must fail to prove itself is one nobody runs.

**The local report.** `pytest.ini` gains `--junitxml` in `addopts`, so every
local run leaves the full assertion text on disk without anyone remembering a
flag. Three properties were verified rather than assumed:

| | |
|---|---|
| the junit XML carries the full message | §2.4 of the spec — `message` attribute **and** element body, under `-q` |
| a command-line `--junitxml` overrides an `addopts` one | tested both ways; CI's `$JUNIT_REPORT` still wins, so `ci/require_executed_tests.py` is unaffected |
| pytest creates a missing parent directory | tested; works on a fresh clone |

No test currently reads `pytest.ini` or `addopts`, so nothing is coupled to the
change. D-1 asks where the file goes.

**The budget.** 20 consecutive clean CI runs, recorded in the charter's B-13
entry with the run id it starts from. D-6 asks what "consecutive" means when a
run goes red for an unrelated reason, which is not a pedantic question — four of
the thirteen CI runs so far were red and none of them was this.

**Explicitly not done:** no retry, no re-attempt on `connection_error`, no
widening of the fixture. The entry's own reasoning — *a retry is a place a real
failure can hide* — is untouched by twenty-nine clean observations.

---

## 4. Q-C and Q-D: the ledger (T6)

### 4.1 Mid-run reconciliation (AC9)

`ledger.reconcile()` has one caller today: the pre-flight probe. A refusal
arriving *during* a run carries the same authoritative figure and is discarded.

The body survives verbatim the whole way — `error=str(exc)` at
`orchestrator.py:429` and `:450`, into `CaseResult.error` — and
`limit_from_message` is already imported in `run_evals.py`. So the change is to
scan the completed run's cases for rate-limited ones naming **TPD**, and
reconcile.

It goes **after** `ledger.record(...)`, not before: `record()` adds and
`reconcile()` overwrites, so reconciling last leaves the provider's own total
standing rather than having the local estimate added on top of it. D-4 asks
which figure to take when several cases were refused.

**Tested against the captured payload** already in
`tests/test_rate_limit_telemetry.py` — `on tokens per day (TPD): Limit 200000,
Used 199301`. **Mutations:** delete the reconcile call and the ledger stays at
the local estimate; feed an **RPD** body and it must *not* reconcile, because a
request-per-day refusal says nothing about tokens.

**This does not close B-6.** The live leg — a real refusal, parsed, overwritten,
and read by the next run's pre-flight — stays open and its entry must keep
saying so. A strike-through here would read as finished.

### 4.2 Naming the blind spot (AC10)

`DailySpend.describe()` says `(local estimate)` or `(provider-reconciled)`. It
gains what the estimate cannot see, with §2.5's measured size.

**Asserted on the returned string, never on the module's source text** — this
project has written a structural test that matched its own docstring twice, and
an absence assertion that matched the comment explaining it twice more.

Nothing is plumbed. No bind mount, no `evals/` in the runtime image, no
`GET /history/data` call from the pre-flight.

---

## 5. Files and decomposition

| task | what | verified by |
|---|---|---|
| **T1** | Charter §6: a row for Iteration 9 | Read it. **Pause.** |
| **T2** | The end-to-end round-trip instrument (AC3) | Its own mutation, against unmodified code |
| **T3** | Q-B: read the schema once | Miss goes 9 → 6 trips. **Pause** — this is the task that may be filed |
| **T4** | Q-A: retire the cache (AC2, AC4, AC5); amend `010` AC9 | AC5 fingerprint gate; latency re-measured. **Pause.** |
| **T5** | Q-E: message parity, `addopts`, the budget | The helper's own test; mutation on the message |
| **T6** | Q-C + Q-D: mid-run reconcile, `describe()` | Captured payload; RPD negative case |
| **T7** | Board and handoff: §8 entries, `HANDOFF.md`, README if a number moved | `down` then `up` — **not** `down -v`, see below; full suite. **Pause.** |

**T7 does not do a `down -v` clean boot, and that is a change from Iteration 8
T7.** Nothing in this iteration touches `db/init/`, the Dockerfile or the compose
boot path, so a from-empty boot would verify nothing this iteration put at risk —
and it would discard the `querypilot_data` volume holding the **18 answers,
23,131 recorded tokens and 1 feedback mark** that §2.5's B-6 measurement was
taken from and that a later iteration may want to re-measure. Volumes are kept;
the boot is verified the way a developer actually restarts the stack.

**Deleted:** `api/db/schema_cache.py`, `tests/test_schema_cache.py`.
**Edited:** `api/agent/fingerprints.py`, `api/agent/orchestrator.py`,
`api/main.py`, `tests/conftest.py`, `tests/test_cache.py`,
`tests/test_eval_questions.py`, `pytest.ini`, `.gitignore` (D-1),
`evals/ledger.py`, `evals/run_evals.py`, `tests/test_daily_quota_guards.py`,
`specs/000-project.md`, `specs/010-hardening.md`, `HANDOFF.md`.
**New:** a home for the round-trip instrument (D-2 decides where).

---

## 6. Decisions

- **D-1 — Where does the local junit report go?** `addopts` needs a path.

  *My lean: `.querypilot/last-test-run.xml`.* `.querypilot/` is already
  gitignored and is already where this project keeps local, machine-specific
  state. The alternative is a new top-level ignore entry for a new file. The one
  thing that gives me pause is that `.querypilot/` also holds the spend ledger,
  which an autouse fixture isolates — and putting a *non*-isolated artefact in
  the same directory invites the assumption that it is isolated too. It is
  written by pytest at session end and read by nothing, so it is not the shared-
  state trap; it is only shaped like it.

- **D-2 — What happens to the 22 deleted tests, and where does the instrument
  live?** Nothing in `tests/test_schema_cache.py` tests anything that survives.

  *My lean: delete the file, and move two things out of it before it goes.* The
  `counting_round_trips()` harness becomes the instrument's, in a new
  `tests/test_request_round_trips.py`. And the *argument* in
  `test_introspection_really_is_the_expensive_thing` — the test that asked the
  B-14 question and was right to — is quoted into the charter's B-14 closure
  entry. A test that did its job and is then deleted without trace is how the
  reasoning gets lost and re-derived.

- **D-3 — How is the shared-with-`evals` test updated?** It asserts an import
  name that will not exist.

  *My lean: swap the name to `api.db.introspection.get_schema` and keep
  everything else, including the docstring's reasoning.* The asymmetry it exists
  to pin is unchanged: the schema path is shared with the eval runner and the
  answer cache is not. Its twin, `test_the_answer_cache_is_not_reachable_from_the_eval_runner`,
  is untouched.

- **D-4 — Which `Used` figure reconciles when more than one case was refused?**
  A run that hits TPD refuses every question after the first.

  *My lean: the maximum across the refused cases.* The provider's daily count
  only rises within a day, so the maximum is the latest true figure and is
  **order-independent** — it survives a change in how reports are iterated,
  which "the last one" would not. Taking the first would pin the ledger to the
  moment of the earliest refusal and understate.

- **D-5 — If threading compromises a boundary, what exactly gets filed?** The
  user's instruction is to file rather than force.

  *My lean: a B-15 entry naming the boundary that resisted, with the measured
  cost of not doing it — 3 round trips a miss with the cache, 9 without.* And
  T4 proceeds regardless, because Q-A does not depend on Q-B. Filing it also
  changes T4's expected numbers from 12 trips a miss to 21, which the report
  must state rather than quietly re-baseline.

- **D-6 — What does "20 consecutive clean CI runs" count?** Four of thirteen CI
  runs so far were red, and none was this.

  *My lean: count runs in which the gold-query pair actually executed and
  passed, and let an unrelated red run neither reset nor advance the count.* A
  build that fails at `docker compose up` is not evidence about B-13 in either
  direction, and a rule that resets on it would make the budget unreachable on a
  noisy week. Counted by hand from the charter entry's starting run id — a
  mechanical counter is CI infrastructure built for a debt we expect to close.

- **D-7 — How is `010` AC9 amended?** It is a criterion of a closed iteration
  that T4 makes false.

  *My lean: a dated note beneath the original text, which is left intact* —
  `011`'s charter §6 pattern exactly. The alternative, recording it only in the
  B-14 entry, leaves `010` reading as though the cache exists to anyone who
  opens it on its own, and specs in this project are read on their own.

---

## 7. Risks

- **T4 is a deletion, and deletions are where absence assertions rot.**
  `HANDOFF.md` §6 records five instances of a test that asserts something is
  missing and passes for the wrong reason. The structural test in D-3 is the
  specific one at risk: making the suite green by deleting it would remove the
  only thing pinning the product/benchmark asymmetry.
- **AC3's instrument is the criterion most likely to be satisfied by a test that
  proves nothing.** Counting inside the module is what already exists and is
  what missed the adoption gap. T2 exists as a separate task, before the
  changes, so that its mutation is run against code nobody has touched yet.
- **AC5 is expected to pass, which is the dangerous kind of gate.** §2.4 argues
  it should pass trivially. That argument is a reason to run it, not a reason to
  trust it.
- **T3 may be the wrong shape and is the task most likely to be filed.** It is
  sequenced before the deletion for that reason, and D-5 says in advance what
  filing it costs, so the decision is not taken under sunk cost at T4.
- **T6 closes half of B-6 and the entry must keep saying which half.** The
  strike-through habit is strong and a half-discharged item reads as finished.
- **This is a maintenance iteration and its main risk is scope.** Three of seven
  tasks change no behaviour at all. If a task starts growing a feature, that is
  the signal the spec's §6 warned about.
