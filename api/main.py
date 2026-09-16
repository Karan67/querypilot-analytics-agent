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
import os
import pathlib
import time
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from api.agent.fingerprints import DeployedPrompt, deployed_fingerprints
from api.agent.orchestrator import answer
from api.agent.prompts import ADOPTED_RENDERING
from api.agent.tools import execute_sql
from api.targets import CATEGORY_UNKNOWN_DATABASE, DATABASE_TARGETS, DEFAULT_TARGET
from api.llm.base import LLMError, TokenUsage
from api.llm.factory import get_provider
from api.store import history
from api.store.history import AskRecord
from api import config
from api.http import auth, cache, forwarded, headers, quota
from api.http.errors import STATUS_ANSWERED, failure_for
from api.http.serialization import encode_rows, schema_to_dict
from api.http.shapes import SHAPE_CHARTABLE, SHAPE_EMPTY, chart_series, classify

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


def _unauthorized() -> JSONResponse:
    """The one refusal, for all four ways of failing (AC6).

    Same status, same body, same headers whether the header was absent,
    malformed, named an identity that does not exist, or named one that does
    and got the secret wrong. A gate that distinguishes them tells an attacker
    which half of the guess was right; "no such user" tells them the endpoint
    is worth attacking at all.
    """
    return JSONResponse(
        status_code=401,
        content=auth.UNAUTHORIZED_BODY,
        headers=auth.UNAUTHORIZED_HEADERS,
    )


@app.middleware("http")
async def require_credentials(request: Request, call_next):
    """The gate (AC1), and it is middleware for a measured reason.

    **A route dependency cannot protect the static mount.** `013-auth-plan.md`
    §1 tested it rather than assuming: against an app-level deny-all dependency,
    `GET /` returned 401 and `GET /static/x.js` returned 200, because
    `app.mount("/static", StaticFiles(...))` is a sub-application and the
    parent's dependency injection does not run inside it. Middleware sits above
    routing, so the mount is covered by the same rule as everything else
    (resolved D-1, D-3).

    The second thing middleware buys is that a route added next year is
    protected by default. The exemption is data — `auth.OPEN_PATHS` — and T5's
    completeness test reads it, so a new unprotected path fails the suite rather
    than shipping quietly.

    **A 401 is never routed through `api/http/errors.py`.** That table maps
    *answer* failures onto status codes; an unauthorised caller has not asked a
    question and there is nothing to report `ok: false` about (`013-auth.md` §5).
    """
    if auth.is_open(request.url.path):
        return await call_next(request)

    try:
        identities = auth.current_identities(config.get_secret(auth.USERS_ENV))
    except auth.AuthConfigurationError as exc:
        # **Fail closed** (resolved D-2). A deployment with a typo in the
        # variable name refuses everything rather than serving the API to the
        # internet, which is the whole argument: a mechanism that is switched
        # off must not look identical to one that is working.
        #
        # Logged on every protected request rather than once at startup, and
        # that is deliberate -- the reason is then discoverable from any point
        # in the log, not only from the lines nobody scrolls back to. The
        # message names the variable and never quotes its value (AC5).
        # Prefixed with where, not with what: the exception already says "no
        # request can be authorised", and a prefix repeating it produced
        # "no request can be authorised: ... so no request can be authorised"
        # in the container log. Seen in the real container at T6, not guessed.
        logger.error("QueryPilot auth gate: %s", exc)
        return _unauthorized()

    presented = auth.parse_basic(request.headers.get("Authorization"))
    if presented is None:
        return _unauthorized()

    identity = auth.verify(identities, *presented)
    if identity is None:
        return _unauthorized()

    # Iteration 12 T9. The one place the name that authenticated is available
    # at all -- `verify()` returns it and nothing downstream re-derives it.
    # `/ask` reads this to weigh the request against its own daily ceiling; no
    # other route looks at it, so leaving it unset costs nothing anywhere else.
    request.state.identity = identity

    return await call_next(request)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """AC7. Every response carries the security headers, including the 401.

    **Registered after the gate on purpose.** Starlette builds its middleware
    stack so that the last one registered is the outermost, so declaring this
    below `require_credentials` is what puts it *around* the gate. Register it
    above and it would run only for requests the gate already let through --
    and the `401` is precisely the response an unauthenticated browser renders,
    so it is the one that most needs a content policy.

    The policy itself lives in `api/http/headers.py`. This function is the
    registration and nothing else, on the same terms as `api/agent/tools.py`:
    a thin wrapper delegating to the module that owns the decision.
    """
    response = await call_next(request)
    headers.apply(response.headers, secure=request.url.scheme == "https")
    return response


@app.middleware("http")
async def trust_configured_proxies(request: Request, call_next):
    """AC10. Rewrite the scheme and client from `X-Forwarded-*`, if we may.

    **The outermost middleware, and it has to be.** Everything downstream that
    asks how the request arrived -- the header middleware's HSTS decision
    above, and any future per-client accounting -- reads `request.url.scheme`
    and `request.client`. Those come from the ASGI scope, so the correction has
    to happen before anything else looks, which means this runs first, which in
    Starlette means it is registered last.

    The decision itself is `api/http/forwarded.py`'s: believe the headers only
    when the peer that sent them is inside a configured proxy network. A
    misconfiguration here is silent in both directions -- too permissive and
    any client can claim HTTPS, too strict and a correct deployment never
    emits HSTS -- so `/health` reports the resolved networks.

    A malformed `QUERYPILOT_TRUSTED_PROXIES` is not allowed to take the service
    down. The request proceeds on the evidence of the connection itself, which
    is the conservative answer: no HSTS, and the peer's own address.
    """
    try:
        raw = os.environ.get(forwarded.TRUSTED_PROXIES_ENV)
        client_host = request.client.host if request.client else None
        scheme = forwarded.resolve_scheme(
            request.url.scheme,
            client_host,
            request.headers.get(forwarded.FORWARDED_PROTO_HEADER),
            raw,
        )
        resolved = forwarded.resolve_client(
            client_host, request.headers.get(forwarded.FORWARDED_FOR_HEADER), raw
        )
    except forwarded.TrustConfigurationError as exc:
        logger.warning("trusted proxy configuration is unusable: %s", exc)
        return await call_next(request)

    request.scope["scheme"] = scheme
    if resolved and resolved != client_host:
        port = request.scope["client"][1] if request.scope.get("client") else 0
        request.scope["client"] = (resolved, port)
    return await call_next(request)


def _auth_status() -> dict:
    """Whether the credential map is usable, for `/health` to report.

    **The other half of failing closed.** D-2 chose 401-everything over
    refusing to boot precisely so an operator can still ask the container what
    is wrong — and that answer has to be somewhere. `api/http/auth.py`'s own
    docstring promises it lands here and in the logs.

    Shaped like the `history` block beside it, which reports the same kind of
    thing: a subsystem that is degraded while the service is up. The message is
    safe to return to an anonymous caller because `load_identities` is tested
    never to quote a secret, and because when this is `false` every protected
    route is refusing anyway.
    """
    try:
        auth.current_identities(config.get_secret(auth.USERS_ENV))
    except auth.AuthConfigurationError as exc:
        return {"configured": False, "error": str(exc)}
    return {"configured": True, "error": ""}


@app.get("/health", tags=["ops"])
def health(request: Request) -> JSONResponse:
    """Liveness plus target-database readiness.

    Iteration 0 is done when this endpoint reports ``ok``, because that proves
    the whole chain: the API started, the read-only role exists, it can
    authenticate, and the sample dataset was loaded.

    An **authenticated** caller sees the full body::

        {
          "status": "ok",
          "database": {
            "connected": true,
            "user": "querypilot_ro",
            "database": "chinook",
            "public_tables": 12
          },
          "auth": {"configured": true, "error": ""}
        }

    (plus `history`, `proxy`, `secrets` and `spend` — see below), or 503 with
    ``{"status": "degraded", "database": {"connected": false, "error": "..."}}``
    if the database is unreachable. A health check that reports healthy while
    its dependency is down is worse than no health check, so the database
    round-trip is not optional here.

    **An anonymous caller sees only `{"status": "ok"}`, or `{"status":
    "degraded"}` on a 503** (Iteration 12 T10, resolved spec §7 Q-H). Every
    field this endpoint could return was individually harmless on a laptop —
    ``querypilot_ro`` and ``chinook`` are both already in the README — but
    together, to a stranger, they are a free answer to "is this worth
    attacking, and what is it running." The status **code** is what a
    healthcheck actually needs, and `api/healthcheck.py` was written at T3 to
    read only that; collapsing the body changes nothing it depends on. This
    reverses `013-auth.md`'s D-4, which kept `/health` unchanged specifically
    to let an *anonymous* operator see why authentication was misconfigured —
    that operator now has to authenticate to see it, on the same terms as
    every other diagnostic below.

    **Iteration 10 made this the only endpoint an anonymous caller can reach
    at all** (`013-auth.md` §2.7: the compose healthcheck probes it with a bare
    ``urlopen`` and no credentials, so a gate here makes the container
    permanently unhealthy). T10 narrows what reaching it gets you; it does not
    reopen the gate.

    Goes through `execute_sql()` like everything else (see `_HEALTH_QUERY`).
    That also simplifies the failure handling: the exception cases this used to
    catch by hand — a missing DSN, an auth failure, a database still starting —
    are what `ExecutionResult.ok` already reports.
    """
    authenticated = (
        auth.identify_caller(
            request.headers.get("Authorization"), config.get_secret(auth.USERS_ENV)
        )
        is not None
    )

    result = execute_sql(_HEALTH_QUERY)

    if not result.ok:
        logger.warning("health check failed: %s (%s)", result.error, result.category)
        if not authenticated:
            return JSONResponse(status_code=503, content={"status": "degraded"})
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "database": {"connected": False, "error": result.error},
                "auth": _auth_status(),
            },
        )

    if not authenticated:
        return JSONResponse(status_code=200, content={"status": "ok"})

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
            # Iteration 10. `status` is deliberately **not** widened to a
            # `degraded_auth` value: resolved D-4 said leave `/health` exactly
            # as it is, and the compose healthcheck reads the status code
            # rather than this field. A misconfigured gate is reported, not
            # escalated -- the container is genuinely up, and every protected
            # route is genuinely refusing.
            "auth": _auth_status(),
            # Iteration 12 T6. Which networks this deployment believes when
            # they claim a request arrived over HTTPS.
            #
            # It is here because getting it wrong is silent in both
            # directions: too permissive and any client can claim HTTPS, so
            # HSTS becomes a lie it tells on its own behalf; too strict and a
            # correct deployment behind Caddy never emits HSTS at all. Neither
            # failure produces an error anybody sees. Networks only -- never a
            # header, never a client address.
            "proxy": forwarded.status(),
            # Iteration 12 T8. Where each secret was read from, never what it
            # says. `source` is file, environment or unset; `readable` is the
            # one thing an operator cannot see from outside, because a
            # `_FILE` path that does not exist fails closed on purpose and
            # would otherwise look identical to a credential that was simply
            # never configured.
            "secrets": {
                auth.USERS_ENV: config.secret_status(auth.USERS_ENV),
                "GROQ_API_KEY": config.secret_status("GROQ_API_KEY"),
                # Iteration 13 (specs/016-second-llm-provider.md). Reported
                # unconditionally like GROQ_API_KEY, even though Groq stays
                # the runtime default (D-E) -- an operator switching
                # QUERYPILOT_LLM_PROVIDER=cerebras needs to see this is
                # configured *before* flipping that switch, not after.
                "CEREBRAS_API_KEY": config.secret_status("CEREBRAS_API_KEY"),
            },
            # Iteration 12 T9. Today's global count and both configured
            # limits, read without writing -- a health check that itself
            # counted as a question would inflate the number it reports.
            "spend": history.spend_status(),
        },
    )


def _target_configured(name: str) -> bool:
    """Is this registered target's DSN actually set on this deployment?

    Checks presence only, never liveness -- a real connection attempt on
    every `/databases` listing would make browsing the dropdown as slow as
    asking a question, for a fact `/health`'s own `database.connected`
    already reports for the one target that matters to the healthcheck.
    """
    entry = DATABASE_TARGETS.get(name)
    return entry is not None and bool(os.environ.get(entry.dsn_env, "").strip())


def _resolve_target(database: str | None) -> str | None:
    """The registered target a client asked for, or `None` if it names none.

    `None` in, `DEFAULT_TARGET` out -- every caller that predates dynamic
    database switching. `None` out means the caller must refuse with
    `CATEGORY_UNKNOWN_DATABASE` (400) before spending an agent turn or a
    round trip on a target that was never going to exist.

    **A known target whose DSN is not configured is deliberately not caught
    here.** It is still returned, and reaches `execute_sql()`/`get_schema()`
    exactly like a genuinely unreachable database would -- both now
    categorise identically (`connection_error`, 503) since `execute_sql`'s
    own gate covers a missing DSN as of this feature, so there is no second
    definition of "unreachable" to keep in sync with the first.
    """
    if database is None:
        return DEFAULT_TARGET
    return database if database in DATABASE_TARGETS else None


@app.get("/databases", tags=["ops"])
def databases_endpoint() -> JSONResponse:
    """Which databases this deployment can be asked about.

    Invariant #1 of dynamic database switching: a client learns *names*
    here, never a connection string -- `available` says whether this
    deployment has that target's DSN configured, nothing about where it
    points.
    """
    targets = [
        {"name": name, "available": _target_configured(name)}
        for name in sorted(DATABASE_TARGETS)
    ]
    return JSONResponse(
        status_code=200,
        content={"targets": targets, "default": DEFAULT_TARGET},
    )


@app.get("/schema", tags=["ops"])
def schema_endpoint(database: str | None = None) -> JSONResponse:
    """The structural map of one registered database (018-ui-redesign.md AC4-AC7).

    `?database=<name>` (dynamic-database-switching) selects which; omitted
    or `null` means `DEFAULT_TARGET`, unchanged from before this parameter
    existed. An unrecognised name is refused with 400 before any database
    round trip is attempted, mirroring `/ask`'s own validation below.

    Protected by the same Basic Auth gate as every route but `/health` --
    nothing here is added to `auth.OPEN_PATHS`.

    Reads the schema the same way `/ask` already does: through
    `_deployed_fingerprints()`, not a direct `get_schema()` call. That is
    what keeps this module's "reaches the database only through the agent"
    guarantee intact (AC5) -- `test_ac4_the_only_database_call_in_the_module_
    is_execute_sql` asserts `api/main.py` imports nothing from `api.db`
    except `execute_sql`, and a direct `get_schema()` import here would trip
    it exactly like it would have at the cache key (`_deployed_fingerprints`'s
    own docstring).

    A discarded config argument (`rendering`/`glossary`) is a real cost of
    reuse here -- this route has no prompt to fingerprint -- but it is one
    already-cheap catalog read, not a new database round trip shape.
    """
    target = _resolve_target(database)
    if target is None:
        return JSONResponse(
            status_code=400,
            content={
                "error": (
                    f"Unknown database {database!r}. Known databases: "
                    f"{sorted(DATABASE_TARGETS)}."
                )
            },
        )

    fingerprints = _deployed_fingerprints(target=target)
    if fingerprints is None:
        # Same failure `/health` reports as `database.connected: false`,
        # phrased for this route's own shape rather than reusing that one
        # (AC7: a legible message, not a blank panel or a stack trace). Also
        # reached when `target` is registered but not configured on this
        # deployment -- `execute_sql()` categorises that identically to an
        # unreachable database, so there is nothing extra to check here.
        return JSONResponse(
            status_code=503,
            content={"error": "The database schema could not be read."},
        )
    return JSONResponse(
        status_code=200, content=schema_to_dict(fingerprints.schema)
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
    #: Dynamic-database-switching. A target *key*, never a DSN (Invariant
    #: #1) -- `None` (the default, and every client that predates this
    #: field) means `DEFAULT_TARGET`. Validated against `DATABASE_TARGETS`
    #: in `ask()` itself, not here: a bad value is a `400` in the same
    #: answer-shaped body every other `/ask` refusal already uses
    #: (`CATEGORY_UNKNOWN_DATABASE`), not a bare pydantic 422, so the client
    #: always gets the one JSON shape it already handles.
    database: str | None = None


@app.post("/ask", tags=["agent"])
def ask(request: AskRequest, http_request: Request) -> JSONResponse:
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

    **The daily spend ceiling is weighed first** (Iteration 12 T9, resolved
    D-3), before the cache and before any provider is built. Authentication
    narrows who can spend the project's quota; it does not bound how much one
    identity spends, or what a browser tab left reloading costs before anyone
    notices, and that is the gap this closes. A refusal here never reaches
    `_answer_or_replay` and is never written to history: like the 401 above it,
    the caller has not asked a question that was processed, so there is
    nothing for `record_ask` to have a row about.
    """
    # Validated before the spend ceiling is weighed, for the same reason the
    # ceiling itself is weighed before the cache: a request this deployment
    # was never going to be able to serve should not consume a reservation
    # against the daily count either.
    target = _resolve_target(request.database)
    if target is None:
        failure = failure_for(CATEGORY_UNKNOWN_DATABASE)
        return JSONResponse(
            status_code=failure.status,
            content={
                "ok": False,
                "sql": "",
                "columns": [],
                "rows": [],
                "shape": SHAPE_EMPTY,
                "series": None,
                "trace": [],
                "category": CATEGORY_UNKNOWN_DATABASE,
                "error": (
                    f"Unknown database {request.database!r}. Known "
                    f"databases: {sorted(DATABASE_TARGETS)}."
                ),
                "retryable": False,
                "provider": "",
                "model": "",
            },
        )

    identity = getattr(http_request.state, "identity", "") or ""
    decision = history.reserve_question(identity)
    if not decision.allowed:
        failure = failure_for(decision.category)
        return JSONResponse(
            status_code=failure.status,
            content={
                "ok": False,
                "sql": "",
                "columns": [],
                "rows": [],
                # `classify([], [])` -- an unreached agent has no result set at
                # all, and this is the shape the classifier itself gives an
                # empty one, so the ceiling's refusal reuses the same value
                # rather than inventing a fourth meaning of "nothing here".
                "shape": SHAPE_EMPTY,
                "series": None,
                "trace": [],
                "category": decision.category,
                "error": failure.message,
                "retryable": failure.retryable,
                # The agent was never reached (§ above), so there is no
                # provider or model to name -- same reasoning as `sql: ""`.
                "provider": "",
                "model": "",
            },
        )

    started = time.perf_counter()
    answered = _answer_or_replay(request.question, target=target)
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
        # 018-ui-redesign.md AC8/AC10: which provider and model produced this
        # *answer*, unlike `provider_ms` above which reports what *this
        # request* spent. Captured at compute time (`_Computed`) so both
        # survive a cache hit without rebuilding a provider just to ask it
        # its own name.
        "provider": answered.provider_name,
        "model": answered.model,
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


def _is_cacheable(computed) -> bool:
    """Only a successful answer is worth keeping.

    **This follows from D-3 rather than being a preference.** The cache has no
    expiry, so a cached failure would be served for the life of the process: a
    question asked during a thirty-second rate limit would become permanently
    unanswerable, with no way for the user to retry. Re-running a failure costs
    exactly what the first attempt cost, which is the right price for something
    that might now succeed.
    """
    return bool(computed.result.ok)


@dataclasses.dataclass(frozen=True)
class _Computed:
    """One `answer()` call, plus which provider actually produced it.

    This is what the cache stores now (018-ui-redesign.md, D-1) -- not the
    bare `AgentResult` -- so a later cache hit can still report which
    provider and model produced the answer on screen, without rebuilding a
    provider just to ask it its own name. That would undo the exact property
    that makes a hit cheap and outage-proof (`_answer_or_replay`'s docstring).

    `_record_history`'s `model` column stays empty on a hit **on purpose**:
    no call was made *this* request, so no cost is credited to it. This is a
    different question -- what genuinely produced the answer being shown
    right now -- and the two are allowed to disagree.
    """

    result: object
    provider_name: str
    model: str


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
    # Survive a cache hit (unlike `provider` above, which stays `None` on one)
    # because they are strings captured at compute time, not a live object a
    # hit would have to rebuild. See `_Computed`.
    provider_name: str = ""
    model: str = ""


def _deployed_fingerprints(target: str = DEFAULT_TARGET) -> DeployedPrompt | None:
    """`(schema_fp, prompt_fp)` for the configuration this API actually sends.

    Delegates the schema read to `api/agent/`, which is where reading a schema
    to build a prompt already happens. **`api/main.py` is asserted to reach the
    database only through the agent**, and importing `get_schema` here would
    have broken that -- a structural test walks this module's imports and fails
    on anything from `api.db`. Widening the test to admit an introspection call
    would have been the wrong repair: the first version of this task did exactly
    that, and it is how a rule acquires its first undocumented exception.

    `target` (dynamic-database-switching) is not itself an `api.db` import --
    it is a plain key from `api.targets.DATABASE_TARGETS`, resolved and
    validated at the HTTP boundary (`_resolve_target`) before this function
    ever sees it, and it reaches `get_schema()` only through
    `api.agent.fingerprints.deployed_fingerprints`.

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
        rendering=_DEPLOYED_RENDERING, glossary=_DEPLOYED_GLOSSARY, target=target
    )


def _answer_or_replay(question: str, target: str = DEFAULT_TARGET) -> _Answered:
    """Answer the question, or hand back an answer already paid for.

    **The provider is `None` on a hit** -- not merely unused, never built --
    which is what makes a cached answer survive a provider outage or a missing
    key, and what makes `provider_ms` honestly zero rather than a timer that was
    started and never used. `provider_name`/`model` are different: they are
    plain strings captured once at compute time (`_Computed`), so they do
    survive a hit without requiring `get_provider()` to be called again.
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
            return _Computed(
                answer(
                    question,
                    rendering=_DEPLOYED_RENDERING,
                    glossary=_DEPLOYED_GLOSSARY,
                    schema=schema,
                    target=target,
                ),
                "",
                "",
            )

        timed.append(provider)
        return _Computed(
            answer(
                question,
                provider=provider,
                rendering=_DEPLOYED_RENDERING,
                glossary=_DEPLOYED_GLOSSARY,
                schema=schema,
                target=target,
            ),
            getattr(provider, "NAME", ""),
            getattr(provider, "model", ""),
        )

    fingerprints = _deployed_fingerprints(target=target)
    if fingerprints is None:
        # No schema was read, so there is nothing to hand on: `answer()` reads
        # its own and reports the unreachable database in its own category.
        computed = compute()
        return _Answered(
            computed.result,
            False,
            timed[0] if timed else None,
            provider_name=computed.provider_name,
            model=computed.model,
        )

    # **Carried, never inspected.** T3 threads the one schema read from
    # `api/agent/` through to `api/agent/`; this module holds the value and
    # names neither its type nor any attribute of it, which is what keeps AC4's
    # two structural assertions true -- no `api.db` import, and no database
    # call from this module.
    schema, schema_fp, prompt_fp = fingerprints
    # `target` folds into the key explicitly, defense in depth alongside the
    # fingerprints: `schema_fp` already differs between databases with
    # different structures, but a key that names the target rather than
    # relying solely on that to disambiguate is one a reader can trust
    # without re-deriving why two schemas can never collide.
    key = cache.cache_key(question, schema_fp, prompt_fp, target)
    computed, cache_hit = cache.get_or_compute(
        key, lambda: compute(schema), _is_cacheable
    )
    return _Answered(
        computed.result,
        cache_hit,
        timed[0] if timed else None,
        schema_fp,
        prompt_fp,
        provider_name=computed.provider_name,
        model=computed.model,
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
