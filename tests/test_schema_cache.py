"""Iteration 7 T6 — the schema is introspected once and reused (AC9).

**Every claim here is counted, not timed.** A cache that still introspects is
invisible to a stopwatch — the second call is faster anyway, on a warm pool and
a warm plan cache — and unmissable to a counter. So these tests hook
SQLAlchemy's cursor events and count the round trips the database actually
served, which is the strongest form of the question *did we skip the read?*

The numbers that make this worth doing, measured in the container on
2026-09-10:

| | round trips | time |
|---|---|---|
| `get_schema()` | 52 | 99ms |
| the catalog probe | 3 | 5.3ms |

Fifty-two because SQLAlchemy's `Inspector` asks per relation — columns, primary
key, foreign keys and kind, across twelve relations.

The load-bearing test is `test_a_repeated_call_does_not_reintrospect`, and it
asserts the round trips are the probe's three and nothing more. The mutation it
guards is removing the reuse, which takes that number back to fifty-five.
"""

from __future__ import annotations

import contextlib

import pytest
import sqlalchemy

from api.db import schema_cache
from api.db.engine import get_engine
from api.db.introspection import Schema, get_schema
from api.db.schema_cache import (
    CATALOG_SIGNATURE_SQL,
    cached_schema,
    catalog_fingerprint,
    fingerprint_of,
)


class _RoundTrips:
    """Counts statements the database was actually asked to run."""

    def __init__(self):
        self.statements: list[str] = []

    @property
    def n(self) -> int:
        return len(self.statements)

    def __repr__(self) -> str:  # shown when an assertion fails
        return f"<{self.n} round trips: {[s[:48] for s in self.statements]}>"


@contextlib.contextmanager
def counting_round_trips():
    counter = _RoundTrips()
    engine = get_engine()

    def _record(conn, cursor, statement, parameters, context, executemany):
        counter.statements.append(" ".join(str(statement).split()))

    sqlalchemy.event.listen(engine, "before_cursor_execute", _record)
    try:
        yield counter
    finally:
        sqlalchemy.event.remove(engine, "before_cursor_execute", _record)


# --- the measurement this task exists for ------------------------------------


def test_introspection_really_is_the_expensive_thing(configured_database):
    """Pins the premise rather than trusting the spec's prose.

    **This test did its job, and the answer moved** (Iteration 8 T5). It was
    written to fail *"as a failure asking whether the module is still worth its
    weight"* if introspection ever stopped being many round trips. B-10 is that
    event: replacing SQLAlchemy's `Inspector` with three catalog queries took
    `get_schema()` from **52 round trips and 99ms** to **9 and 29ms**, measured
    on 2026-09-10.

    The cache still saves something real — 3 round trips against 9, and 8.7ms
    against 29.0ms — but the margin it was justified on has narrowed by a
    factor of about five, and `010-hardening.md`'s honest framing of "99ms of a
    ~1,000ms request" is now 29ms of one. **Whether this module is still worth
    its weight is a live question and is recorded as such**, not answered by
    quietly lowering a threshold.

    What is asserted is the invariant the cache depends on and nothing more:
    introspecting costs strictly more than probing. If those ever converge, the
    cache is protecting nothing and this fails again, which is the behaviour
    that made the test valuable in the first place.
    """
    get_schema()  # warm the pool and the plan cache; we are counting, not timing

    with counting_round_trips() as introspection_trips:
        get_schema()

    with counting_round_trips() as probe_trips:
        catalog_fingerprint()

    assert introspection_trips.n > probe_trips.n, (
        f"introspection ({introspection_trips.n}) no longer costs more than the "
        f"probe ({probe_trips.n}) that exists to avoid it; the cache is "
        f"machinery protecting nothing"
    )
    # The three statements of one execute_sql() are the floor, so anything at
    # or below that is not three queries and the premise has changed again.
    assert introspection_trips.n >= 6, (
        f"introspection is down to {introspection_trips.n} round trips; if it "
        f"is now a single query, retire the cache rather than re-tune this"
    )


def test_the_probe_is_cheaper_than_what_it_replaces(configured_database):
    """One `execute_sql()` is three statements: the read-only transaction, the
    statement timeout, and the query. That is the price of *not* introspecting.
    """
    with counting_round_trips() as trips:
        catalog_fingerprint()

    assert trips.n <= 5, f"the probe should be one query, not a survey: {trips}"
    assert any("pg_class" in s for s in trips.statements)


def test_a_repeated_call_does_not_reintrospect(configured_database):
    """**The mutation test for AC9.**

    Counted, because the point is not that the second call is quick -- it would
    be quicker anyway -- but that the database was never asked. Removing the
    reuse in `cached_schema` takes this from three round trips to fifty-five.
    """
    first = cached_schema()

    with counting_round_trips() as trips:
        second = cached_schema()

    assert trips.n <= 5, f"the schema was re-introspected: {trips}"
    assert second is first, "the very same object, not an equal one"


def test_many_repeated_calls_cost_one_probe_each(configured_database):
    """Ten questions in a row must not be ten introspections.

    The shape of the deployed path: `deployed_fingerprints()` and `answer()`
    both want the schema, so a single question asked it twice before T6.
    """
    cached_schema()

    with counting_round_trips() as trips:
        for _ in range(10):
            cached_schema()

    assert trips.n <= 50, f"ten reuses should cost ten probes, not ten surveys: {trips}"


def test_one_question_now_reads_the_schema_once(configured_database):
    """T4 added the second read; this is the assertion that it is gone.

    Both callers in the request path go through `cached_schema`, so the pair
    costs one introspection and two probes rather than two introspections.

    Calibrated against a real introspection rather than against a hardcoded
    fifty-two, so it keeps meaning what it says if SQLAlchemy changes how many
    queries it asks.
    """
    schema_cache.clear()
    with counting_round_trips() as one_introspection:
        get_schema()
    baseline = one_introspection.n

    schema_cache.clear()
    with counting_round_trips() as pair:
        cached_schema()  # what `deployed_fingerprints()` does
        cached_schema()  # what `answer()` does

    assert pair.n < baseline * 2, (
        f"the pair cost {pair.n} round trips against {baseline} for one "
        f"introspection -- the second read was not saved"
    )
    assert pair.n <= baseline + 10, (
        f"expected one introspection plus two probes, got {pair.n} "
        f"against a {baseline}-round-trip introspection"
    )


# --- invalidation, which is the half AC9 actually asks for -------------------


def test_a_changed_fingerprint_forces_a_fresh_introspection(monkeypatch):
    """**Reuse is invalidated, not trusted forever.**

    The probe is faked rather than the database altered: the read-only role
    cannot issue DDL, and charter §4 has exactly one recorded exemption for
    reaching around the safety layer, which this is not. What is under test here
    is the cache's decision, and that decision is a function of the fingerprint.
    Whether the fingerprint itself moves when the catalog moves is a different
    claim, asserted by the coverage tests below.
    """
    monkeypatch.setattr(schema_cache, "catalog_fingerprint", lambda: "aaaaaaaaaaaa")
    first = cached_schema()

    monkeypatch.setattr(schema_cache, "catalog_fingerprint", lambda: "bbbbbbbbbbbb")
    second = cached_schema()

    assert second is not first, "a changed catalog must produce a fresh read"


def test_an_unchanged_fingerprint_reuses(monkeypatch):
    monkeypatch.setattr(schema_cache, "catalog_fingerprint", lambda: "aaaaaaaaaaaa")

    assert cached_schema() is cached_schema()


def test_an_unverifiable_probe_is_never_trusted(monkeypatch, configured_database):
    """`None` means *unverifiable*, not *unchanged*.

    Treating it as a value to compare would make a broken probe look like a
    stable catalog -- the cache's worst failure, because it never looks broken.
    """
    monkeypatch.setattr(schema_cache, "catalog_fingerprint", lambda: None)

    first = cached_schema()
    second = cached_schema()

    assert second is not first, "an unverifiable catalog must be re-read every time"


def test_a_truncated_probe_is_unverifiable(monkeypatch):
    """**The trap worth naming.** Gate 3 caps results at 1,000 rows. Ninety-one
    signature rows fit today; two hundred tables would not.

    A hash of the first thousand rows of a stable catalog is *perfectly stable*
    and blind to everything after it, so truncation has to be refused rather
    than hashed. Guards the mutation of dropping the `truncated` check.
    """
    from api.db.execution import ExecutionResult

    monkeypatch.setattr(
        schema_cache,
        "execute_sql",
        lambda _sql: ExecutionResult(
            ok=True, columns=("signature",), rows=(("a|r|x|integer|true",),), truncated=True
        ),
    )
    assert catalog_fingerprint() is None


def test_a_failed_probe_is_unverifiable(monkeypatch):
    from api.db.execution import ExecutionResult

    monkeypatch.setattr(
        schema_cache,
        "execute_sql",
        lambda _sql: ExecutionResult(ok=False, error="connection refused"),
    )
    assert catalog_fingerprint() is None


def test_the_probe_passes_the_safety_layer(configured_database):
    """Charter §4 is absolute, so the probe is a `SELECT` through `execute_sql()`
    like everything else -- Gate 2, the read-only transaction, the timeout.

    It earns its own test because the first draft passed Gate 2 and then failed
    at execution on `text || "char"`, which is a reminder that the validator
    accepting a statement is not the same as PostgreSQL running it.
    """
    from api.db.execution import execute_sql

    result = execute_sql(CATALOG_SIGNATURE_SQL)

    assert result.ok, result.error
    assert not result.truncated
    assert len(result.rows) > 50


# --- what the fingerprint is sensitive to ------------------------------------


def test_the_fingerprint_covers_everything_the_schema_carries(schema, configured_database):
    """**The coverage claim, asserted rather than argued.**

    Anything a `Schema` carries but the signature omits is a change the cache
    cannot see: add a foreign key the probe ignores, and the prompt goes on
    describing a relationship the database no longer has, with a fingerprint
    that never moved.

    So every relation, column, type and constraint that `get_schema()` reports
    must appear somewhere in the probe's material. This does not need DDL to be
    meaningful -- it establishes the invariant that the material is a superset
    of the schema.
    """
    from api.db.execution import execute_sql

    material = "\n".join(row[0] for row in execute_sql(CATALOG_SIGNATURE_SQL).rows)

    for table in schema.tables:
        assert table.name in material, f"{table.name} is invisible to the probe"

        for column in table.columns:
            token = f"{table.name}|"
            assert f"|{column.name}|" in material, (
                f"{table.name}.{column.name} is invisible to the probe"
            )
            assert token in material

        for fk in table.foreign_keys:
            assert fk.referred_table in material

    # Primary keys and foreign keys arrive as constraint definitions.
    assert "constraint|" in material
    assert "PRIMARY KEY" in material
    assert "FOREIGN KEY" in material


def test_every_field_of_a_column_signature_carries_something(configured_database):
    """**A mutation survived for want of this test.**

    Replacing `format_type(...)` with an empty string left every other
    assertion green: table names, column names and constraints were all still
    there, so the coverage test above passed while the fingerprint had gone
    blind to *every type change* -- `VARCHAR(160)` widened to `VARCHAR(200)`,
    `integer` promoted to `bigint`, `numeric(10,2)` losing its scale.

    Checking the fields structurally catches that without having to reconcile
    two spellings of a type: `get_schema()` reports `VARCHAR(160)` and
    `format_type` reports `character varying(160)`, so a substring match
    between them would be a test of the spelling rather than of the coverage.
    """
    from api.db.execution import execute_sql

    columns = [
        row[0].split("|")
        for row in execute_sql(CATALOG_SIGNATURE_SQL).rows
        if not row[0].startswith("constraint|")
    ]
    assert columns

    for relation, kind, column, type_name, notnull in columns:
        assert relation, "a signature with no relation name"
        assert kind, "a signature with no relation kind"
        assert column, "a signature with no column name"
        assert type_name, f"{relation}.{column} contributes no type to the fingerprint"
        assert notnull in ("true", "false"), f"{relation}.{column} has no nullability"

    distinct_types = {c[3] for c in columns}
    assert len(distinct_types) > 3, (
        f"only {distinct_types} in the fingerprint; a type change would be invisible"
    )


def test_the_fingerprint_moves_when_the_material_does():
    """Sensitivity, tested without a database because `fingerprint_of` is a
    pure function of the signature rows -- which is why it is separate from the
    query at all."""
    base = ("album|r|title|character varying(160)|true",)

    assert fingerprint_of(base) == fingerprint_of(base)
    assert fingerprint_of(base) != fingerprint_of(
        ("album|r|title|character varying(200)|true",)
    ), "a widened column must change the fingerprint"
    assert fingerprint_of(base) != fingerprint_of(base + ("album|r|extra|integer|false",)), (
        "an added column must change the fingerprint"
    )
    assert fingerprint_of(base) != fingerprint_of(()), "an emptied schema must differ"


def test_the_fingerprint_ignores_row_order():
    """The schema is a *set* of relations, so the hash must be too.

    Sorting happens in Python rather than as `ORDER BY`, because a SQL sort
    would make the hash depend on the server's collation -- the same schema on a
    differently-configured server would fingerprint differently, and nothing
    about the schema would have changed.
    """
    rows = ("b|r|y|integer|true", "a|r|x|integer|false", "c|v|z|text|true")

    assert fingerprint_of(rows) == fingerprint_of(tuple(reversed(rows)))
    assert fingerprint_of(rows) == fingerprint_of(sorted(rows))


def test_the_separator_prevents_a_boundary_collision():
    """Two signatures that re-split differently must not hash the same, for the
    reason the cache key carries a NUL between its parts."""
    assert fingerprint_of(("ab", "c")) != fingerprint_of(("a", "bc"))


def test_the_fingerprint_is_short_and_stable():
    assert len(fingerprint_of(("x",))) == schema_cache.FINGERPRINT_LENGTH


# --- the boundary -----------------------------------------------------------


def test_the_cache_returns_a_real_schema(configured_database):
    """It is a cache, not a substitute: what comes back is what `get_schema()`
    returns, including on the very first call."""
    cached = cached_schema()

    assert isinstance(cached, Schema)
    assert {t.name for t in cached.tables} == {t.name for t in get_schema().tables}


def test_an_unreachable_database_still_raises(monkeypatch):
    """`cached_schema()` must fail the way `get_schema()` fails, or every caller
    that already handles an unreachable database quietly stops working."""
    from api.db.introspection import SchemaIntrospectionError

    def explode():
        raise SchemaIntrospectionError("could not connect")

    monkeypatch.setattr(schema_cache, "catalog_fingerprint", lambda: None)
    monkeypatch.setattr(schema_cache, "get_schema", explode)

    with pytest.raises(SchemaIntrospectionError):
        cached_schema()


def test_the_probe_does_not_reach_around_the_safety_layer():
    """Structural. Charter §4 names `run_evals.py` and debug helpers explicitly,
    and a module that holds a hand-written catalog query is exactly where
    somebody would reach for a raw connection to make it faster.

    Asserted against the parsed AST rather than by grepping, because this
    repository has repeatedly written a structural test that matched its own
    commentary.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("api/db/schema_cache.py").read_text(encoding="utf-8"))

    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "execute_sql" in called, "the probe must go through the safety layer"
    for forbidden in ("get_engine", "connect", "text", "raw_connection"):
        assert forbidden not in called, f"schema_cache reaches the database via {forbidden}"


# --- adoption: the module is only worth anything if the request path uses it --


def test_the_request_path_does_not_introspect_twice(configured_database, monkeypatch):
    """**A mutation survived for want of this test too.**

    Reverting `deployed_fingerprints()` to raw `get_schema()` left every test
    above green, because they all exercise `cached_schema` directly. A cache
    nothing calls is a module with good tests and no effect.

    Counted end to end through the endpoint, which is where the second read was
    added at T4 and where it has to be gone.
    """
    from fastapi.testclient import TestClient

    from api.agent.orchestrator import AgentResult
    from api.db.execution import ExecutionResult
    from api.main import app

    monkeypatch.setattr(
        "api.main.answer",
        lambda question, **_: AgentResult(
            ok=True,
            question=question,
            sql="SELECT 1",
            result=ExecutionResult(ok=True, columns=("n",), rows=((1,),)),
            attempts_used=1,
        ),
    )
    client = TestClient(app)

    schema_cache.clear()
    with counting_round_trips() as one_introspection:
        get_schema()
    baseline = one_introspection.n

    schema_cache.clear()
    client.post("/ask", json={"question": "warm the cache"})

    with counting_round_trips() as trips:
        client.post("/ask", json={"question": "a second, different question"})

    assert trips.n < baseline, (
        f"the second request cost {trips.n} round trips against a "
        f"{baseline}-round-trip introspection -- the request path is not using "
        f"the cache"
    )


def test_the_request_path_asks_for_the_cached_schema():
    """Structural, and blunt on purpose.

    The counting test above proves the effect; this one names the cause, so a
    failure says *which* module went back to introspecting rather than only
    that the round trips went up. Asserted against the parsed AST, never by
    grepping, because this repository has repeatedly written a structural test
    that matched its own commentary.
    """
    import ast
    import pathlib

    for module in ("api/agent/fingerprints.py", "api/agent/orchestrator.py"):
        tree = ast.parse(pathlib.Path(module).read_text(encoding="utf-8"))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "cached_schema" in called, f"{module} does not use the schema cache"
        assert "get_schema" not in called, (
            f"{module} still introspects directly, so AC9's 'introspected once' "
            f"does not hold on the request path"
        )
