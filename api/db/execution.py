"""Query execution — the only place generated SQL reaches the database.

Implements `specs/003-execute-sql.md`. Per `specs/000-project.md` §5 the logic
lives here and `api/agent/tools.py` holds only a registered wrapper.

This module *consults* the safety layer; it is not part of it. Gate 2 stays
whole in `api/safety/validator.py`, and this file may not reimplement, relax, or
second-guess it.

Four things happen on every call, in this order::

    validate_sql()                  Gate 2  — no bypass, no flag (AC1-AC4)
    BEGIN; SET TRANSACTION READ ONLY   Gate 1b — refuses even a superuser (AC5)
    SET LOCAL statement_timeout        Gate 3  — scoped to the transaction (AC9)
    fetchmany(MAX_ROWS + 1); ROLLBACK  Gate 3  — memory cap (AC11-AC14), AC6

**Failures are return values, never exceptions** (AC16, AC23). The agent reads
the reason and retries against it, so an exception escaping here would end the
loop instead of informing it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from api.db.engine import get_engine
from api.safety.validator import validate_sql

logger = logging.getLogger("querypilot.execution")

#: Maximum rows returned to the caller (AC11, resolved Q-A).
#:
#: The cap is applied when fetching, never by rewriting the query (AC15), so the
#: SQL shown to the user stays byte-identical to the SQL that ran.
MAX_ROWS = 1000

#: Statement timeout applied per transaction via SET LOCAL (AC9, resolved Q-C).
#:
#: Matches the role default from `db/init/03_readonly_role.sh`. Set here as well
#: so the limit is explicit at the call site rather than inherited from role
#: configuration a future migration might not reproduce.
STATEMENT_TIMEOUT = "10s"

# --- PostgreSQL SQLSTATEs, which drive categorisation entirely -------------
# Measured, not looked up. No categorisation decision in this module reads
# message text -- the same discipline the validator applies to statement types,
# for the same reason: message wording is not a contract.

#: 57014 query_canceled -- what statement_timeout produces.
SQLSTATE_QUERY_CANCELED = "57014"

#: 25006 read_only_sql_transaction -- a write attempted in our transaction.
SQLSTATE_READ_ONLY_TRANSACTION = "25006"

# --- Result categories (AC19) ----------------------------------------------

#: Gate 2 refused it. It never ran.
CATEGORY_REJECTED = "rejected"

#: Exceeded the statement timeout. The model should narrow the query.
CATEGORY_TIMEOUT = "timeout"

#: Postgres ran it and refused it -- bad column, bad table, type error.
CATEGORY_DATABASE_ERROR = "database_error"

#: Could not reach the database. Not the model's fault; retrying is pointless.
CATEGORY_CONNECTION_ERROR = "connection_error"

#: SQLSTATE 25006 (AC26). Gate 2 accepted a statement the database refused as a
#: write. This is a validator defect, never a model mistake, and it is the one
#: condition here that means the safety layer is wrong.
CATEGORY_GATE_VIOLATION = "gate_violation"

@dataclass(frozen=True)
class ExecutionResult:
    """The outcome of one execution attempt.

    Frozen, so a caller cannot mutate a result it was handed, and so equality is
    structural -- which is what AC25 (determinism) is asserted with.

    On success: `ok` is True, `columns` and `rows` carry the data, `category` and
    `error` are empty.

    On failure: `ok` is False, `category` says what kind of failure (AC19) and
    `error` says what happened in terms the agent can act on. `sqlstate` and
    `hint` are populated when PostgreSQL supplied them.

    `rows` holds **native Python types** -- `Decimal` for NUMERIC, `datetime` for
    TIMESTAMP (resolved Q-B). Converting to JSON primitives happens at the API
    boundary, not here: `Decimal` to `float` silently loses precision, and an
    analytics product should not degrade its numbers on the way out of the
    database.
    """

    ok: bool
    columns: tuple[str, ...] = ()
    rows: tuple[tuple, ...] = field(default=())
    row_count: int = 0
    truncated: bool = False
    category: str = ""
    error: str = ""
    sqlstate: str = ""
    hint: str = ""


def _primary_message(exc: SQLAlchemyError) -> str:
    """The server's primary message, without the surroundings.

    `str(exc)` from SQLAlchemy appends the full SQL and a documentation URL, and
    psycopg connection failures embed the host, port and resolved address --
    "connection to server at 'localhost', port 5432 ... failed". None of that may
    reach the agent (AC22): its context is model input, and from Iteration 6 it
    is streamed to a browser.

    `diag.message_primary` is exactly the one line the server sent, and nothing
    else.
    """
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    primary = getattr(diag, "message_primary", None)
    if primary:
        return str(primary).strip()
    return str(orig or exc).strip().splitlines()[0]


def _categorise(exc: SQLAlchemyError) -> ExecutionResult:
    """Turn a database exception into a categorised failure (AC19-AC22).

    **Driven entirely by SQLSTATE, never by message text.** Wording varies with
    server version and locale; SQLSTATE does not. Same discipline the validator
    applies to statement types, for the same reason.
    """
    orig = getattr(exc, "orig", None)
    sqlstate = str(getattr(orig, "sqlstate", None) or "")
    diag = getattr(orig, "diag", None)
    hint = str(getattr(diag, "message_hint", None) or "").strip()

    if sqlstate == SQLSTATE_READ_ONLY_TRANSACTION:
        # AC26. Gate 2 already certified this as a single read-only SELECT, so a
        # read-only violation can only mean the validator passed a write. That
        # is a defect in the safety layer, not a mistake by the model, and it is
        # the one condition in this module logged at error level.
        #
        # Kept out of `database_error` deliberately: the retry loop would
        # otherwise spend its budget asking the model to rewrite a query that
        # should never have been accepted, disguising a stop-the-line safety
        # defect as an accuracy problem.
        logger.error(
            "GATE 2 FAILURE: the validator accepted a statement PostgreSQL "
            "refused as a write (SQLSTATE %s). This is a safety defect, not a "
            "model error.",
            sqlstate,
        )
        return ExecutionResult(
            ok=False,
            category=CATEGORY_GATE_VIOLATION,
            error=(
                "The query passed validation but the database refused it as a "
                "write. This indicates a defect in the SQL validator, not a "
                "problem with the question."
            ),
            sqlstate=sqlstate,
        )

    if sqlstate == SQLSTATE_QUERY_CANCELED:
        return ExecutionResult(
            ok=False,
            category=CATEGORY_TIMEOUT,
            error=(
                f"Query exceeded the {STATEMENT_TIMEOUT} statement timeout and "
                f"was cancelled. Narrow it: add filters, reduce the number of "
                f"joined rows, or aggregate earlier."
            ),
            sqlstate=sqlstate,
        )

    if sqlstate:
        # The server ran it and refused it -- bad column, bad table, type error.
        # The primary message and hint are the agent's raw material for the
        # retry, so both are passed through rather than summarised.
        return ExecutionResult(
            ok=False,
            category=CATEGORY_DATABASE_ERROR,
            error=_primary_message(exc),
            sqlstate=sqlstate,
            hint=hint,
        )

    # No SQLSTATE means the statement never reached the server. Rewriting the
    # query cannot help, and the loop needs to know that so it stops trying.
    return ExecutionResult(
        ok=False,
        category=CATEGORY_CONNECTION_ERROR,
        error=(
            "Could not reach the database. This is not a problem with the "
            "query, and rewriting it will not help."
        ),
    )


def _harden(connection) -> None:
    """Apply Gate 1b and Gate 3 to the open transaction.

    Both statements must run **inside** an already-begun transaction:

    * `SET TRANSACTION READ ONLY` is scoped to the current transaction by
      PostgreSQL itself. Chosen over SQLAlchemy's
      `execution_options(postgresql_readonly=True)`, which is a *session*
      characteristic that stops leaking only because the pool resets it --
      measured clean today, but `pool_reset_on_return=None` is a legal setting,
      and a security boundary should not depend on our own pool configuration.

    * `SET LOCAL statement_timeout` reverts on rollback. Measured: 250ms inside
      the transaction, back to the role default of 10s on the next checkout.
      Plain `SET` would poison every later query on that pooled connection.

    **`SET LOCAL` outside a transaction is a silent no-op** -- PostgreSQL only
    warns -- so issuing these on an autocommit connection would leave Gate 3
    doing nothing while looking correct. `test_ac9_timeout_is_actually_in_force`
    asserts the effect rather than the call.
    """
    connection.execute(text("SET TRANSACTION READ ONLY"))
    # STATEMENT_TIMEOUT is a module constant, never input. Interpolating
    # anything into SQL in this project deserves justifying: PostgreSQL does not
    # accept a bind parameter in SET LOCAL, and this value never originates
    # outside this file.
    connection.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))


def execute_sql(sql: str) -> ExecutionResult:
    """Validate, then run one read-only query.

    Args:
        sql: the candidate statement, as generated by the model.

    Returns:
        An `ExecutionResult`. Expected failures -- rejection, timeout, a bad
        column, an unreachable database -- are returned, not raised (AC16).

    **There is no way to skip validation** (AC2). No parameter, keyword,
    environment variable, or module toggle. A `skip_validation` argument would
    defeat the entire safety layer, and the absence of one is deliberate: not for
    tests, not for `run_evals.py`, not for debugging.

    Never raises (AC23).
    """
    # Gate 2 first, before a connection is acquired (AC3). A rejected query
    # costs no connection and touches no pool.
    ok, reason = validate_sql(sql)
    if not ok:
        # The validator's reason is passed through verbatim (AC4). `002` spent
        # its whole design budget making that text actionable; re-wording or
        # truncating it here would throw that away.
        return ExecutionResult(ok=False, category=CATEGORY_REJECTED, error=reason)

    try:
        return _run(sql)
    except SQLAlchemyError as exc:
        # Every expected database failure lands here and leaves as a categorised
        # result (AC16). Named exception only -- a bare `except Exception` would
        # swallow real bugs in this module and report them to the agent as
        # database errors, which is the lesson `002` learned catching TokenError.
        return _categorise(exc)


def _run(sql: str) -> ExecutionResult:
    """Execute one already-validated statement. Callers must have run Gate 2.

    Separate from `execute_sql` so the exception boundary is visible: everything
    in here may raise, and exactly one caller turns that into a result.
    """
    with get_engine().connect() as connection:
        transaction = connection.begin()
        try:
            _harden(connection)
            # stream_results goes on the STATEMENT, never the connection.
            # Set at connection level it applies to every statement, and
            # SQLAlchemy wraps each one in `DECLARE ... CURSOR FOR` -- which is
            # a syntax error for `SET TRANSACTION READ ONLY`, so the guards in
            # _harden() would fail before the query ever ran.
            #
            # A server-side cursor is what keeps the row cap meaningful:
            # measured on a cross join, fetchmany(5) took 0.13s through a
            # client-side cursor (which materialises the whole result first)
            # versus 0.02s server-side.
            result = connection.execute(
                text(sql).execution_options(stream_results=True)
            )

            # Columns come from the cursor description, so they are present even
            # when nothing matched (AC18). An empty result is a success: the
            # agent needs the column names to say "none" rather than "error".
            columns = tuple(result.keys())

            # One row beyond the cap, so truncation is known without a second
            # COUNT(*) that would double the work and could disagree under
            # concurrency (AC13).
            fetched = result.fetchmany(MAX_ROWS + 1)
            truncated = len(fetched) > MAX_ROWS
            rows = tuple(tuple(row) for row in fetched[:MAX_ROWS])

            # Close before the rollback: a streamed result holds a server-side
            # cursor, and rolling back underneath an open one is a needless race.
            result.close()

            return ExecutionResult(
                ok=True,
                columns=columns,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
            )
        finally:
            # Always, on both paths (AC6). A SELECT needs no commit, and never
            # committing removes the possibility of one arriving by accident --
            # there is no code path in this module that can write.
            transaction.rollback()
