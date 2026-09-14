# 014 — Iteration 11: Test isolation

**Status:** drafted 2026-09-15. Discharges **B-15**.

Charter: `specs/000-project.md`. Plan: `specs/014-test-isolation-plan.md`.
Measurements: `specs/014-test-isolation-census.json`, regenerable with one command.

---

## 1. What this iteration is for

`tests/conftest.py` declares `configured_database` as
`@pytest.fixture(scope="session", autouse=True)`. It probes Postgres once and,
when nothing answers, skips — or, under `QUERYPILOT_TESTS_REQUIRE_DATABASE=1`,
fails — **every test in the suite**.

That is right for the tests that read the database and wrong for the ones that do
not, and `autouse=True` means nothing distinguishes them. B-15 was opened at
Iteration 10 T4 when the authentication suite, which tests a credential gate that
never touches Postgres, was measured at 64/64 skipped with the stack down.

This iteration replaces the ambient dependency with a declared one: a `needs_db`
marker, an opt-in `configured_database`, and a fail-loud prohibition that turns
an undeclared database read into an error at the exact test that made it. The
invariant it leaves behind is an equality, not an inequality — the set of marked
tests equals the set whose fixture closure reaches the database — because an
inequality is a leak list, and a leak list is a thing people append to.

The charter's own entry on B-15 names the risk this has to beat:

> splitting the autouse fixture so that database-independent tests survive its
> skip means deciding, per test, which kind it is, and getting that wrong in the
> *other* direction gives a test that runs without the database it silently
> needed.

So the partition is not a judgement made by reading code. It is measured.

---

## 2. Measurements

All figures below come from `tools/measure_db_access.py`, a pytest plugin
committed with this spec, and are reproduced in
`specs/014-test-isolation-census.json`. Regenerate with:

```bash
.venv/Scripts/python.exe -m pytest -p tools.measure_db_access \
    --db-access-out=specs/014-test-isolation-census.json \
    --junitxml=.pytest_cache/census-junit.xml -q
```

The instrument answers two different questions and the gap between them is the
finding. **Traffic** is a `before_cursor_execute` event: a statement reached a
cursor, which is ground truth. **Closure** is whether the test or its module
*asked* for `configured_database`, by parameter or by `usefixtures`, computed at
collection time with autouse deliberately excluded.

### 2.1 The partition: 888 of 1,348 tests never touch Postgres

Measured 2026-09-15, stack up, Python 3.12, pytest 8.4.2, SQLAlchemy 2.0.52.

| verdict | tests | share | meaning |
|---|---:|---:|---|
| `hermetic` | **888** | 65.9% | no traffic, no declaration — hostage to the autouse skip for nothing |
| `declared` | 172 | 12.8% | reads the database and says so |
| `closure_without_traffic` | 223 | 16.5% | declares the dependency, makes no traffic |
| `traffic_without_closure` | **65** | 4.8% | **reads the database and says nothing** |
| total | 1,348 | | 237 make traffic; 395 declare |

`closure_without_traffic` is not a defect. It is overwhelmingly tests that
declare the fixture and then monkeypatch over the engine to simulate an
unreachable host — they need `create_engine` to be real in order to replace it.

### 2.2 The leak is 65 tests across 12 files

These reach Postgres with nothing in their fixture closure that asks for it. They
pass today only because `autouse=True` supplies a database they never requested.

| tests | file |
|---:|---|
| 15 | `tests/test_ask_endpoint.py` |
| 12 | `tests/test_ask_recording.py` |
| 7 | `tests/test_cache.py` |
| 7 | `tests/test_daily_quota_guards.py` |
| 6 | `tests/test_auth.py` |
| 4 | `tests/test_multi_pass_recording.py` |
| 4 | `tests/test_quota_endpoint.py` |
| 3 | `tests/test_tools.py` |
| 2 | `tests/test_result_shapes.py`, `tests/test_schema_tool.py`, `tests/test_serialization.py` |
| 1 | `tests/test_execution.py` |

**12 files, not the 13 assumed before measuring, and the composition is not the
assumed one either.** `tests/test_orchestrator.py` and
`tests/test_token_accounting.py` were both expected here and neither belongs:
each already carries `pytestmark = pytest.mark.usefixtures("configured_database")`
at module level, so the whole file is declared.

### 2.3 `/ask` reaches Postgres through the answer cache, not through the agent

The largest leak is the one static reading gets wrong, and it is worth stating in
full because it is the argument for having built an instrument at all.

Every `/ask` test in `tests/test_ask_endpoint.py` replaces the agent —
`monkeypatch.setattr("api.main.answer", lambda question, **_: result)` — and
builds a synthetic `ExecutionResult`. Read statically, the file cannot reach the
database: `api/main.py` imports no introspection, and its only `execute_sql()`
call is in `/health`, which these tests never request.

Measured, 15 of its 31 tests issue **9 statements each**:

```
SET TRANSACTION READ ONLY
SET LOCAL statement_timeout = '10s'
SELECT c.relname, c.relkind::text FROM pg_class c JOIN pg_namespace n ...
```

Three introspection queries, each inside Gate 3's preamble. The route is
`/ask` → `_answer_or_replay()` → the answer-cache fingerprint →
`api/agent/fingerprints.py:163` → `get_schema()` → `execute_sql()`. The
fingerprint reads the schema **to build the cache key**, which happens *before*
`answer()` is reached — so stubbing the agent never prevented it, and never
could.

A test file whose every database call is stubbed, which nonetheless makes 135
round trips per run.

### 2.4 B-15's own premise was wrong: the auth suite is mixed

| | claimed at Iteration 10 T4 | measured |
|---|---:|---:|
| tests in `tests/test_auth.py` | 64 | **67** |
| that need Postgres | 0 | **6** |
| that this iteration frees | 64 | **61** |

Six tests call `GET /health`, which goes through `execute_sql()` by design —
`013-auth.md` §2.7 records that `/health` cannot be gated and routes through the
safety layer like everything else. They are at lines 275, 289, 514, 524 and 539.

**The marker must therefore be applicable per test, not only per module.** A
file-level partition would either skip six tests that should run or run six that
need a database. This is the single most consequential correction measurement
made to the plan.

### 2.5 The readiness probe is charged to whichever test runs first

`configured_database` opens a throwaway engine and issues `SELECT 1` to decide
whether to skip. Being session-scoped, that runs inside the **setup phase of the
first test that needs it**, and a per-test instrument attributes it there.

In the full run it lands on:

```
tests/test_ask_endpoint.py::test_ac4_no_endpoint_touches_the_database_directly
    {"setup": 1, "call": 0, "teardown": 0}   ["SELECT 1"]
```

The test asserting that no endpoint touches the database directly is the one the
census charges with a database round trip. Nothing is wrong with the test; the
statement is not its doing. This is why the instrument splits traffic by phase
rather than totalling it — without the split, one arbitrary test per session
reads as needing a database it does not use, and which test that is changes with
collection order.

### 2.6 A dead DSN still exits 0, with the count now at 1,348

The calibration run — same command, `TEST_DATABASE_URL` pointed at a closed port,
`QUERYPILOT_TESTS_REQUIRE_DATABASE` unset:

```
1348 skipped, 1 warning in 6.63s
exit=0
```

Traffic is zero for every test, which is what makes §2.1 a measurement rather
than an artifact: the instrument reports nothing when nothing ran.

It also re-measures the number `011-ship.md` §2.1 first recorded at 1,109 skipped
and exit 0, the failure `ci/require_executed_tests.py` exists to catch. **The
count has grown 21% since that guard was written and the exit code has not
changed.**

Closure is unaffected by the dead DSN — still 395 — because it is computed at
collection. That is what lets the equality guard run without a database.

### 2.7 The CI floor survives, with 112 tests of margin

`ci/require_executed_tests.py` sets `DEFAULT_FLOOR = 1000` against a suite where
a lost database currently means **0 executed**. After this iteration a lost
database means the hermetic lane still runs.

| | |
|---|---:|
| hermetic tests that would execute | **888** |
| `DEFAULT_FLOOR` | 1,000 |
| margin | 112 |

The floor holds, and it holds **by 8%**. Had the hermetic share been the ~82%
assumed before measuring, it would have been 1,105 — above the floor, and the
guard would have been silently dead the moment this iteration shipped. It is the
closest call in the iteration and it was decided by a measurement, not a guess.

The margin erodes as the suite grows, so §3 requires a second belt that measures
the database lane directly rather than inferring its health from a total.

---

## 3. Acceptance criteria

### The partition

- **AC1** — `configured_database` is not `autouse`. A test reaches it by carrying
  `@pytest.mark.needs_db` and requesting the fixture, both.
- **AC2** — `needs_db` is registered in `pytest.ini` and `--strict-markers` is in
  `addopts`, so a misspelled marker is a collection error rather than a silent
  no-op.
- **AC3** — the set of tests carrying `needs_db` **equals** the set whose fixture
  closure reaches `configured_database`. Both directions are asserted. No
  allow-list, no inequality, no exemption.
- **AC4** — the marker does not auto-apply the fixture. Both are written
  explicitly and a test keeps them in sync; injection would make half of AC3 a
  tautology.

### The prohibition

- **AC5** — a test without the marker that builds an `Engine` raises
  `DatabaseAccessProhibitedError`, naming the test and what to do about it.
- **AC6** — the error is **not** catchable by `except Exception`. Verified
  necessary: `api/db/introspection.py:342` catches `(SQLAlchemyError, RuntimeError)`
  and re-raises as `SchemaIntrospectionError`, so a `RuntimeError` base would be
  swallowed on the exact path the prohibition polices, and
  `test_ac17_unreachable_database_raises_schema_error` would pass **while the
  prohibition fired**.
- **AC7** — the prohibition survives a warm `lru_cache`. A marked test leaves a
  live engine in `get_engine`'s cache; an unmarked test running afterwards must
  not get a cache hit.
- **AC8** — the prohibition is immune to `monkeypatch.undo()` in a test body,
  which HANDOFF.md §6 records as having reverted conftest's autouse isolators for
  an entire iteration.

### What it buys

- **AC9** — `pytest tests/test_auth.py -m "not needs_db"` passes with the stack
  down: 61 tests run, 0 skipped.
- **AC10** — the database lane has its own floor in CI, so it cannot silently
  empty while the total stays above `DEFAULT_FLOOR`.

### Honest about itself

- **AC11** — the instrument that produced §2 is committed and its output is
  regenerable from one command recorded in this spec.
- **AC12** — the closure walk carries a non-vacuity guard. It reads a private
  pytest attribute; if that changes shape the walk returns empty sets and the
  census reports a clean suite, which is the loudest possible way to be wrong.

---

## 4. Non-goals

- **Not speeding the suite up.** The hermetic lane will be faster because it
  skips a probe, not because anything was optimised. No parallelism, no
  `pytest-xdist`.
- **Not reducing the number of tests that need Postgres.** §2.3's fingerprint
  read is a real dependency of the code under test; making `/ask` testable
  without a database is a change to `api/`, and this iteration does not touch
  `api/`.
- **Not a test-database fixture, factory, or transactional rollback harness.**
  The tests that need Postgres keep using the one they have.
- **Not retiring `QUERYPILOT_TESTS_REQUIRE_DATABASE`.** Its meaning narrows to
  the marked lane and its documentation is corrected; the flag stays.

---

## 5. Contracts this iteration must not break

- **Nothing bypasses the safety layer.** The prohibition patches
  `api.db.engine.create_engine`, which is upstream of `execute_sql()` and does
  not change how SQL is validated or run. No new exemption; the charter §4 count
  stays at one.
- **No test hardcodes a DSN.** The calibration run points `TEST_DATABASE_URL` at
  a closed port from the command line; nothing new is embedded.
- **`ci/require_executed_tests.py` keeps working unchanged.** §2.7 confirms the
  floor still fires. The new lane guard is additional, not a replacement.
- **The suite count does not fall.** 1,348 before, 1,348 after. This iteration
  moves where a dependency is declared; it deletes no test.
- **`tests/conftest.py` stays the only conftest.** The new module is imported by
  it, not a second collection root.

---

## 6. Risks

- **Getting the partition wrong in the direction that does not fail.** A test
  marked `needs_db` that does not need one is merely slow; a test that needs one
  and is not marked now raises rather than silently connecting. The prohibition
  is what makes the second class loud, which is why it lands before the partition
  is trusted.
- **`_load_dotenv()` at `tests/conftest.py:169`** `setdefault`s every `.env` key
  at import, including `QUERYPILOT_DATABASE_URL`. So `get_database_url()` never
  raises locally and any "the DSN is unset" defence is vacuous on a developer's
  machine and green in CI. This is why the prohibition patches a function rather
  than manipulating the environment.
- **`tests/test_ci_guards.py::GATE_SUBJECT`** must remain a marked test, or two
  existing subprocess guards pass for the wrong reason — a non-zero exit from the
  prohibition rather than from the require-database gate.
- **The private closure walk.** Mitigated by AC12's landmarks, and retired
  outright once AC1 lands: with autouse gone, pytest's public `item.fixturenames`
  becomes equivalent, and the census reports whether the two agree.

---

## 7. Open questions

All five were resolved before implementation began.

- **Q-A — spec and plan together, or spec first?** *Resolved: both, then
  implement T1–T3 and pause.*
- **Q-B — where do §2's numbers come from, given the prior sweep's output was
  never committed and has been lost?** *Resolved: rebuild the sweep as a
  committed instrument and re-run it.* Vindicated by §2.2 and §2.3 — the
  remembered figures named two files that were already declared and missed the
  largest leak entirely.
- **Q-C — derive the marker list from the instrument, or from the earlier
  reading?** *Resolved: from the instrument, per run.* "Do not hardcode from
  conversational memory."
- **Q-D — where does `DatabaseAccessProhibitedError` live?** *Resolved:
  `tests/isolation.py`, a new test-support module, keeping `api/` free of test
  scaffolding and `conftest.py` from growing past 396 lines.*
- **Q-E — should the marker imply the fixture?** *Resolved: no.* See AC4.
