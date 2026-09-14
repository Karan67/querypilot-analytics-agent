# 014 — Iteration 11 plan: Test isolation

Spec: `specs/014-test-isolation.md`. Measurements: `specs/014-test-isolation-census.json`.

---

## 1. Approach

Three mechanisms, landed in an order chosen so that each one's failure is
unambiguous.

1. **Measure** (T1). An instrument that says, per test, whether Postgres was
   really touched and whether the dependency was declared. It produced §2 and it
   does not go away afterwards — T5's permanent guard is driven by the same
   plugin, which is what makes committing it worth more than "so §2 reproduces".
2. **Declare** (T2, T4). Register a `needs_db` marker, then apply it where the
   census says it belongs and drop `autouse=True`.
3. **Prohibit** (T3, T5). Make an undeclared database read raise at the exact
   test that made it, then assert the marked set and the closure set are equal.

**The prohibition is built before the partition and wired after it.** At T3 the
fixture ships **not autouse**: every test currently has `configured_database` in
its closure and none carries the marker, so wiring it first would put the whole
suite in the red at once and the resulting failures would say nothing about which
tests are actually misdeclared. This is the repository's own precedent —
`authed_client`'s docstring records that Iteration 10 landed the credential logic
unwired at T2 and only turned the gate on at T4, "because wiring it first would
have put 100 call sites in the red at once."

The split between T3 and T4 also buys a free bisect: a T4 failure means *a marker
is missing*; a T5 failure after a green T4 means *this test reaches the database
by a route the census did not see*. Those want different fixes.

---

## 2. The instrument

### 2.1 The listener attaches to the `Engine` class

`sqlalchemy.event.listen(Engine, "before_cursor_execute", …)` at
`pytest_configure`, not to `get_engine()`'s instance.

`get_engine` is `lru_cache`d, and `tests/conftest.py`, `tests/test_execution.py`,
`tests/test_schema_tool.py` and `tests/test_tools.py` all call
`get_engine.cache_clear()` mid-session. Each clear means the next `get_engine()`
returns a *different* `Engine` with no listener on it, so an instance-level
attachment goes quietly blind partway through the run and reports zeros for
everything after. Class-level registration covers every instance including ones
constructed later, which also catches `conftest`'s throwaway probe engine —
§2.5 of the spec exists only because the probe was visible.

`tests/test_request_round_trips.py::counting_round_trips` is the working
precedent. It attaches per-instance because it lives inside a single test and
never meets a `cache_clear()`.

### 2.2 Counting `create_engine` would measure the wrong thing

Not only because of the cache: **`create_engine` does not connect.** SQLAlchemy's
pool is lazy and `create_engine` against a dead host succeeds. A call count
measures *intent to be able to connect*; the census needs *did traffic happen*.

### 2.3 The closure walk excludes autouse, and says so loudly if it breaks

`FuncFixtureInfo.initialnames` is the attribute that looks right and is wrong: it
folds autouse names in, and while `configured_database` is autouse that makes the
answer "yes" for all 1,348 items. The walk uses `argnames` plus the arguments of
any `usefixtures` marker, resolved transitively through `name2fixturedefs`.

That reads `item._fixtureinfo`, a private attribute, so it carries landmarks —
three files covering the three distinct paths (direct parameter, transitive,
module-level `usefixtures`). If pytest changes shape the walk returns empty sets,
every disagreement disappears, and the census reports a perfectly clean suite.
The landmarks make that impossible to miss. Verified non-vacuous in the committed
run: 31 / 13 / 1 declared items respectively.

Once T4 drops `autouse=True` the private walk and pytest's public
`item.fixturenames` become equivalent. The plugin computes both and reports
`closure_walk_agrees_with_fixturenames`, which is `false` today and must be
`true` after T4 — that is what lets T5's permanent guard use the public attribute
and leave the private walk behind in `tools/`.

---

## 3. The prohibition

### 3.1 `DatabaseAccessProhibitedError` subclasses `BaseException`

A deliberate departure from the house rule ("subclass the narrowest stdlib base
that fits"), justified by a named `except` clause rather than by taste.

`api/db/introspection.py:342` reads `except (SQLAlchemyError, RuntimeError)` and
re-raises as `SchemaIntrospectionError`. It has to: `get_engine()` raises
`RuntimeError` when the DSN is unset. So a `RuntimeError` base — the one house
style would pick, matching `SchemaIntrospectionError(RuntimeError)` — is
**swallowed on the exact call path the class exists to police**, and
`tests/test_schema_tool.py::test_ac17_unreachable_database_raises_schema_error`
goes green while the prohibition fires.

That is CLAUDE.md's recurring shape: *a default elsewhere silently stands in for
the code under test.* `BaseException` is the narrowest base no `except Exception`
can reach. The only `except BaseException` in `api/` is `api/http/cache.py:152`,
which records and re-raises, so it is not a hole. `api/db/` contains no
`except Exception` and no bare `except` today, and a structural test keeps it so.

Also rejected: `SQLAlchemyError`. It is one word, it would look like it worked,
and it would let `execute_sql()` categorise the prohibition into an
`ExecutionResult` and `/health` return a tidy 503 — converting a fail-loud guard
into a silently swallowed one.

### 3.2 Patch `create_engine`, not `get_engine`

`api/db/execution.py:30` binds `get_engine` at import time, so patching its home
leaves the one production call site pointing at the original — the trap
`tests/test_request_round_trips.py:174-177` already documents. `create_engine` is
resolved from `api.db.engine`'s module globals **at call time**, inside
`get_engine`'s body, so one `setattr` is honoured by every caller.

Patching `get_engine` would also destroy `cache_clear`, which `conftest.py` and
eight other sites call. Substituting a stub that carries a no-op `cache_clear`
means hand-building a fake `lru_cache`, and then "delete the prohibition" becomes
indistinguishable from "the stub's `cache_clear` silently did nothing" — an
unfalsifiable defence, which is the thing this project mutation-tests to avoid.

The wrong choice cannot be made quietly: because of `cache_clear`, retargeting to
`get_engine` raises `AttributeError` in `conftest.py` immediately. A free
tripwire, recorded in the module docstring.

### 3.3 Evict before patching

`configured_database` is session-scoped and leaves a **live Engine in
`get_engine`'s `lru_cache`**. An unmarked test running afterwards calls
`get_engine()`, gets a cache hit, connects, and never calls `create_engine` — so
patching without evicting is a defence that is correct on the first test of the
session and inert on every one after it.

Setup: dispose the cached engine, `cache_clear()`, then patch. The dispose is not
tidiness — each eviction otherwise abandons a pool of up to 5 connections, and
`pool_size=5, max_overflow=5` against a default `max_connections=100` makes that
a real ceiling. `get_engine()` when the cache is warm is a pure cache hit, so the
dispose costs no connection.

Teardown: unpatch, evict again. That looks vacuous — an unmarked test cannot have
built an engine — and is not: HANDOFF.md §6 records that `monkeypatch.undo()` in
a test body reverts conftest's autouse fixtures, which once wrote a real 32 KB
database to `C:\data` for an entire iteration. If the prohibition were ever
reverted mid-test, teardown eviction stops the engine escaping into the next file.

### 3.4 A private `MonkeyPatch`, not the shared fixture

`pytest.MonkeyPatch.context()`. The shared `monkeypatch` fixture is exactly what
`undo()` reverts, and the prohibition is the one fixture that must be immune to
it. It also keeps the prohibition out of `monkeypatch`'s ordering graph, which it
has no reason to be in.

### 3.5 A marked test does not silently no-op

`isolation_decision(marked, closure)` returns `allow` only when both hold. Both
mismatches return `inconsistent` and raise, naming the missing half:

| marked | closure | decision |
|---|---|---|
| yes | yes | `allow` |
| yes | no | `inconsistent` — runs with no probe, against whatever `_load_dotenv()` left in the environment |
| no | yes | `inconsistent` — the prohibition would fire under a test that plainly asked for a database |
| no | no | `prohibit` |

`marked and not closure` is the failure where someone adds the marker to silence
the prohibition without wiring the fixture. That test cannot skip when the stack
is down, so it connects to the developer's real database silently.

Factored into a **pure function** because the repository will contain neither
inconsistent combination once T5 lands, so they are testable only on synthetic
input. This repo has been bitten before by a scan that stayed green when its
discrimination was deleted.

---

## 4. Files and decomposition

| | task | files | state |
|---|---|---|---|
| **T1** | the instrument, and §2 | `tools/measure_db_access.py`, `specs/014-test-isolation-census.json`, `specs/014-*.md` | **done** |
| **T2** | register the marker | `pytest.ini` | **done** |
| **T3** | the prohibition, unwired | `tests/isolation.py`, `tests/test_isolation.py` | **done** |
| | **⏸ PAUSE — the partition is the irreversible part and is reviewed against measured numbers before it is applied** | | |
| **T4** | the partition | `tests/conftest.py`, ~18 test modules | |
| **T5** | wire it, and enforce equality | `tests/conftest.py`, `tests/test_ci_guards.py` | |
| **T6** | CI lane floor and the record | `ci/require_database_lane.py`, `.github/workflows/ci.yml`, charter §8, `HANDOFF.md`, `CLAUDE.md` | |

`tests/isolation.py` is imported as `tests.isolation`, never bare `isolation`:
`tests/` has no `__init__.py` and pytest's `prepend` import mode makes both
reachable, which would give two module objects, two error classes, and an
`except` that mysteriously does not catch.

---

## 5. Decisions

- **D-1 — `BaseException`, not `RuntimeError`.** §3.1. The only decision here that
  contradicts house style, and the only one backed by a specific `except` clause.
- **D-2 — the marker does not auto-apply the fixture.** Injecting
  `configured_database` into marked items would make `marked ⊆ closure` true by
  construction — half of AC3 becomes a tautology. Both are written explicitly and
  kept in sync by a test, which is how this repo already handles `DEFAULT_FLOOR`
  against the workflow's argument, and the CI interpreter against the Dockerfile.
- **D-3 — the equality guard is a test that shells out, not a collection hook.**
  `pytest_collection_modifyitems` sees only the selected items, so any `-k` or
  single-file run would re-litigate a whole-suite invariant against a subset and
  report a misleading partial verdict. It also fails with exit 3/4 and **no junit
  report**, which `ci/require_executed_tests.py` would then surface as a floor
  failure — two belts firing, neither naming the cause.
- **D-4 — tests that need `create_engine` real in order to replace it are marked
  like any other.** They make no traffic (`closure_without_traffic`, 223 of them)
  but they are database tests: they declare the fixture, probe, and skip with the
  stack down exactly as they do today. No second marker, no exemption category.
- **D-5 — the census artifact is committed at 426 KB.** It is the receipt for §2
  and the input to T5's guard. Regenerated rarely and by one command.

---

## 6. Test plan and mutations

Every defence is removed and a test is required to go red. Mutations `a`–`h` run
before the T3 pause; `i`–`l` accompany T4–T6.

| # | mutation | must go red |
|---|---|---|
| a | remove the `event.listen(Engine, …)` registration | a test asserting a statement is *counted* — not that a listener is *registered* |
| b | attach the listener to `get_engine()`'s instance | traffic after a known `cache_clear()` drops to zero |
| c | closure walk returns `set()` | the three landmarks |
| d | closure walk uses `initialnames` | closure covers every item on the pre-change tree |
| e | delete `--strict-markers` | behavioural: `--collect-only -o markers=` must become a collection error naming `needs_db` |
| f | error base becomes `RuntimeError` | an unmarked `get_schema()` raises `DatabaseAccessProhibitedError`, not `SchemaIntrospectionError` |
| g | retarget the patch to `get_engine` | `conftest.py`'s `cache_clear()` raises `AttributeError` |
| h | `inconsistent` branch becomes a silent `return` | `isolation_decision` on synthetic input |

---

## 7. Risks

- **The CI floor clears by 112 tests.** §2.7. Had the hermetic share been the
  ~82% assumed before measuring, the floor would have been dead on arrival. T6
  adds a database-lane floor because a total cannot detect an empty lane.
- **`GATE_SUBJECT` in `tests/test_ci_guards.py`** must remain a marked test, or
  two existing subprocess guards pass for the wrong reason.
- **`QUERYPILOT_TESTS_REQUIRE_DATABASE` narrows** at T4 to the marked lane. Its
  docstring is quoted in `ci.yml` and `ci/require_executed_tests.py`; all three
  need the same edit or they drift.
- **Subprocess runs inherit `pytest.ini`'s `addopts`**, including its
  `--junitxml`, so every subprocess invocation must pass its own or it clobbers
  the parent's report mid-session.
