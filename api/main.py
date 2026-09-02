"""QueryPilot API.

Iteration 0 scope: the process starts, and it can prove it reaches the target
database over the read-only role. Nothing else lives here yet - the tools
arrive in Iteration 1, the agent loop in Iteration 4.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from api.db.engine import get_engine

logger = logging.getLogger("querypilot")

app = FastAPI(
    title="QueryPilot",
    description="Natural language analytics agent over a read-only database.",
    version="0.0.0",
)


@app.get("/health", tags=["ops"])
def health() -> JSONResponse:
    """Liveness plus target-database readiness.

    Iteration 0 is done when this endpoint reports ``ok``, because that proves
    the whole chain: the API started, the read-only role exists, it can
    authenticate, and the sample dataset was loaded.

    Returns 200 with::

        {
          "status": "ok",
          "database": {
            "connected": true,
            "user": "querypilot_ro",
            "database": "chinook",
            "public_tables": 11
          }
        }

    and 503 with ``{"status": "degraded", "database": {"connected": false,
    "error": "..."}}`` if the database is unreachable. A health check that
    reports healthy while its dependency is down is worse than no health check,
    so the database round-trip is not optional here.
    """
    try:
        with get_engine().connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT current_user           AS db_user,
                           current_database()     AS db_name,
                           (SELECT count(*)
                              FROM information_schema.tables
                             WHERE table_schema = 'public') AS public_tables
                    """
                )
            ).one()
    except (SQLAlchemyError, RuntimeError) as exc:
        # RuntimeError covers a missing QUERYPILOT_DATABASE_URL; SQLAlchemyError
        # covers auth failures, a database that is still starting, and network
        # problems. All of them mean the same thing to a caller: not ready.
        logger.warning("health check failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "database": {"connected": False, "error": str(exc)},
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "database": {
                "connected": True,
                "user": row.db_user,
                "database": row.db_name,
                "public_tables": row.public_tables,
            },
        },
    )
