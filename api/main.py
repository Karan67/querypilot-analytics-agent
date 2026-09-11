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

import dataclasses
import logging
import pathlib
import time
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from api.agent.fingerprints import DeployedPrompt, deployed_fingerprints
from api.agent.orchestrator import answer
from api.agent.prompts import ADOPTED_RENDERING
from api.agent.tools import execute_sql
from api.llm.base import LLMError, TokenUsage
from api.llm.factory import get_provider
from api.store import history
from api.store.history import AskRecord
from api.http import cache, quota
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

#: The deployed prompt configuration, in one place.
#:
#: Passed to `answer()` *and* folded into the cache's prompt fingerprint, which
#: is the whole reason it is a constant rather than two defaults that happen to
#: agree. Flipping `answer()`'s own default without this would leave the cache
#: keyed on a prompt the API does not send -- a mismatch with no symptom, since
#: both sides would keep working and only the hits would be wrong.
#:
#: The glossary ships **on** (`008` resolved Q-D); `ADOPTED_RENDERING` is
#: `compact`, which D-2 selected on a dev-split A/B at Iteration 6 T7.
_DEPLOYED_GLOSSARY = True
_DEPLOYED_RENDERING = ADOPTED_RENDERING


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
    started = time.perf_counter()
    answered = _answer_or_replay(request.question)
    total_ms = int((time.perf_counter() - started) * 1000)

    result = answered.result
    cache_hit = answered.cache_hit
    provider = answered.provider

    # AC10/AC11. The provider is built per request and a cache hit builds none,
    # so what it learned about the limits has to be handed somewhere that
    # outlives the request or it is lost with it.
    #
    # The `is not None` check here is a readability guard, **not the protection**
    # -- `observe` ignores a `None` snapshot, and removing this line changes no
    # behaviour and fails no test. The load-bearing rule lives in `observe` for
    # a reason: a provider that simply reported nothing this time reaches it too,
    # and that path has no call-site check to lean on.
    if provider is not None:
        quota.observe(getattr(provider, "last_rate_limit", None))

    # Resolved at T4: `usage` reports what **this request** spent, so a hit
    # reports zero. The alternative -- replaying the original answer's figures --
    # makes the history column sum to more than was ever billed unless every
    # future reader remembers to filter on `cache_hit = 0`, and a total that
    # silently overstates spend is the same class of defect as the one that
    # understated it and cost a hand reconstruction on 2026-09-09.
    #
    # `measured` is False on that zero, following `TokenUsage`'s own convention:
    # a zero-call usage is the identity and carries no measurement claim.
    usage = TokenUsage() if cache_hit else result.usage

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
            "total_tokens": usage.total_tokens,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "calls": usage.calls,
            "measured": usage.measured,
        },
        "total_ms": total_ms,
        # Zero on a hit, because no provider was built and none was called.
        "provider_ms": provider.elapsed_ms if provider is not None else 0,
        # AC8: a cached answer must be *visibly* cached. Presenting an answer
        # computed some time ago as freshly computed is the analytics equivalent
        # of the accuracy claim `009` AC13 banned from the page.
        #
        # It means "this request did not call the provider", which also covers a
        # coalesced follower whose answer is perfectly fresh. That is the claim
        # both readers want: the page can say the answer may not have been
        # computed for you, and exactly one history row carries the cost of any
        # one provider call.
        "cache_hit": cache_hit,
    }

    payload["id"] = _record_history(request, answered, payload, total_ms, usage)

    if result.ok:
        return JSONResponse(status_code=STATUS_ANSWERED, content=payload)

    # `failure_for` raises on an unmapped category rather than inventing a
    # generic message. The completeness test in tests/test_error_mapping.py is
    # what keeps that from happening in front of a user.
    failure = failure_for(result.category)
    payload["error"] = failure.message
    payload["retryable"] = failure.retryable
    return JSONResponse(status_code=failure.status, content=payload)


@app.get("/quota", tags=["ops"])
def quota_endpoint() -> JSONResponse:
    """What the provider last said about its limits (AC10, AC11).

    **Reports observation, not policy.** It never calls the provider — asking
    the provider how much quota is left would spend quota — so it returns what
    the most recent real question learned, together with how long ago that was.
    Before any question has been asked it returns `known: false` and no numbers,
    because a default reading is indistinguishable from a healthy one.

    **Two buckets, not three.** B-1 measured a per-minute token limit and a
    per-day request limit in the response headers, and a **200,000 tokens-per-day
    limit that appears in no header at all** — it surfaces only in the body of a
    429. This endpoint cannot report what the provider never sends, and does not
    invent it.

    Always 200. A low bucket is not a service failure: the answer still arrives,
    it arrives more slowly, and the whole point of AC11 is to say so rather than
    to start refusing.

    **The API still does not pace** (resolved Q-C, `009` AC5). This endpoint
    tells the user what is happening; it does not sleep on their behalf. A
    request that takes ten seconds because the provider is slow is honest; one
    that takes ten seconds because we chose to wait is not.
    """
    return JSONResponse(status_code=200, content=quota.snapshot())


#: Rows the reader shows by default, and the most it will show at all.
#:
#: Capped because the endpoint takes the number from the query string, and an
#: uncapped `limit` is a way to ask one process to build an arbitrarily large
#: JSON document. 500 is far more than anyone reads and small enough to be free.
class FeedbackRequest(BaseModel):
    """One mark against one answer (AC13).

    `id` is the answer's id from the `/ask` payload, never the question text.
    §2.6's whole point is that the same question can be answered well once and
    badly later, so a mark keyed on the text would be attached to nothing in
    particular.

    `rating` is `Literal[-1, 1]` (resolved D-6) rather than an `int` with a
    validator, so an out-of-range value is refused by the schema and reported as
    a `422` naming the field. Two values, because two cannot be averaged into a
    number that looks like accuracy — the pressure AC14 exists to resist — and
    because §2.6 says there is no data yet to justify a finer instrument.

    `note` is capped at the same length as `AskRequest.question`. **This is the
    project's first unauthenticated write** (§4 of the spec names it), so the
    one thing a stranger can put in the database is bounded before it is
    stored.
    """

    id: str = Field(min_length=1, max_length=64)
    rating: Literal[-1, 1]
    note: str = Field(default="", max_length=1000)

    @field_validator("rating", mode="before")
    @classmethod
    def reject_booleans(cls, value):
        """A boolean is not a rating, and **accepting one silently biased the
        data toward "good"** (found by the parametrized test below, T6).

        `Literal[-1, 1]` alone lets JSON `true` through: Python's `True == 1`,
        so Pydantic coerces it to a valid rating. `false` becomes `0`, which is
        *not* in the literal and is refused. So a client written against a
        boolean API would have every "yes" recorded and every "no" rejected —
        producing a table of unanimous approval out of divided opinion, on
        precisely the signal §2.6 says this project has none of.

        `Field(strict=True)` cannot express this: Pydantic raises
        `Unable to apply constraint 'strict' to schema of type 'literal'`.
        """
        if isinstance(value, bool):
            raise ValueError(
                "rating must be the integer -1 or 1, not a boolean; "
                "true would be stored as 1 while false would be refused"
            )
        return value


@app.post("/feedback", tags=["agent"])
def feedback(request: FeedbackRequest) -> JSONResponse:
    """Store one mark. `201` on success, `404` on an unknown answer.

    **A `404` rather than a quiet accept** (resolved D-6). Storing a mark
    against an id nothing matches would leave a row that can never be
    interpreted — the orphan version of the empty-list-versus-503 mistake T7
    avoided, and worse, because it would inflate whatever eventually reads this
    table with records that mean nothing.

    A store failure is a `503` and not a `201`. `record_ask` swallows its
    failures because logging is incidental to answering a question; here the
    write *is* the request, so reporting success on a mark that was never
    stored would be a lie about the one thing the caller asked for.

    Nothing is aggregated on the way in or out (AC14): the response echoes the
    stored mark and no count, rate or score is computed anywhere.
    """
    try:
        feedback_id = history.record_feedback(
            ask_id=request.id, rating=request.rating, note=request.note
        )
    except history.UnknownAsk:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": (
                    f"No answer with id {request.id!r}. A mark attaches to an "
                    f"answer from POST /ask, not to a question."
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        logger.warning("feedback write failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": f"{type(exc).__name__}: {exc}"},
        )

    return JSONResponse(
        status_code=201,
        content={
            "ok": True,
            "error": "",
            "feedback_id": feedback_id,
            "id": request.id,
            "rating": request.rating,
        },
    )


HISTORY_DEFAULT_LIMIT = 50
HISTORY_MAX_LIMIT = 500


@app.get("/history/data", tags=["ops"])
def history_data(limit: int = HISTORY_DEFAULT_LIMIT) -> JSONResponse:
    """What was asked, what it cost, and what the agent did — as JSON (AC4).

    **A reader is allowed to fail where a writer is not**, and the asymmetry is
    deliberate. `record_ask` swallows everything, because a user who asked a
    question and got an answer should not be punished for our logging breaking.
    Someone opening the history page is asking a different question, and telling
    them the store is unreadable is the true answer to it — so this returns
    `503` rather than an empty list, which would read as *nothing was ever
    asked*.
    """
    limit = max(1, min(limit, HISTORY_MAX_LIMIT))

    try:
        rows = history.recent(limit=limit)
        traces = history.steps_for_ids([row["id"] for row in rows])
        marks = history.feedback_for_ids([row["id"] for row in rows])
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        logger.warning("history read failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "answers": [],
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "error": "",
            "answers": [
                _history_row(
                    row, traces.get(row["id"], ()), marks.get(row["id"], ())
                )
                for row in rows
            ],
        },
    )


def _history_row(row, steps, marks=()) -> dict:
    """One stored answer, shaped for the reader.

    `tokens` carries `measured` beside it rather than the number alone, per
    D-1's standing rule: a provider-billed figure and a locally counted one are
    different quantities, and a reader that shows them identically is inviting
    somebody to add them up.

    `feedback` is **the list of marks, not a summary of them** (AC14). No count,
    no rate, no latest-wins single value, no "3 of 4 were good". §2.6 measured
    zero bad answers in the whole record, and a proportion computed over that is
    the accuracy claim `009` AC13 kept off the page — the shape of this field is
    what makes computing one an obvious addition rather than a rendering
    detail.
    """
    return {
        "id": row["id"],
        "asked_at": row["asked_at"],
        "question": row["question"],
        "ok": bool(row["ok"]),
        "category": row["category"],
        "sql": row["sql"],
        "shape": row["shape"],
        "row_count": row["row_count"],
        "attempts_used": row["attempts_used"],
        "total_ms": row["total_ms"],
        "provider_ms": row["provider_ms"],
        "tokens": row["total_tokens"],
        "measured": bool(row["usage_measured"]),
        "provider_calls": row["provider_calls"],
        "cache_hit": bool(row["cache_hit"]),
        "model": row["model"],
        "schema_fp": row["schema_fp"],
        "prompt_fp": row["prompt_fp"],
        "trace": [
            {
                "attempt": step["attempt"],
                "action": step["action"],
                "ok": bool(step["ok"]),
                "category": step["category"],
                "error": step["error"],
                "sql": step["sql"],
            }
            for step in steps
        ],
        "feedback": [
            {
                "rating": mark["rating"],
                "note": mark["note"],
                "created_at": mark["created_at"],
            }
            for mark in marks
        ],
    }


@app.get("/history", include_in_schema=False)
def history_page() -> HTMLResponse:
    """The reader (AC4). Read at request time, like the answer page."""
    return HTMLResponse(_WEB_DIR.joinpath("history.html").read_text(encoding="utf-8"))


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


def _is_cacheable(result) -> bool:
    """Only a successful answer is worth keeping.

    **This follows from D-3 rather than being a preference.** The cache has no
    expiry, so a cached failure would be served for the life of the process: a
    question asked during a thirty-second rate limit would become permanently
    unanswerable, with no way for the user to retry. Re-running a failure costs
    exactly what the first attempt cost, which is the right price for something
    that might now succeed.
    """
    return bool(result.ok)


@dataclasses.dataclass(frozen=True)
class _Answered:
    """One answer plus how it was obtained.

    A record rather than a five-tuple because every field has a reader that
    would otherwise be indexing by position: the payload wants `cache_hit`, the
    timing wants `provider`, and the history row wants both fingerprints so it
    can say which schema and which prompt produced this answer.
    """

    result: object
    cache_hit: bool
    provider: object | None
    schema_fp: str = ""
    prompt_fp: str = ""


def _deployed_fingerprints() -> DeployedPrompt | None:
    """`(schema_fp, prompt_fp)` for the configuration this API actually sends.

    Delegates the schema read to `api/agent/`, which is where reading a schema
    to build a prompt already happens. **`api/main.py` is asserted to reach the
    database only through the agent**, and importing `get_schema` here would
    have broken that -- a structural test walks this module's imports and fails
    on anything from `api.db`. Widening the test to admit an introspection call
    would have been the wrong repair: the first version of this task did exactly
    that, and it is how a rule acquires its first undocumented exception.

    `None` when the schema cannot be read, which deliberately **bypasses the
    cache** rather than inventing a key without one. `answer()` then produces
    its own `connection_error`, so an unreachable database fails exactly the way
    it did before the cache existed.

    On a miss this is a second introspection, because `answer()` reads the
    schema again to build the prompt. **Measured in the container rather than
    estimated**: `get_schema()` is a 118ms median over five warm calls, and a
    warm miss went from a 169ms non-provider gap at T3 to 272ms here. A hit
    costs 113ms, essentially all of it this call.

    T6 (AC9) caches `get_schema()` at its source, which collapses both reads to
    one lookup and takes a hit to near zero. Paying it now is the honest
    ordering: a key that cannot notice a schema change is not a correctness
    guarantee, and 118ms against a 459ms provider call is the cheaper half.
    """
    return deployed_fingerprints(
        rendering=_DEPLOYED_RENDERING, glossary=_DEPLOYED_GLOSSARY
    )


def _answer_or_replay(question: str) -> _Answered:
    """Answer the question, or hand back an answer already paid for.

    **The provider is `None` on a hit** -- not merely unused, never built --
    which is what makes a cached answer survive a provider outage or a missing
    key, and what makes `provider_ms` honestly zero rather than a timer that was
    started and never used.
    """
    timed: list[_TimedProvider] = []

    def compute(schema=None):
        try:
            provider = _TimedProvider(get_provider())
        except LLMError:
            # Unknown provider or missing key (AC4). `answer()` builds its own
            # and maps the failure to a category the error table knows, so this
            # defers to it rather than keeping a second copy of that mapping in
            # sync. Both attempts fail immediately and neither touches the
            # network.
            return answer(
                question,
                rendering=_DEPLOYED_RENDERING,
                glossary=_DEPLOYED_GLOSSARY,
                schema=schema,
            )

        timed.append(provider)
        return answer(
            question,
            provider=provider,
            rendering=_DEPLOYED_RENDERING,
            glossary=_DEPLOYED_GLOSSARY,
            schema=schema,
        )

    fingerprints = _deployed_fingerprints()
    if fingerprints is None:
        # No schema was read, so there is nothing to hand on: `answer()` reads
        # its own and reports the unreachable database in its own category.
        return _Answered(compute(), False, timed[0] if timed else None)

    # **Carried, never inspected.** T3 threads the one schema read from
    # `api/agent/` through to `api/agent/`; this module holds the value and
    # names neither its type nor any attribute of it, which is what keeps AC4's
    # two structural assertions true -- no `api.db` import, and no database
    # call from this module.
    schema, schema_fp, prompt_fp = fingerprints
    key = cache.cache_key(question, schema_fp, prompt_fp)
    result, cache_hit = cache.get_or_compute(
        key, lambda: compute(schema), _is_cacheable
    )
    return _Answered(
        result, cache_hit, timed[0] if timed else None, schema_fp, prompt_fp
    )


def _record_history(request, answered, payload, total_ms, usage) -> str | None:
    """Persist one answered question, and never let that failure reach the user.

    Resolved D-2: *the primary contract of `/ask` is to return an answer.* The
    store swallows its own exceptions, so this returns `None` rather than
    raising, and `/health` is where a degraded store becomes visible.

    The returned id is the **answer id** Iteration 8's feedback attaches to --
    an answer, never a question string, because the agent may answer the same
    question differently next time.
    """
    result = answered.result
    provider = answered.provider

    # All three are absent on a cache hit, where no provider was ever built.
    # That is the truthful record: there was no call, so there is no rate-limit
    # snapshot to take and no model to credit. The row that paid for the answer
    # carries them, and `cache_hit` is what connects the two.
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
            provider_ms=provider.elapsed_ms if provider is not None else 0,
            sql=result.sql,
            category=result.category,
            shape=payload["shape"],
            row_count=len(payload["rows"]),
            attempts_used=result.attempts_used,
            # The effective usage, which is zero on a hit. Summing this column
            # across every row gives what was actually billed, with no filter to
            # remember and no double counting.
            total_tokens=usage.total_tokens,
            prompt_tokens=usage.prompt_tokens,
            usage_measured=usage.measured,
            provider_calls=usage.calls,
            cache_hit=payload["cache_hit"],
            tpm_remaining=tokens_left,
            rpd_remaining=requests_left,
            model=getattr(provider, "model", ""),
            # Which schema and which prompt this answer was produced under. The
            # columns have existed since T2 and were always empty; they are what
            # makes a row still readable after the schema or the prompt moves,
            # and they are the same two numbers the cache key is built from.
            schema_fp=answered.schema_fp,
            prompt_fp=answered.prompt_fp,
            steps=tuple(payload["trace"]),
        )
    )
