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


def isolation_decision(*, marked: bool, closure: bool) -> str:
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
    """Drop the memoised Engine, disposing its pool rather than leaking it.

    Without this the prohibition is inert for most of a run. `get_engine` is
    `lru_cache(maxsize=1)` and `configured_database` is session-scoped, so the
    first marked test leaves a live Engine behind that outlives every test after
    it. An unmarked test then calls `get_engine()`, gets a **cache hit**,
    connects, and never reaches `create_engine` at all — so patching the factory
    without evicting the cache is a defence that works on the first test of the
    session and on none of the rest.

    The dispose is not tidiness. Each eviction otherwise abandons a pool of up to
    five connections, and `pool_size=5, max_overflow=5` against a default
    `max_connections=100` makes that a ceiling a long run can actually hit. A
    warm `get_engine()` is a pure cache hit, so reaching the object to dispose it
    costs no connection and cannot itself trip the prohibition.
    """
    from api.db import engine as engine_module

    if engine_module.get_engine.cache_info().currsize:
        engine_module.get_engine().dispose()
    engine_module.get_engine.cache_clear()


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


@pytest.fixture
def prohibit_database_access(request):
    """Refuse an Engine to any test that did not declare a database.

    **Not `autouse` yet.** Every test currently reaches `configured_database`
    through `tests/conftest.py`'s `autouse=True`, and none carries the marker, so
    switching this on before the partition exists would make the whole suite
    `inconsistent` at once. T5 removes the autouse and turns this on in the same
    step.

    `request.fixturenames` is the public closure and becomes trustworthy at that
    moment: with no autouse fixture reaching the database, a name appears in it
    only because the test or its module asked. The census plugin reports
    `closure_walk_agrees_with_fixturenames` so that equivalence is a checked fact
    rather than an assumption — it is `false` today and must be `true` after T4.
    """
    decision = isolation_decision(
        marked=request.node.get_closest_marker(NEEDS_DB_MARKER) is not None,
        closure=DATABASE_FIXTURE in request.fixturenames,
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
