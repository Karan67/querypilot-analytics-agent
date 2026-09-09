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
import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from api.agent.orchestrator import answer
from api.agent.tools import execute_sql
from api.llm.factory import get_provider
from api.store import history
from api.store.history import AskRecord
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

class _TimedProvider:
    """Wraps a provider to time `complete()` and nothing else.

    **This is why the endpoint builds a provider rather than letting `answer()`
    pick one.** `010-hardening.md` §2.2 measured the provider at 94.2% of wall
    clock, so AC2 requires the two durations be recorded apart -- and the only
    place that boundary is visible is around `complete()`.

    Attribute access delegates, which is not incidental: `usage_for_call()`
    reads `last_usage` off the provider with `getattr`, and `/quota` reads
    `last_rate_limit`. A wrapper that did not proxy would silently produce
    unmeasured token counts -- exactly the failure D-1 exists to prevent, and
    invisible in every test that does not check `measured`.

    It does **not** pace, delay, retry or otherwise alter the call (`009` AC5).
    """

    def __init__(self, inner):
        self._inner = inner
        self.elapsed_ms = 0
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        started = time.perf_counter()
        try:
            return self._inner.complete(system, user)
        finally:
            self.elapsed_ms += int((time.perf_counter() - started) * 1000)
            self.calls += 1

    def __getattr__(self, name):
        return getattr(self._inner, name)


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

    # Resolved D-2's second half. A history write that fails is swallowed so the
    # user still gets their answer -- which would leave the system silently
    # amnesiac if nothing ever said so. This is where it says so.
    #
    # It is deliberately **not** a 503: history is observability, and reporting
    # the service as down because logging broke would be the cascade D-2 exists
    # to prevent, moved into the health check.
    store_error = history.degraded()
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok" if not store_error else "degraded_history",
            "database": {
                "connected": True,
                "user": db_user,
                "database": db_name,
                "public_tables": public_tables,
            },
            "history": {"writable": not store_error, "error": store_error},
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
    provider = _TimedProvider(get_provider())
    started = time.perf_counter()
    result = answer(request.question, provider=provider)
    total_ms = int((time.perf_counter() - started) * 1000)

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
        # AC1. `AgentResult.usage` existed from Iteration 4 and this boundary
        # threw it away, which is how a 20,370-token gap in the spend ledger had
        # to be reconstructed by hand from three instrumented probes. `measured`
        # travels with it, per D-1: a locally counted number and a billed number
        # are different quantities, and a figure that does not say which it is
        # is not a measurement.
        "usage": {
            "total_tokens": result.usage.total_tokens,
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
            "calls": result.usage.calls,
            "measured": result.usage.measured,
        },
        "total_ms": total_ms,
        "provider_ms": provider.elapsed_ms,
        # T4 makes this meaningful; the field exists now so the contract does
        # not change under the page when the cache lands. AC8 requires a cached
        # answer to be visibly cached.
        "cache_hit": False,
    }

    payload["id"] = _record_history(request, result, payload, provider, total_ms)

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


def _record_history(request, result, payload, provider, total_ms) -> str | None:
    """Persist one answered question, and never let that failure reach the user.

    Resolved D-2: *the primary contract of `/ask` is to return an answer.* The
    store swallows its own exceptions, so this returns `None` rather than
    raising, and `/health` is where a degraded store becomes visible.

    The returned id is the **answer id** Iteration 8's feedback attaches to --
    an answer, never a question string, because the agent may answer the same
    question differently next time.
    """
    snapshot = getattr(provider, "last_rate_limit", None)
    tokens_left = getattr(getattr(snapshot, "tokens", None), "remaining", None)
    requests_left = getattr(getattr(snapshot, "requests", None), "remaining", None)

    return history.record_ask(
        AskRecord(
            # The user's question, not the agent's echo of it. They are the same
            # string today, and a history of what was *asked* should not depend
            # on that staying true -- the record's job is to say what the user
            # sent, whatever the agent did with it afterwards.
            question=request.question,
            ok=result.ok,
            total_ms=total_ms,
            provider_ms=provider.elapsed_ms,
            sql=result.sql,
            category=result.category,
            shape=payload["shape"],
            row_count=len(payload["rows"]),
            attempts_used=result.attempts_used,
            total_tokens=result.usage.total_tokens,
            prompt_tokens=result.usage.prompt_tokens,
            usage_measured=result.usage.measured,
            provider_calls=result.usage.calls,
            cache_hit=payload["cache_hit"],
            tpm_remaining=tokens_left,
            rpd_remaining=requests_left,
            model=getattr(provider, "model", ""),
            steps=tuple(payload["trace"]),
        )
    )
