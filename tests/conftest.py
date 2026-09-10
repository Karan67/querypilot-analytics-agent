"""Shared pytest fixtures.

**The database location is known only to this module.** Decision D-1 in
`specs/001-schema-tool-plan.md`: the DSN comes from ``TEST_DATABASE_URL`` and
nothing is hardcoded in test setup. A test that embeds ``localhost:5432``
directly is to be rejected in review — Iteration 8 CI must be able to point the
whole suite at a different server by setting one environment variable.

The suite runs against a live Chinook container rather than a mocked
``Inspector``. Mocking introspection would assert that the mock matches the code
and prove nothing about Postgres, which is the only thing the schema tool
actually talks to.
"""

from __future__ import annotations

import os
import pathlib

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

#: Local-development fallback only. Matches the committed `.env.example`, which
#: holds no real secret. CI overrides it via TEST_DATABASE_URL.
DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg://querypilot_ro:readonlylocaldev@localhost:5432/chinook"
)

#: Seconds the reachability probe waits before giving up and skipping.
#:
#: Not optional. A refused connection fails immediately, but a host that accepts
#: the packet and never answers — Docker Desktop stopped mid-session, a firewall
#: dropping rather than rejecting, a paused container — makes libpq wait
#: indefinitely by default, and the suite hangs instead of skipping. Observed
#: exactly that: a three-minute hang when the Docker daemon went away.
#:
#: libpq clamps anything below 2 to 2 seconds.
PROBE_CONNECT_TIMEOUT_SECONDS = 3


def _database_url() -> str:
    """Resolve the test DSN. Deliberately not named ``test_*`` — pytest would
    collect it as a test case."""
    return os.environ.get("TEST_DATABASE_URL") or DEFAULT_TEST_DATABASE_URL


@pytest.fixture(scope="session", autouse=True)
def configured_database() -> str:
    """Point the application's engine at the test database, or skip the suite.

    ``get_schema()`` reads ``QUERYPILOT_DATABASE_URL`` through ``get_engine()``,
    which is ``lru_cache``d. Setting the variable and clearing that cache lets
    the tool under test run completely unmodified — no test-only parameter is
    threaded through the production call path, which keeps AC14 (no parameters)
    honest rather than merely asserted.

    If nothing is listening, every test skips with a reason that says what to do
    about it. An unreachable database is an environment problem, and reporting
    it as a wall of failures would bury the one line that matters.
    """
    from api.db import engine as engine_module

    url = _database_url()

    try:
        probe = create_engine(
            url,
            connect_args={"connect_timeout": PROBE_CONNECT_TIMEOUT_SECONDS},
        )
        with probe.connect() as conn:
            conn.execute(text("SELECT 1"))
        probe.dispose()
    except SQLAlchemyError as exc:
        pytest.skip(
            f"No database at the configured DSN ({exc.__class__.__name__}). "
            f"Start it with 'docker compose up -d', or set TEST_DATABASE_URL. "
            f"Underlying error: {exc}"
        )

    os.environ[engine_module.DATABASE_URL_ENV] = url
    engine_module.get_engine.cache_clear()
    yield url
    engine_module.get_engine.cache_clear()


@pytest.fixture(scope="session")
def schema(configured_database: str):
    """The introspected schema. Session-scoped: it is immutable, and one
    catalog read is enough for the whole suite."""
    from api.db.introspection import get_schema

    return get_schema()


@pytest.fixture(scope="session")
def relations(schema) -> dict:
    """Relations keyed by name, for readable lookups in assertions."""
    return {table.name: table for table in schema.tables}


@pytest.fixture(scope="session")
def track_columns(relations) -> dict:
    """Columns of `track` keyed by name. `track` is the workhorse fixture: it
    has a primary key, nullable and non-nullable columns, varchar with a
    length, numeric with a precision, and three foreign keys."""
    return {column.name: column for column in relations["track"].columns}


def _load_dotenv() -> None:
    """Make `.env` visible to host tests.

    Production never does this -- the container gets its environment from
    docker compose, which is the 12-factor arrangement. But host tests have no
    such injector, and the live LLM tests need GROQ_API_KEY. Test-only, and
    existing environment always wins so CI can override without touching files.
    """
    env_file = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


@pytest.fixture(autouse=True)
def isolated_spend_ledger(tmp_path, monkeypatch):
    """Never let a test write the real daily-spend ledger.

    **Autouse, and structural on purpose.** The B-5 tests isolate the ledger
    explicitly, but two tests elsewhere drive `main()` end to end and reach the
    ledger write without knowing it exists -- and they wrote 6,800 tokens and 40
    requests of fake spend into the real file on the first full run after B-5
    landed. Gitignored, so it would never have shown up in review; and read at
    the next real run's pre-flight, where it would have silently moved the
    daily guard by the size of a small benchmark.

    This is the `EVALS_PATH` trap of T5 in a second place, and per-test
    discipline is what failed there too. A test that has to remember to isolate
    shared state is a test that will one day forget, so the isolation is applied
    to every test whether it asks or not.
    """
    from evals import ledger

    monkeypatch.setattr(ledger, "DEFAULT_PATH", tmp_path / "spend.json")


@pytest.fixture(autouse=True)
def isolated_history_store(tmp_path, monkeypatch):
    """Never let a test write the real history database.

    **The third instance of the same trap, isolated before it can bite rather
    than after.** T5's `EVALS_PATH` put a fabricated entry in the real
    `EVALS.md`; B-5's ledger took 6,800 tokens of fake spend from two tests that
    drove `main()` end to end and had no idea a ledger existed. Both were
    gitignored, so neither would have shown up in review.

    `POST /ask` now writes history, and the endpoint tests drive it without
    caring that a store exists -- which is precisely the shape of both earlier
    failures. Per-test discipline is what failed twice, so this is autouse and
    applies whether a test asks for it or not.
    """
    from api.store import history

    monkeypatch.setattr(history, "DEFAULT_PATH", tmp_path / "history.db")
    monkeypatch.setattr(history, "_degraded", "")


@pytest.fixture(autouse=True)
def isolated_answer_cache():
    """Never let one test's answer be served to another.

    **The fourth instance of the same trap, and the first where the shared state
    is in memory rather than on disk.** `EVALS.md`, the spend ledger and the
    history database were all files; this one is a module-level dict, which is
    worse in one specific way -- a leaked entry does not sit there waiting to be
    noticed, it makes a *later* test's provider call silently not happen.

    Consider the failure it prevents: a test asks a question and asserts the
    provider was called once. It passes alone and fails in a full run, or worse,
    passes in both because a *different* test was the one that paid. The suite
    would then be asserting the behaviour of whichever test happened to run
    first, which is not a property anybody chose.

    Cleared before and after: before, so a test never inherits; after, so a
    failing test does not leave a primed cache behind for the next file.
    """
    from api.http import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def isolated_quota_snapshot():
    """The fifth. Same rule, applied without waiting to be bitten.

    `api/http/quota.py` keeps the last rate-limit reading in a module global,
    because T4 made the provider per-request and there is nowhere else for it to
    live. A reading left behind by one test would make another test's `/quota`
    report limits nobody in that test observed -- and the warning it drives is
    exactly the kind of thing that looks right until it is wrong.
    """
    from api.http import quota

    quota.clear()
    yield
    quota.clear()


@pytest.fixture(autouse=True)
def isolated_schema_cache():
    """The sixth, and the one with a way to hold something that is not real.

    The ledger, the history store, the answer cache and the quota snapshot can
    all be *polluted* by a test. This one can be **falsified**: a test that
    monkeypatches `get_schema` to return a hand-built `Schema` leaves that fake
    cached behind, and the next test builds its prompt from a database that
    does not exist.

    It costs the suite a real introspection per test that needs one, which is
    what the suite already paid before T6 existed. Correctness is not the thing
    to spend for speed here.
    """
    from api.db import schema_cache

    schema_cache.clear()
    yield
    schema_cache.clear()
