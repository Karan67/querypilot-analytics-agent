"""Acceptance tests for `execute_sql()` — specs/003-execute-sql.md.

T1 covers AC1–AC4 and the fail-closed invariant. Execution itself arrives with
T2/T3, so nothing here needs a database yet — the Gate 2 interception tests are
pure, and deliberately so: proving a rejected query never reaches the database is
easiest when there is provably no database to reach.
"""

from __future__ import annotations

import inspect as pyinspect

import pytest

from api.db.execution import (
    CATEGORY_REJECTED,
    MAX_ROWS,
    STATEMENT_TIMEOUT,
    ExecutionResult,
    execute_sql,
)

#: Statements Gate 2 refuses. Kept short and pointed — the exhaustive corpus
#: lives in `tests/test_validator.py`; these exist to prove interception, not to
#: re-test the validator.
REJECTED_BY_GATE_TWO = [
    pytest.param("DROP TABLE track", id="drop"),
    pytest.param("DELETE FROM track", id="delete"),
    pytest.param("UPDATE track SET name = 'x'", id="update"),
    pytest.param("SELECT 1; DROP TABLE track", id="stacked"),
    pytest.param(
        "WITH gone AS (DELETE FROM track RETURNING *) SELECT * FROM gone",
        id="delete-cte",
    ),
    pytest.param("SELECT * INTO evil FROM track", id="select-into"),
    pytest.param("SET statement_timeout = 0", id="set-timeout"),
    pytest.param("", id="empty"),
]


# --- AC1, AC4: interception -------------------------------------------------


@pytest.mark.parametrize("sql", REJECTED_BY_GATE_TWO)
def test_ac1_rejected_queries_are_intercepted(sql):
    result = execute_sql(sql)
    assert result.ok is False
    assert result.category == CATEGORY_REJECTED


@pytest.mark.parametrize("sql", REJECTED_BY_GATE_TWO)
def test_ac4_validator_reason_is_passed_through_verbatim(sql):
    """Not re-worded, not summarised, not truncated. `002` spent its entire
    design budget making that text actionable for the retry loop; restating it
    here would throw that away."""
    from api.safety.validator import validate_sql

    _, reason = validate_sql(sql)
    assert execute_sql(sql).error == reason


def test_ac1_calls_the_validator_rather_than_reimplementing_it(monkeypatch):
    """Interception must go through Gate 2 itself.

    A copy of the rules here would drift from the validator, and the drift would
    be invisible until something got through. Replacing `validate_sql` with a
    stub proves the real one is on the path.
    """
    from api.db import execution

    calls = []

    def _spy(sql):
        calls.append(sql)
        return False, "stub rejection"

    monkeypatch.setattr(execution, "validate_sql", _spy)
    result = execute_sql("SELECT 1")

    assert calls == ["SELECT 1"]
    assert result.error == "stub rejection"


# --- AC2: no bypass ---------------------------------------------------------


def test_ac2_signature_offers_no_bypass():
    """`skip_validation=True` must never exist — not for tests, not for
    `run_evals.py`, not for debugging. A single keyword would defeat the whole
    safety layer, so its absence is asserted rather than assumed.

    Scans **identifiers**, not raw source. An earlier version grepped the text
    and failed on this module's own docstring, which explains why the bypass is
    absent. A test that forbids documenting a security decision is the wrong
    test.
    """
    import ast

    signature = pyinspect.signature(execute_sql)
    assert list(signature.parameters) == ["sql"]

    tree = ast.parse(pyinspect.getsource(execute_sql))
    identifiers = {
        node.arg if isinstance(node, ast.arg) else getattr(node, "id", None)
        for node in ast.walk(tree)
        if isinstance(node, (ast.arg, ast.Name))
    }
    identifiers |= {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    identifiers.discard(None)

    for smell in ("skip_valid", "unsafe", "force", "bypass", "no_valid", "trusted"):
        offenders = [name for name in identifiers if smell in name.lower()]
        assert not offenders, f"possible bypass affordance: {offenders}"


def test_ac2_no_module_level_toggle():
    """Nor a module constant someone could flip at runtime."""
    from api.db import execution

    for name in dir(execution):
        lowered = name.lower()
        assert "skip" not in lowered
        assert "bypass" not in lowered
        assert not (lowered.startswith("disable") or lowered.startswith("allow_write"))


# --- AC3: validation happens before a connection is acquired ----------------


def test_ac3_rejected_query_acquires_no_connection(monkeypatch):
    """Ordering, asserted rather than assumed.

    If `get_engine` were called before validation, a rejected query would still
    cost a connection — and, more importantly, the code would be one edit away
    from executing before checking.
    """
    from api.db import execution

    def _explode():
        raise AssertionError("a connection was acquired for a rejected query")

    monkeypatch.setattr(execution, "get_engine", _explode, raising=False)

    result = execute_sql("DROP TABLE track")
    assert result.category == CATEGORY_REJECTED


# --- contract ---------------------------------------------------------------


def test_result_is_frozen_and_compares_structurally():
    import dataclasses

    result = execute_sql("SELECT 1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.ok = True

    assert execute_sql("DROP TABLE track") == execute_sql("DROP TABLE track")


def test_result_defaults_are_empty_not_none():
    """Empty tuples and empty strings, never None — a caller iterating `rows` or
    formatting `error` should not have to null-check first."""
    result = ExecutionResult(ok=False)
    assert result.columns == ()
    assert result.rows == ()
    assert result.row_count == 0
    assert result.truncated is False
    assert result.error == "" and result.sqlstate == "" and result.hint == ""


def test_constants_match_the_resolved_decisions():
    assert MAX_ROWS == 1000              # Q-A
    assert STATEMENT_TIMEOUT == "10s"    # Q-C, matches the role default


def test_ac23_never_raises_on_hostile_input():
    for value in [None, 42, [], "", "x" * 50_000, "((((", "\x00"]:
        result = execute_sql(value)
        assert isinstance(result, ExecutionResult)
        assert result.ok is False
        assert result.error


# --- T2: the read-only transaction (AC5-AC9) -------------------------------


def test_ac5_transaction_is_read_only(configured_database):
    """Asserted against the database, not against the code that sets it."""
    from sqlalchemy import text

    from api.db.engine import get_engine
    from api.db.execution import _harden

    with get_engine().connect() as conn:
        trans = conn.begin()
        try:
            _harden(conn)
            assert conn.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
        finally:
            trans.rollback()


def test_ac9_timeout_is_actually_in_force(configured_database, monkeypatch):
    """`SET LOCAL` outside a transaction is a silent no-op — PostgreSQL only
    warns — so this asserts the *effect*, not that the statement was issued.

    **The value must differ from the role default**, and that is not a detail.
    An earlier version asserted `SHOW statement_timeout == "10s"`, which is also
    what `db/init/03_readonly_role.sh` pins on the role. Deleting the entire
    `SET LOCAL` line failed nothing: the role default masked it perfectly.

    Mutation testing found that; review would not have. Overriding the constant
    to a value no role default could supply is what gives this test teeth.
    """
    from sqlalchemy import text

    from api.db import execution
    from api.db.engine import get_engine

    monkeypatch.setattr(execution, "STATEMENT_TIMEOUT", "250ms")

    with get_engine().connect() as conn:
        trans = conn.begin()
        try:
            execution._harden(conn)
            in_force = conn.execute(text("SHOW statement_timeout")).scalar_one()
        finally:
            trans.rollback()

    assert in_force == "250ms", (
        f"SET LOCAL did not take effect; statement_timeout is {in_force!r}. "
        f"If that is the role default, Gate 3 is being inherited rather than "
        f"applied here."
    )


def test_ac9_timeout_is_enforced_not_merely_set(configured_database, monkeypatch):
    """The behavioural half: a query exceeding the limit is actually killed.

    Uses a short override so the suite does not pay the production 10 seconds.
    """
    import time

    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    from api.db import execution
    from api.db.engine import get_engine
    from api.db.execution import SQLSTATE_QUERY_CANCELED

    monkeypatch.setattr(execution, "STATEMENT_TIMEOUT", "250ms")

    started = time.perf_counter()
    with get_engine().connect() as conn:
        trans = conn.begin()
        try:
            execution._harden(conn)
            with pytest.raises(SQLAlchemyError) as caught:
                conn.execute(text("SELECT pg_sleep(5)"))
        finally:
            trans.rollback()
    elapsed = time.perf_counter() - started

    assert getattr(caught.value.orig, "sqlstate", None) == SQLSTATE_QUERY_CANCELED
    assert elapsed < 3, f"took {elapsed:.1f}s; the timeout was not enforced"


def test_ac7_read_only_transaction_refuses_writes_distinctly(configured_database):
    """The read-only transaction is a gate *independent* of role privileges —
    measured to refuse even a superuser.

    Its refusal is SQLSTATE 25006, distinct from Gate 1's privilege refusal, and
    that distinction is what AC26 turns into a canary.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    from api.db.engine import get_engine
    from api.db.execution import SQLSTATE_READ_ONLY_TRANSACTION, _harden

    with get_engine().connect() as conn:
        trans = conn.begin()
        try:
            _harden(conn)
            with pytest.raises(SQLAlchemyError) as caught:
                conn.execute(text("CREATE TABLE probe_should_not_exist (id int)"))
            assert getattr(caught.value.orig, "sqlstate", None) == SQLSTATE_READ_ONLY_TRANSACTION
        finally:
            trans.rollback()


def test_ac8_settings_do_not_leak_to_the_next_checkout(configured_database):
    """A leaked `SET` would poison every later query on that pooled connection —
    silently, and in a way no single test of this module would notice."""
    from sqlalchemy import text

    from api.db.engine import get_engine

    execute_sql("SELECT 1")

    with get_engine().connect() as conn:
        assert conn.execute(text("SHOW transaction_read_only")).scalar_one() == "off"
        assert conn.execute(text("SHOW statement_timeout")).scalar_one() == "10s"


def test_ac6_nothing_is_committed(configured_database):
    """There is no commit anywhere in this module, on either path."""
    import inspect as pyi

    from api.db import execution

    source = pyi.getsource(execution)
    assert ".commit()" not in source
    assert "rollback()" in source


def test_ac6_rollback_happens_even_when_the_body_raises(configured_database, monkeypatch):
    """The rollback is in a `finally`. If it were only on the success path, an
    exception would leave the transaction open until the pool recycled it."""
    from api.db import execution

    rolled_back = []
    original = execution._harden

    def _boom(connection):
        original(connection)
        raise RuntimeError("simulated failure inside the transaction")

    monkeypatch.setattr(execution, "_harden", _boom)

    with pytest.raises(RuntimeError):
        execute_sql("SELECT 1")

    # The connection must be usable again immediately, which it would not be if
    # a transaction had been left open on it.
    from sqlalchemy import text

    from api.db.engine import get_engine

    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar_one() == 1
    rolled_back.append(True)
    assert rolled_back


# --- T3: results (AC16, AC17, AC18) ----------------------------------------


def test_ac17_returns_columns_and_rows(configured_database):
    result = execute_sql("SELECT track_id, name FROM track ORDER BY track_id LIMIT 3")
    assert result.ok is True
    assert result.columns == ("track_id", "name")
    assert result.row_count == 3
    assert len(result.rows) == 3
    assert result.truncated is False
    assert result.category == "" and result.error == ""


def test_ac18_empty_result_is_a_success_with_columns(configured_database):
    """An empty result is not an error. The agent needs the column names to
    answer "none" rather than reporting a failure it cannot fix."""
    result = execute_sql("SELECT track_id, name FROM track WHERE 1 = 0")
    assert result.ok is True
    assert result.columns == ("track_id", "name")
    assert result.rows == ()
    assert result.row_count == 0
    assert result.error == ""


def test_native_types_survive_unconverted(configured_database):
    """Resolved Q-B: numeric fidelity is preserved here and JSON conversion
    happens at the API boundary. A future "helpful" `float()` in this module
    would silently degrade every monetary value in the product."""
    from datetime import datetime
    from decimal import Decimal

    result = execute_sql("SELECT unit_price FROM track LIMIT 1")
    assert isinstance(result.rows[0][0], Decimal), "NUMERIC was converted away"

    result = execute_sql("SELECT invoice_date FROM invoice LIMIT 1")
    assert isinstance(result.rows[0][0], datetime), "TIMESTAMP was converted away"


def test_realistic_analytics_query(configured_database):
    result = execute_sql(
        "SELECT g.name, count(*) AS tracks FROM track t "
        "JOIN genre g ON g.genre_id = t.genre_id "
        "GROUP BY g.name ORDER BY tracks DESC LIMIT 5"
    )
    assert result.ok is True
    assert result.columns == ("name", "tracks")
    assert result.row_count == 5


def test_the_view_is_queryable(configured_database):
    """Ties `001` AC12 to real execution: a relation the schema tool reports
    must actually be readable through this path."""
    result = execute_sql("SELECT invoice_id, total FROM invoice_totals LIMIT 3")
    assert result.ok is True
    assert result.row_count == 3


def test_set_operations_execute(configured_database):
    """Ties `002` AC25 to real execution."""
    result = execute_sql(
        "(SELECT name FROM track LIMIT 2) UNION (SELECT name FROM artist LIMIT 2)"
    )
    assert result.ok is True


# --- T4: row cap and truncation (AC11-AC13, AC15) --------------------------


def test_ac11_ac12_large_result_is_capped_and_flagged(configured_database):
    """Silently returning a capped result would let the agent state a
    conclusion drawn from partial data as fact — worse than an error."""
    result = execute_sql("SELECT track_id FROM track")
    assert result.ok is True
    assert result.row_count == MAX_ROWS
    assert len(result.rows) == MAX_ROWS
    assert result.truncated is True


def test_ac13_boundary_exactly_max_rows_is_not_truncated(configured_database):
    """The off-by-one that matters. Fetching MAX_ROWS + 1 and comparing with
    `>` is what makes exactly-MAX_ROWS report honestly; `>=` would claim a
    truncation that did not happen, and fetching only MAX_ROWS would miss one
    that did."""
    result = execute_sql(f"SELECT track_id FROM track LIMIT {MAX_ROWS}")
    assert result.row_count == MAX_ROWS
    assert result.truncated is False


def test_ac13_boundary_one_over_is_truncated(configured_database):
    result = execute_sql(f"SELECT track_id FROM track LIMIT {MAX_ROWS + 1}")
    assert result.row_count == MAX_ROWS
    assert result.truncated is True


def test_ac15_a_smaller_limit_in_the_query_is_respected(configured_database):
    """No LIMIT is injected, so a query carrying its own smaller limit comes
    back untouched and unflagged."""
    result = execute_sql("SELECT track_id FROM track LIMIT 7")
    assert result.row_count == 7
    assert result.truncated is False


def test_ac15_no_limit_is_injected_into_the_sql(configured_database):
    """The SQL shown to the user must be byte-identical to the SQL that ran.
    Capping happens on the fetch, never by rewriting."""
    import inspect as pyi

    from api.db import execution

    source = pyi.getsource(execution.execute_sql)
    for smell in ("LIMIT", "limit "):
        assert smell not in source, f"query rewriting crept in: {smell!r}"


# --- T5-T6: categorisation (AC10, AC19-AC22) -------------------------------


def test_ac19_bad_column_is_a_database_error(configured_database):
    from api.db.execution import CATEGORY_DATABASE_ERROR

    result = execute_sql("SELECT artist_name FROM album")
    assert result.ok is False
    assert result.category == CATEGORY_DATABASE_ERROR
    assert result.sqlstate == "42703"
    assert "artist_name" in result.error


def test_ac19_bad_table_is_a_database_error(configured_database):
    from api.db.execution import CATEGORY_DATABASE_ERROR

    result = execute_sql("SELECT * FROM tracks")
    assert result.category == CATEGORY_DATABASE_ERROR
    assert result.sqlstate == "42P01"


def test_ac20_sqlstate_is_the_categorisation_signal(configured_database):
    """SQLSTATE, not message text. Wording varies with server version and
    locale; the code does not."""
    import inspect as pyi

    from api.db import execution

    source = pyi.getsource(execution._categorise)
    assert "sqlstate ==" in source
    assert "in str(" not in source and ".lower()" not in source


def test_ac21_hint_is_preserved_when_offered(configured_database):
    """Frequently the answer outright, and free."""
    result = execute_sql("SELECT count(*) FROM track GROUP BY nope")
    assert "Perhaps you meant" in result.hint
    assert "track.name" in result.hint


def test_ac21_absent_hint_is_not_an_error(configured_database):
    """Measured: Postgres offers no hint for `SELECT artist_name FROM album`.
    The hint is passed through when offered and never depended on."""
    result = execute_sql("SELECT artist_name FROM album")
    assert result.hint == ""
    assert result.error and result.sqlstate == "42703"


def test_ac10_timeout_is_its_own_category(configured_database, monkeypatch):
    """The agent's correct response to a timeout — narrow the query — differs
    from its response to a bad column, so the categories must differ."""
    import time

    from api.db import execution
    from api.db.execution import CATEGORY_TIMEOUT, SQLSTATE_QUERY_CANCELED

    monkeypatch.setattr(execution, "STATEMENT_TIMEOUT", "250ms")

    started = time.perf_counter()
    result = execution.execute_sql("SELECT pg_sleep(5)")
    elapsed = time.perf_counter() - started

    assert result.ok is False
    assert result.category == CATEGORY_TIMEOUT
    assert result.sqlstate == SQLSTATE_QUERY_CANCELED
    assert elapsed < 3, f"took {elapsed:.1f}s -- the timeout was not applied"
    assert "narrow" in result.error.lower()


def test_ac19_unreachable_database_is_a_connection_error(monkeypatch):
    """Distinct from `database_error` so the loop stops rewriting a query that
    was never the problem."""
    from api.db import engine as engine_module
    from api.db.execution import CATEGORY_CONNECTION_ERROR

    engine_module.get_engine.cache_clear()
    monkeypatch.setenv(
        engine_module.DATABASE_URL_ENV,
        "postgresql+psycopg://nobody:secretpw@querypilot-no-such-host:5432/nothing",
    )
    try:
        result = execute_sql("SELECT 1")
        assert result.ok is False
        assert result.category == CATEGORY_CONNECTION_ERROR
        assert result.sqlstate == ""
    finally:
        engine_module.get_engine.cache_clear()


def test_ac22_no_result_leaks_connection_details(monkeypatch):
    """The agent's context is model input, and from Iteration 6 it is streamed
    to a browser. psycopg's own connection errors embed host, port and resolved
    address, so the raw message must never be passed through."""
    from api.db import engine as engine_module

    engine_module.get_engine.cache_clear()
    monkeypatch.setenv(
        engine_module.DATABASE_URL_ENV,
        "postgresql+psycopg://nobody:secretpw@querypilot-no-such-host:5432/nothing",
    )
    try:
        result = execute_sql("SELECT 1")
        blob = f"{result.error} {result.hint} {result.sqlstate}"
        for leak in ("secretpw", "nobody", "querypilot-no-such-host", "5432",
                     "postgresql://", "Traceback", "psycopg"):
            assert leak not in blob, f"leaked {leak!r}: {blob}"
    finally:
        engine_module.get_engine.cache_clear()


def test_ac22_database_errors_carry_no_sql_echo_or_docs_link(configured_database):
    """SQLAlchemy's `str(exc)` appends the whole statement and a docs URL.
    `diag.message_primary` is the one line the server actually sent."""
    result = execute_sql("SELECT artist_name FROM album")
    assert "[SQL:" not in result.error
    assert "sqlalche.me" not in result.error
    assert "\n" not in result.error


# --- T7: the gate_violation canary (AC26) ----------------------------------


def test_ac26_gate_violation_category_exists():
    from api.db.execution import CATEGORY_GATE_VIOLATION

    assert CATEGORY_GATE_VIOLATION == "gate_violation"


def test_ac26_read_only_violation_maps_to_gate_violation(caplog):
    """No known input reaches this state — that is the point. It is a canary for
    Gate 2 having passed a write, so it is driven with a synthetic exception.

    If this ever fires in production it means the validator is broken, which is
    why it is the only condition in this module logged at error level.
    """
    import logging

    from sqlalchemy.exc import SQLAlchemyError

    from api.db.execution import (
        CATEGORY_GATE_VIOLATION,
        SQLSTATE_READ_ONLY_TRANSACTION,
        _categorise,
    )

    class _Diag:
        message_primary = "cannot execute DELETE in a read-only transaction"
        message_hint = None

    class _Orig(Exception):
        sqlstate = SQLSTATE_READ_ONLY_TRANSACTION
        diag = _Diag()

    exc = SQLAlchemyError("wrapped")
    exc.orig = _Orig()

    with caplog.at_level(logging.ERROR, logger="querypilot.execution"):
        result = _categorise(exc)

    assert result.category == CATEGORY_GATE_VIOLATION
    assert result.sqlstate == SQLSTATE_READ_ONLY_TRANSACTION
    assert "validator" in result.error
    assert any("GATE 2 FAILURE" in record.message for record in caplog.records)


def test_ac26_is_not_reachable_by_any_known_input(configured_database):
    """The canary must stay silent. Every write form Gate 2 knows about is
    stopped before execution, so none of these should reach SQLSTATE 25006."""
    from api.db.execution import CATEGORY_GATE_VIOLATION, CATEGORY_REJECTED

    for sql in [
        "DELETE FROM track",
        "UPDATE track SET name = 'x'",
        "INSERT INTO track (name) VALUES ('x')",
        "DROP TABLE track",
        "TRUNCATE track",
        "WITH d AS (DELETE FROM track RETURNING *) SELECT * FROM d",
        "SELECT * INTO evil FROM track",
    ]:
        result = execute_sql(sql)
        assert result.category == CATEGORY_REJECTED, (
            f"{sql!r} was not stopped by Gate 2"
        )
        assert result.category != CATEGORY_GATE_VIOLATION


# --- T8: hostile input, determinism, no retries (AC23-AC25) ---------------


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT " + "(" * 200 + "1" + ")" * 200, id="deep-nesting"),
        pytest.param("x" * 50_000, id="over-length"),
        pytest.param("SELECT 1\x00; DROP TABLE t", id="null-byte"),
        pytest.param("'unterminated", id="unterminated-string"),
        pytest.param("garbage nonsense", id="not-a-statement"),
    ],
)
def test_ac23_hostile_input_never_raises(configured_database, sql):
    """These are all stopped by Gate 2 before a connection is opened, which is
    the point: hostile input costs nothing and reaches nothing."""
    result = execute_sql(sql)
    assert isinstance(result, ExecutionResult)
    assert result.ok is False
    assert result.error


def test_ac24_executes_once_and_does_not_retry(monkeypatch):
    """Retry policy belongs to the agent loop (Iteration 4). A hidden retry here
    would corrupt both the latency numbers and the eval accounting, and would be
    invisible to the loop that thinks it is in control."""
    from sqlalchemy.exc import OperationalError

    from api.db import execution

    calls = []

    def _failing_run(sql):
        calls.append(sql)
        raise OperationalError("stmt", {}, Exception("boom"))

    monkeypatch.setattr(execution, "_run", _failing_run)
    result = execution.execute_sql("SELECT 1")

    assert len(calls) == 1, f"executed {len(calls)} times; retries are not this tool's job"
    assert result.ok is False


def test_ac24_no_retry_machinery_in_the_source():
    import inspect as pyi

    from api.db import execution

    source = pyi.getsource(execution).lower()
    for smell in ("for attempt", "while attempt", "max_retries", "retry(", "backoff"):
        assert smell not in source, f"retry machinery crept in: {smell!r}"


def test_ac25_deterministic(configured_database):
    for sql in [
        "SELECT track_id FROM track ORDER BY track_id LIMIT 5",
        "SELECT artist_name FROM album",
        "DROP TABLE track",
        "",
    ]:
        assert execute_sql(sql) == execute_sql(sql)


def test_ac25_result_has_no_time_varying_fields():
    """Resolved Q-D: no `duration_ms`. Telemetry is Iteration 7, and a timing
    field would make AC25 impossible to assert as plain equality."""
    import dataclasses

    names = {f.name for f in dataclasses.fields(ExecutionResult)}
    for timing in ("duration_ms", "elapsed", "started_at", "timestamp"):
        assert timing not in names
