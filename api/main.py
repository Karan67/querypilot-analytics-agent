"""QueryPilot API.

**Iteration 6 gave this module its second endpoint, six iterations late.** Its
original docstring said the tools arrived in Iteration 1 and the agent loop in
Iteration 4; neither ever landed here, and until `POST /ask` the agent was
reachable only from the eval runner and the test suite. `009-frontend.md` §2.1
records the measurement.

This module stays a **thin surface**. It parses a request, calls `answer()`, and
adapts the result for JSON — the shape classifier, the encoder and the error
table each live in their own module under `api/http/`, following the rule
`api/agent/tools.py` states about itself.

**`answer()` does not learn about HTTP.** The endpoint adapts it; the agent does
not grow transport concerns, for the same reason the LLM provider interface is
one method wide.
"""

from __future__ import annotations

import logging
import pathlib

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from api.agent.orchestrator import answer
from api.agent.tools import execute_sql
from api.http.errors import STATUS_ANSWERED, failure_for
from api.http.serialization import encode_rows
from api.http.shapes import SHAPE_CHARTABLE, chart_series, classify

#: The liveness probe, as a query rather than as engine calls.
#:
#: **Iteration 6 T5 found this endpoint bypassing the safety layer**, and it had
#: done so since Iteration 0. It opened its own connection through `get_engine()`
#: and ran `conn.execute(text(...))`, which is a code path executing SQL without
#: passing Gate 2 — exactly what charter §4 forbids, with the one recorded
#: exemption being `tests/test_validator_gates.py` and nothing else.
#:
#: The argument for leaving it was that the statement is a compile-time constant
#: containing no user input, so the risk was nil. The argument that won is that
#: an absolute rule with an undocumented exception is not an absolute rule, and
#: routing a plain `SELECT` through the standard gate costs nothing. It passes
#: Gate 2 unchanged, and `api/main.py` now reaches the database through exactly
#: one function, which is a property a structural test can assert about the
#: whole module rather than one handler.
_HEALTH_QUERY = """
    SELECT current_user           AS db_user,
           current_database()     AS db_name,
           (SELECT count(*)
              FROM information_schema.tables
             WHERE table_schema = 'public') AS public_tables
"""

#: Where the page lives.
#:
#: Resolved from `__file__`, never from the working directory. The Docker build
#: context is `./api` and `COPY . ./api/` lands this package at `/app/api/`, so
#: a relative path would resolve differently in the container than on a host
#: running pytest from the repository root -- and would fail in whichever one
#: nobody tried.
#:
#: The files sit under `api/` rather than the repository's `frontend/` for the
#: same reason: anything outside the build context is simply not in the image.
_WEB_DIR = pathlib.Path(__file__).resolve().parent / "web"

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
            "public_tables": 12
          }
        }

    and 503 with ``{"status": "degraded", "database": {"connected": false,
    "error": "..."}}`` if the database is unreachable. A health check that
    reports healthy while its dependency is down is worse than no health check,
    so the database round-trip is not optional here.

    Goes through `execute_sql()` like everything else (see `_HEALTH_QUERY`).
    That also simplifies the failure handling: the exception cases this used to
    catch by hand — a missing DSN, an auth failure, a database still starting —
    are what `ExecutionResult.ok` already reports.
    """
    result = execute_sql(_HEALTH_QUERY)

    if not result.ok:
        logger.warning("health check failed: %s (%s)", result.error, result.category)
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "database": {"connected": False, "error": result.error},
            },
        )

    db_user, db_name, public_tables = result.rows[0]
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "database": {
                "connected": True,
                "user": db_user,
                "database": db_name,
                "public_tables": public_tables,
            },
        },
    )


class AskRequest(BaseModel):
    """One natural-language question.

    The question is passed to the agent **unsanitised** (`007` AC20). Stripping
    or escaping it here would corrupt legitimate questions -- apostrophes and
    the word "select" both appear in ordinary English -- and would be the wrong
    defence anyway: nothing the model writes reaches the database without Gate 2
    validating it as a read-only statement.
    """

    question: str = Field(min_length=1, max_length=1000)


@app.post("/ask", tags=["agent"])
def ask(request: AskRequest) -> JSONResponse:
    """Answer one question, and show the working.

    Synchronous, per resolved Q-D: measured at 1.20-2.51s over a single provider
    call, so there is nothing a job queue or a stream would buy. The loop emits
    its SQL in one shot rather than incrementally.

    **A failed question is not an HTTP error** (resolved D-2). The request was
    received, parsed and processed; the answer being negative is a result. Those
    return 200 with `ok: false` and a category the page can render. `503` is
    reserved for the service failing rather than the question failing -- an
    unreachable database or provider, or an exhausted quota, where reporting
    "no results" would be a lie about the data.

    The response always carries `sql` when one was produced, including on
    failure: AC12 says a demo audience learns more from a legible failure than
    from a spinner that stops.
    """
    result = answer(request.question)

    columns = list(result.result.columns) if result.result else []
    rows = encode_rows(result.result.rows) if result.result else []

    shape = classify(columns, rows)

    # Which column the bars are drawn from, decided once, server-side. The
    # client could re-derive it -- and would eventually derive it differently,
    # which is how a chart silently plots the label column. `chart_series`
    # raises rather than guessing for any other shape, so this is the only
    # place the transposition can go wrong (T7, D-3).
    series = None
    if shape == SHAPE_CHARTABLE:
        label_index, measure_index = chart_series(columns, rows)
        series = {"label": label_index, "measure": measure_index}

    payload = {
        "ok": result.ok,
        "question": result.question,
        "sql": result.sql,
        "columns": columns,
        "rows": rows,
        "shape": shape,
        "series": series,
        "attempts_used": result.attempts_used,
        # Resolved Q-E: returned always, rendered collapsed. The retry loop is
        # the evidence that this is an agent rather than a wrapper around one
        # prompt, and it costs a few hundred bytes to include.
        #
        # `attempt` and `error` are here deliberately. Charter §1's diagram is
        # *read the error, revise*, and a trace that omits the error the agent
        # read shows the retry without showing what caused it. This is the audit
        # channel, not the message channel: the user-facing `error` above stays
        # free of SQL and internals, and this sits behind a collapsed section.
        "trace": [
            {
                "attempt": step.attempt,
                "action": step.action,
                "ok": step.ok,
                "category": step.category,
                "error": step.error,
                "sql": step.sql,
            }
            for step in result.steps
        ],
        "category": result.category,
        "error": "",
        "retryable": False,
    }

    if result.ok:
        return JSONResponse(status_code=STATUS_ANSWERED, content=payload)

    # `failure_for` raises on an unmapped category rather than inventing a
    # generic message. The completeness test in tests/test_error_mapping.py is
    # what keeps that from happening in front of a user.
    failure = failure_for(result.category)
    payload["error"] = failure.message
    payload["retryable"] = failure.retryable
    return JSONResponse(status_code=failure.status, content=payload)


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    """The page (AC7): a question can be asked and answered with no terminal.

    Read at request time rather than cached at import, so editing the page
    during development does not need a restart. At a few kilobytes the read is
    not worth optimising, and a stale demo is a worse failure than a slow one.
    """
    return HTMLResponse(_WEB_DIR.joinpath("index.html").read_text(encoding="utf-8"))


#: Mounted last: a mount at "/" would shadow every route declared after it.
app.mount("/static", StaticFiles(directory=_WEB_DIR), name="static")
