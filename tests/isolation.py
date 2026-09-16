"""Refuse a database to any test that did not declare one.

**Import this as `tests.isolation`, never as bare `isolation`.** `tests/` has no
`__init__.py` and pytest's default `prepend` import mode puts the directory on
`sys.path`, so both spellings resolve — to two distinct module objects, with two
distinct `DatabaseAccessProhibitedError` classes, and an `except` clause that
mysteriously fails to catch. `tests/test_ci_guards.py` already imports
`tests.conftest` by that path for the same reason.

---

`tests/conftest.py` made `configured_database` `autouse=True`, which gave every
test a database whether it asked for one or not. `specs/014-test-isolation.md`
§2.2 measured what that was hiding: **65 tests across 12 files reach Postgres
with nothing in their fixture closure that asks for it.** They pass only because
something ambient supplies a connection.

This module makes that fail loudly and at the right place. An unmarked test that
tries to build an `Engine` gets `DatabaseAccessProhibitedError` naming the test
and what to do about it, instead of silently connecting to whatever DSN
`_load_dotenv()` left in the environment — which, on a developer's machine, is
their real database.

**At T3 the fixture below is not `autouse`.** Wiring it before the partition
exists would put the entire suite in the red at once and the failures would say
nothing about which tests are misdeclared. Iteration 10 landed its credential
logic the same way — unwired at T2, switched on at T4 — and `authed_client`'s
docstring records why. T5 turns this one on.
"""

from __future__ import annotations

import contextlib

import pytest

#: The marker a test carries to declare that it reads PostgreSQL. Registered in
#: `pytest.ini`, which also sets `--strict-markers` so a misspelling is a
#: collection error rather than a marker that attaches nothing.
NEEDS_DB_MARKER = "needs_db"

#: The fixture whose presence in a test's closure *is* the declaration. The
#: marker does not apply it; see `pytest.ini` for why the two are written
#: separately and kept in sync by a test.
DATABASE_FIXTURE = "configured_database"

#: A third category the census cannot see, found when the prohibition was wired.
#:
#: Some tests build an `Engine` **in order to watch it fail** -- pointed at a
#: closed port, to prove `execute_sql()` reports a connection error rather than
#: leaking a DSN, or that `get_schema()` raises `SchemaIntrospectionError`. They
#: call `create_engine` and issue **no statements at all**, because the
#: connection never opens. The census measures traffic, so it scores them
#: `hermetic`, and it is right: they pass with the stack down and belong in the
#: hermetic lane.
#:
#: That is the same lazy-pool fact that makes counting `create_engine` calls a
#: bad instrument, read from the other side: traffic cannot find a test whose
#: whole point is that no traffic happens.
#:
#: They are **not** `needs_db`. Marking them so would skip them exactly when the
#: database is unreachable, which is the one condition they exist to test. This
#: marker says only *step aside, the Engine is deliberate* and carries no
#: skipping behaviour and no fixture.
CONSTRUCTS_ENGINE_MARKER = "constructs_engine"

#: Attributes replaced while an undeclared test runs, as (module, attribute).
#:
#: **`api.db.engine.create_engine` is the seam.** It is resolved from
#: `api/db/engine.py`'s module globals at call time, inside `get_engine()`'s
#: body, so one `setattr` is honoured by every caller. Patching `get_engine`
#: instead would miss `api/db/execution.py:30`, which bound the name at import —
#: the trap `tests/test_request_round_trips.py:174-177` already documents — and
#: would destroy the `cache_clear` that `tests/conftest.py` and eight other sites
#: call.
#:
#: **`sqlalchemy.create_engine` is the belt.** `api/db/engine.py:23` copied that
#: binding at import, so the two names are independent objects and patching one
#: does not touch the other. No test constructs an engine that way today; the
#: entry costs one line and closes the category before someone opens it.
PROHIBITED_TARGETS = (
    ("api.db.engine", "create_engine"),
    ("sqlalchemy", "create_engine"),
)


class DatabaseAccessProhibitedError(BaseException):
    """A test without the `needs_db` marker tried to build an Engine.

    **Not an `Exception`, and the base class is the whole defence.**

    House style is to subclass the narrowest stdlib base that fits, which would
    give `RuntimeError` — matching `SchemaIntrospectionError`. Measured against
    the actual call path, that choice is silently fatal:

        # api/db/introspection.py:342
        except (SQLAlchemyError, RuntimeError) as exc:
            raise SchemaIntrospectionError(...) from exc

    That handler has to catch `RuntimeError`, because `get_engine()` raises one
    when the DSN is unset. So a `RuntimeError` base is **swallowed on the exact
    path this class exists to police**, re-reported as an error the suite already
    asserts, and
    `tests/test_schema_tool.py::test_ac17_unreachable_database_raises_schema_error`
    passes *while the prohibition is firing*. That is CLAUDE.md's recurring bug
    shape — a default elsewhere silently standing in for the code under test —
    and here it is two named `except` clauses rather than a hypothesis.

    `BaseException` is the narrowest base that no `except Exception` can reach.
    The only `except BaseException` in `api/` is `api/http/cache.py:152`, which
    records and re-raises, so it is not a hole. pytest reports a `BaseException`
    raised in a test body as an ordinary failure.

    Also considered and rejected: `SQLAlchemyError`. One word, it would look like
    it worked, and it would let `execute_sql()` categorise the prohibition into
    an `ExecutionResult` and `/health` answer a tidy 503 — turning a fail-loud
    guard into a swallowed one.
    """


def isolation_decision(
    *, marked: bool, closure: bool, constructs_engine: bool = False
) -> str:
    """What to do about a test that is `marked` and/or reaches the fixture.

    A pure function because the two `inconsistent` combinations will not exist in
    the repository once the equality guard lands, and a branch with no possible
    input is a branch nobody can prove still works. This project has been bitten
    by exactly that: a scan whose discriminating clause was deleted and stayed
    green, because no case in the tree separated the two implementations.

    - `allow` — declared both ways. Nothing is patched.
    - `prohibit` — declared neither way. Building an Engine raises.
    - `inconsistent` — declared one way only, in either direction. Both are
      errors worth naming rather than silently resolving:

      *Marked without the fixture* is the failure where someone adds the marker
      to quiet the prohibition. That test never probes, so it cannot skip when
      the stack is down, and it connects to whatever DSN is ambient.

      *The fixture without the marker* would otherwise raise
      `DatabaseAccessProhibitedError` under a test that plainly asked for a
      database, which names the wrong problem. The missing marker is the useful
      message.
    """
    if marked and closure:
        return "allow"
    if marked or closure:
        return "inconsistent"
    # Checked last, and only against a test that declared nothing: a half
    # declaration is still an error worth naming, and `constructs_engine` must
    # not be usable to wave one through.
    if constructs_engine:
        return "allow"
    return "prohibit"


def _raise_prohibited(*args, **kwargs):
    raise DatabaseAccessProhibitedError(
        "this test built a SQLAlchemy Engine but does not carry "
        f"@pytest.mark.{NEEDS_DB_MARKER}. Either it reads PostgreSQL — then add "
        f"the marker *and* request the `{DATABASE_FIXTURE}` fixture, both, "
        "because tests/test_ci_guards.py asserts the two sets are equal — or it "
        "does not, and something on its path reaches the database by accident. "
        "specs/014-test-isolation.md §2.3 is one such accident: /ask reads the "
        "schema to build a cache key, long before the agent it stubbed."
    )


def _evict_cached_engine() -> None:
    """Drop every memoised Engine, disposing each pool rather than leaking it.

    Without this the prohibition is inert for most of a run. `get_engine` caches
    one Engine per target and `configured_database` is session-scoped, so the
    first marked test leaves a live Engine behind that outlives every test after
    it. An unmarked test then calls `get_engine()`, gets a **cache hit**,
    connects, and never reaches `create_engine` at all — so patching the factory
    without evicting the cache is a defence that works on the first test of the
    session and on none of the rest.

    The dispose is not tidiness. Each eviction otherwise abandons a pool of up to
    five connections, and `pool_size=5, max_overflow=5` against a default
    `max_connections=100` makes that a ceiling a long run can actually hit.

    **`dispose_all()`, not `get_engine().dispose()` (dynamic-database-
    switching).** The latter disposes only the *default* target's pool by
    construction — `get_engine()` with no argument — which is exactly right
    while there is one target and quietly leaks every other target's pool
    once there is more than one: `get_engine` now caches per target, and
    `api/db/engine.py::_EngineCache` exists specifically because
    `functools.lru_cache` gives no way to enumerate cached entries to dispose
    each. Reaching a cached engine to dispose it costs no connection and
    cannot itself trip the prohibition.
    """
    from api.db import engine as engine_module

    engine_module.get_engine.dispose_all()


@contextlib.contextmanager
def prohibited_engine_access():
    """Refuse Engine construction for the duration of the block.

    Factored out of the fixture so the mechanism is testable before the fixture
    is wired, and so a test can assert what the prohibition does without having
    to be an undeclared test itself.

    Uses a **private** `pytest.MonkeyPatch`, not the shared `monkeypatch`
    fixture. HANDOFF.md §6 records that `monkeypatch.undo()` in a test body
    reverts every fixture sharing that instance — it once restored
    `history.DEFAULT_PATH` and wrote a real 32 KB database to `C:\\data` on every
    run for an iteration. The prohibition is the one thing that must survive a
    test switching its isolation off.
    """
    _evict_cached_engine()
    try:
        with pytest.MonkeyPatch.context() as patcher:
            for module_path, attribute in PROHIBITED_TARGETS:
                patcher.setattr(f"{module_path}.{attribute}", _raise_prohibited)
            yield
    finally:
        # Runs after the patches are lifted. Looks vacuous — an unmarked test
        # cannot have built an Engine — and is the belt for the day a test body
        # undoes the prohibition anyway: whatever it cached must not escape into
        # the next test.
        _evict_cached_engine()


@pytest.fixture(autouse=True)
def prohibit_database_access(request):
    """Refuse an Engine to any test that did not declare a database.

    **Autouse since T5**, which is the point: a guard a test has to opt into
    protects only the tests that remembered it. It shipped unwired at T3 because
    every test then reached `configured_database` through `autouse=True` and none
    carried the marker, so switching it on before T4's partition existed would
    have put the whole suite in the red at once.

    `request.fixturenames` is the public closure and is trustworthy now that no
    autouse fixture reaches the database: a name appears in it only because the
    test or its module asked. That equivalence is not assumed — the census plugin
    reports `closure_walk_agrees_with_fixturenames`, which was `false` before T4
    and `true` after, and `tests/test_isolation.py` asserts it.

    This fixture requests only `request`, so being autouse adds nothing to any
    other test's closure and cannot itself widen what the guard measures.
    """
    decision = isolation_decision(
        marked=request.node.get_closest_marker(NEEDS_DB_MARKER) is not None,
        closure=DATABASE_FIXTURE in request.fixturenames,
        constructs_engine=(
            request.node.get_closest_marker(CONSTRUCTS_ENGINE_MARKER) is not None
        ),
    )

    if decision == "inconsistent":
        raise DatabaseAccessProhibitedError(
            f"{request.node.nodeid} declares a database dependency one way but "
            f"not the other. A test that reads PostgreSQL carries "
            f"@pytest.mark.{NEEDS_DB_MARKER} *and* requests `{DATABASE_FIXTURE}`; "
            "one without the other either runs unprobed against an ambient DSN, "
            "or is refused an Engine it legitimately asked for."
        )

    if decision == "allow":
        yield
        return

    with prohibited_engine_access():
        yield
