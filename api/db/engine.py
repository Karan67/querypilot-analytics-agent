"""Read-only connection to the target database.

This module owns the single SQLAlchemy Engine the API uses to reach the
database the agent analyses. Two rules govern it, both from
specs/000-project.md section 4:

1. The DSN configured here is always the ``querypilot_ro`` role. A privileged
   DSN must never be readable from this process. Seeding and role creation are
   done by the Postgres init hooks in ``db/init/``, not by the API.
2. Nothing here executes agent-generated SQL. From Iteration 1 onward, every
   generated statement goes through ``api/safety/validator.py`` first, and
   ``execute_sql()`` is the only caller permitted to run one.

SQLAlchemy Core only - no ORM, no models. The agent works against the physical
schema, so an ORM layer would add a mapping to maintain and nothing else.
"""

from __future__ import annotations

import os
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

#: Environment variable holding the read-only DSN, e.g.
#: postgresql+psycopg://querypilot_ro:...@db:5432/chinook
DATABASE_URL_ENV = "QUERYPILOT_DATABASE_URL"

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


def get_database_url() -> str:
    """Return the configured read-only DSN.

    Raises:
        RuntimeError: if the variable is unset or empty. Failing loudly is
            deliberate - a silent fallback to a default DSN is how a service
            ends up connected to something other than the read-only role.
    """
    url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not url:
        raise RuntimeError(
            f"{DATABASE_URL_ENV} is not set. Copy .env.example to .env and run "
            f"via docker compose, or export the read-only DSN before starting "
            f"uvicorn."
        )
    return url


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the process-wide read-only Engine, creating it on first use.

    Lazily constructed so that importing this module never requires a
    reachable database - unit tests in later iterations import the package
    without one.
    """
    return create_engine(
        get_database_url(),
        pool_pre_ping=True,  # Postgres restarts should not poison the pool.
        pool_size=5,
        max_overflow=5,
        future=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )
