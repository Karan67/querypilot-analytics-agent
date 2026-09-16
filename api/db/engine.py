"""Read-only connection to the target database.

This module owns the SQLAlchemy Engine(s) the API uses to reach the
database(s) the agent analyses — one per registered target
(`api/targets.py`), keyed by name rather than one process-wide singleton.
Two rules govern it, both from specs/000-project.md section 4, and both now
apply *per target* rather than to one ambient connection:

1. The DSN configured here is always the ``querypilot_ro`` role. A privileged
   DSN must never be readable from this process. Seeding and role creation are
   done by the Postgres init hooks in ``db/init/``, not by the API.
2. Nothing here executes agent-generated SQL. From Iteration 1 onward, every
   generated statement goes through ``api/safety/validator.py`` first, and
   ``execute_sql()`` is the only caller permitted to run one.

**Why a parameter here and not a mutable "current target" global.** The
tempting shortcut — set an env var and call `get_engine.cache_clear()`, which
is exactly what `tests/conftest.py::configured_database` already does to
retarget the suite — is safe only because tests run one at a time. This API
serves concurrent requests (FastAPI runs a sync `def` route in a thread pool),
so two questions asked at the same moment could interleave around a shared
"current target" and one user's question would run against the other's
database. `target` is therefore a real parameter, validated against the
closed `DATABASE_TARGETS` registry before it ever reaches `create_engine` —
an allow-list check, never a raw string reaching a connection string or a SQL
identifier.

SQLAlchemy Core only - no ORM, no models. The agent works against the physical
schema, so an ORM layer would add a mapping to maintain and nothing else.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from api.targets import DATABASE_TARGETS, DEFAULT_TARGET

#: Environment variable holding Chinook's read-only DSN, e.g.
#: postgresql+psycopg://querypilot_ro:...@db:5432/chinook
#:
#: Kept as a public name for backward compatibility — `tests/conftest.py` and
#: `docker-compose.yml` both still refer to it directly — even though the
#: source of truth for *which* variable a given target reads is now
#: `DATABASE_TARGETS[target].dsn_env`.
DATABASE_URL_ENV = "QUERYPILOT_DATABASE_URL"


class UnknownDatabaseTargetError(ValueError):
    """Raised when a caller names a target outside `DATABASE_TARGETS`.

    A `ValueError` subclass, not a bare one, so code written against
    `except ValueError` before targets existed keeps working, while a caller
    that cares specifically about an unknown target can catch this instead.
    """

#: Seconds to wait for a connection to be established before giving up.
#:
#: Not optional, and not a tuning knob. A *refused* connection fails instantly,
#: but a host that swallows the packet without answering — a stopped Docker
#: Desktop, a paused container, a firewall dropping rather than rejecting — makes
#: libpq wait indefinitely by default. Without this, `/health` would hang instead
#: of returning the 503 it exists to return, and the endpoint built to report
#: degradation would report nothing at all.
#:
#: Observed: a three-minute hang in the test suite from exactly this cause before
#: the equivalent timeout was added there.
#:
#: Distinct from Gate 3's `statement_timeout`, which bounds query execution. This
#: bounds getting connected in the first place. libpq clamps values below 2 to 2.
CONNECT_TIMEOUT_SECONDS = 5


def get_database_url(target: str = DEFAULT_TARGET) -> str:
    """Return the configured read-only DSN for one registered target.

    Raises:
        UnknownDatabaseTargetError: `target` is not in `DATABASE_TARGETS`.
        RuntimeError: the target is known but its DSN variable is unset or
            empty. Failing loudly is deliberate - a silent fallback to a
            default DSN is how a service ends up connected to something
            other than the read-only role.
    """
    if target not in DATABASE_TARGETS:
        raise UnknownDatabaseTargetError(
            f"Unknown database target {target!r}. Known targets: "
            f"{sorted(DATABASE_TARGETS)}."
        )
    env_name = DATABASE_TARGETS[target].dsn_env
    url = os.environ.get(env_name, "").strip()
    if not url:
        raise RuntimeError(
            f"{env_name} is not set for database target {target!r}. Copy "
            f".env.example to .env and run via docker compose, or export "
            f"the read-only DSN before starting uvicorn."
        )
    return url


class _EngineCache:
    """One Engine per target, built lazily and kept for the process's life.

    **Not `functools.lru_cache`.** `tests/isolation.py::_evict_cached_engine`
    has to dispose every cached pool before a session moves on — its own
    docstring measures why an un-disposed pool matters (up to five
    connections each, against a `max_connections=100` a long run can actually
    reach). Before dynamic-database-switching, `get_engine()` took no
    argument, so "the cached engine" was unambiguous and `lru_cache`'s own
    `.cache_clear()` was enough. Now there can be one cached engine per
    target, and `lru_cache` exposes no way to enumerate its own entries to
    dispose each — so this class keeps a plain, enumerable dict instead,
    behind just enough of `lru_cache`'s interface (`cache_clear`,
    `cache_info().currsize`) that every existing caller of `get_engine`
    needs no change.
    """

    def __init__(self) -> None:
        self._engines: dict[str, Engine] = {}

    def __call__(self, target: str = DEFAULT_TARGET) -> Engine:
        """Return the read-only Engine for one target, creating it on first use.

        One pool per target — a question against `pagila` must never borrow
        a connection `chinook` opened, and vice versa. Lazily constructed so
        that importing this module never requires a reachable database -
        unit tests import the package without one, and a target nobody has
        asked for yet costs nothing. An invalid target raises inside
        `get_database_url` before `create_engine` is reached and never
        occupies a cache slot, so this can never grow past one entry per
        registered database.
        """
        if target not in self._engines:
            self._engines[target] = create_engine(
                get_database_url(target),
                pool_pre_ping=True,  # Postgres restarts should not poison the pool.
                pool_size=5,
                max_overflow=5,
                future=True,
                connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
            )
        return self._engines[target]

    def cache_clear(self) -> None:
        """Drop every cached engine **without disposing its pool.**

        Matches `functools.lru_cache.cache_clear()`'s own contract exactly —
        it does not dispose either — which is what every existing caller
        already assumes. Disposal is `dispose_all()`, a new and distinct
        operation, because conflating the two here would silently change
        what `tests/conftest.py`'s pre-existing `cache_clear()` calls do.
        """
        self._engines.clear()

    def cache_info(self) -> SimpleNamespace:
        """Just enough of `functools.lru_cache`'s `CacheInfo` for
        `tests/isolation.py::_evict_cached_engine`'s `.currsize` check."""
        return SimpleNamespace(currsize=len(self._engines))

    def dispose_all(self) -> None:
        """Dispose every cached target's pool, then drop them all.

        The correct replacement for `_evict_cached_engine`'s old
        `get_engine().dispose(); cache_clear()` -- that disposed only the
        *default* target's pool by construction (`get_engine()` with no
        argument), which would have quietly leaked `pagila`'s connections on
        every eviction once a test exercised it.
        """
        for engine in self._engines.values():
            engine.dispose()
        self._engines.clear()


get_engine = _EngineCache()
