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
