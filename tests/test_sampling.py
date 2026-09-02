"""Acceptance tests for `sample_rows()` — specs/004-sample-rows.md.

T1 covers query construction (AC7–AC9, AC14, AC15). **These need no database**:
construction is the part carrying the security property, and it is provable in
isolation, so it is proven before anything can execute it.
"""

from __future__ import annotations

import sqlglot
import pytest
from sqlglot import exp

from api.db.introspection import KIND_TABLE, KIND_VIEW, Column, Table
from api.db.sampling import (
    MAX_LISTED_RELATIONS,
    SAMPLE_ROW_COUNT,
    _build_sample_query,
    _order_columns,
)
from api.safety.validator import DIALECT, validate_sql

#: Identifiers that would break naive string formatting. None can reach the
#: builder in practice — the allowlist stops them — so these drive the builder
#: directly. A defence only the allowlist protects is a defence no end-to-end
#: test can reach, and it could be replaced with an f-string unnoticed.
HOSTILE_NAMES = [
    pytest.param('track"; DROP TABLE track; --', id="quote-escape"),
    pytest.param("track; DROP TABLE track", id="statement-separator"),
    pytest.param('a"b', id="embedded-quote"),
    pytest.param("my table", id="space"),
    pytest.param("Track", id="mixed-case"),
    pytest.param("pg_stat_activity", id="catalog-name"),
    pytest.param("track--comment", id="comment-marker"),
    pytest.param("track/*x*/", id="block-comment"),
]


def _table(name: str, columns: list[tuple[str, bool]], kind: str = KIND_TABLE) -> Table:
    """Build a synthetic relation. `columns` is (name, is_primary_key)."""
    return Table(
        name=name,
        kind=kind,
        columns=tuple(
            Column(name=column, type="INTEGER", nullable=False, primary_key=is_pk)
            for column, is_pk in columns
        ),
        foreign_keys=(),
    )


# --- AC14, AC15: ordering ---------------------------------------------------


def test_ac14_orders_by_the_primary_key():
    table = _table("track", [("track_id", True), ("name", False)])
    assert _order_columns(table) == ("track_id",)


def test_ac14_composite_key_uses_every_column():
    """`playlist_track`'s key is (playlist_id, track_id). Ordering by only the
    first would leave ties unbroken and the sample non-deterministic."""
    table = _table("playlist_track", [("playlist_id", True), ("track_id", True)])
    assert _order_columns(table) == ("playlist_id", "track_id")


def test_ac15_relation_without_a_primary_key_orders_by_every_column():
    """Views have no primary key (AC11 of `001`), so this is how
    `invoice_totals` stays deterministic — resolved Q-B."""
    view = _table(
        "invoice_totals",
        [("invoice_id", False), ("total", False)],
        kind=KIND_VIEW,
    )
    assert _order_columns(view) == ("invoice_id", "total")


def test_ac14_primary_key_wins_over_the_all_columns_fallback():
    table = _table("t", [("id", True), ("a", False), ("b", False)])
    assert _order_columns(table) == ("id",)


# --- AC7–AC9: construction and containment ---------------------------------


def test_builds_expected_sql():
    sql = _build_sample_query("track", ["track_id"])
    assert sql == 'SELECT * FROM "track" ORDER BY "track_id" LIMIT 3'


def test_composite_ordering_renders_both_columns():
    sql = _build_sample_query("playlist_track", ["playlist_id", "track_id"])
    assert sql == (
        'SELECT * FROM "playlist_track" '
        'ORDER BY "playlist_id", "track_id" LIMIT 3'
    )


def test_limit_matches_the_configured_sample_size():
    sql = _build_sample_query("track", ["track_id"])
    assert f"LIMIT {SAMPLE_ROW_COUNT}" in sql


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_ac9_hostile_identifier_is_contained(name):
    """**The containment proof.**

    Build the query with a hostile relation name, re-parse the result, and
    assert the parser sees exactly one table whose name is that literal string.

    This asserts the *property* — the payload is inside an identifier, not a
    statement — rather than asserting on doubled quote characters, which would
    only prove the quoting function was called. Replacing AST construction with
    an f-string fails this; a syntax-level assertion might not.
    """
    sql = _build_sample_query(name, ["x"])

    parsed = sqlglot.parse(sql, dialect=DIALECT)
    assert len(parsed) == 1, f"payload split into {len(parsed)} statements: {sql}"

    statement = parsed[0]
    assert isinstance(statement, exp.Select)

    tables = [table.name for table in statement.find_all(exp.Table)]
    assert tables == [name], f"parser saw {tables}, not the literal name"


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_ac9_generated_sql_passes_gate_two(name):
    """Whatever the name, the result is still one read-only SELECT. If a hostile
    identifier could make Gate 2 reject the query, construction would be leaking
    structure rather than containing it."""
    ok, reason = validate_sql(_build_sample_query(name, ["x"]))
    assert ok is True, f"{name!r} produced SQL Gate 2 rejects: {reason}"


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_ac9_no_extra_statement_can_be_smuggled(name):
    """The specific attack: a second statement riding inside the identifier."""
    from api.safety.validator import FORBIDDEN_NODES

    statement = sqlglot.parse_one(_build_sample_query(name, ["x"]), dialect=DIALECT)
    forbidden = [n for n in statement.walk() if isinstance(n, FORBIDDEN_NODES)]
    assert forbidden == [], f"forbidden node reached the tree: {forbidden}"


def test_ac7_no_string_formatting_in_the_builder():
    """Structural. The identifier must never touch an f-string, `%`, `.format()`
    or `+`. Asserted on the AST so the docstring explaining the rule does not
    trip it — the same fix `003`'s bypass test needed."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(_build_sample_query))
    for node in ast.walk(tree):
        assert not isinstance(node, ast.JoinedStr), "f-string in the query builder"
        if isinstance(node, ast.BinOp):
            assert not isinstance(node.op, (ast.Mod, ast.Add)), (
                "string concatenation or % formatting in the query builder"
            )
        if isinstance(node, ast.Attribute):
            assert node.attr != "format", ".format() in the query builder"


def test_empty_order_columns_omits_order_by():
    """Defensive: a relation with no columns cannot happen via `get_schema()`,
    but `order_by()` with no arguments would emit invalid SQL."""
    sql = _build_sample_query("t", [])
    assert "ORDER BY" not in sql
    assert validate_sql(sql)[0] is True


def test_construction_needs_no_database():
    """The whole T1 surface is pure. Nothing here opened a connection, which is
    why these tests run with no fixture."""
    import inspect

    from api.db import sampling

    source = inspect.getsource(sampling)
    assert "get_engine" not in source
    assert "MAX_LISTED_RELATIONS" in source and MAX_LISTED_RELATIONS == 25


# --- T2/T3: allowlist, pipeline, determinism -------------------------------

CATALOG_RELATIONS = [
    pytest.param("pg_stat_activity", id="pg_stat_activity"),
    pytest.param("pg_class", id="pg_class"),
    pytest.param("pg_roles", id="pg_roles"),
    pytest.param("pg_tables", id="pg_tables"),
    pytest.param("information_schema.tables", id="information_schema"),
]


def test_ac1_every_relation_from_get_schema_is_samplable(configured_database):
    from api.db.introspection import get_schema
    from api.db.sampling import sample_rows

    for table in get_schema().tables:
        result = sample_rows(table.name)
        assert result.ok is True, f"{table.name}: {result.category} {result.error}"
        assert len(result.columns) == len(table.columns)


def test_ac4_the_view_is_samplable(configured_database):
    from api.db.sampling import sample_rows

    result = sample_rows("invoice_totals")
    assert result.ok is True
    assert result.row_count == SAMPLE_ROW_COUNT


@pytest.mark.parametrize("relation", CATALOG_RELATIONS)
def test_ac3_system_catalogs_are_refused(configured_database, relation):
    """The allowlist is the only thing stopping these — see the companion test
    below, which proves Gate 2 has no objection to them."""
    from api.db.sampling import CATEGORY_UNKNOWN_RELATION, sample_rows

    result = sample_rows(relation)
    assert result.ok is False
    assert result.category == CATEGORY_UNKNOWN_RELATION


@pytest.mark.parametrize("relation", CATALOG_RELATIONS)
def test_gate_two_does_not_object_to_catalog_relations(relation):
    """Pins *why* the allowlist is load-bearing rather than decorative.

    `SELECT * FROM pg_stat_activity LIMIT 3` is one read-only SELECT, so Gate 2
    passes it, and the role can read it — measured. If Gate 2 ever starts
    rejecting these, the allowlist's justification has changed and someone should
    notice deliberately rather than by accident.
    """
    ok, _ = validate_sql(f"SELECT * FROM {relation} LIMIT 3")
    assert ok is True


def test_ac2_comparison_is_case_sensitive(configured_database):
    """PostgreSQL permits `track` and `Track` as distinct relations. Folding
    would let one be reached by asking for the other."""
    from api.db.sampling import CATEGORY_UNKNOWN_RELATION, sample_rows

    assert sample_rows("track").ok is True
    assert sample_rows("Track").category == CATEGORY_UNKNOWN_RELATION
    assert sample_rows("TRACK").category == CATEGORY_UNKNOWN_RELATION


@pytest.mark.parametrize(
    "value",
    [None, 42, [], {"table": "track"}, "", "   ", 'track"; DROP TABLE track; --'],
)
def test_ac17_bad_input_is_refused_not_raised(configured_database, value):
    from api.db.sampling import CATEGORY_UNKNOWN_RELATION, sample_rows

    result = sample_rows(value)
    assert result.ok is False
    assert result.category == CATEGORY_UNKNOWN_RELATION


def test_ac5_rejection_names_the_relation_and_lists_valid_ones(configured_database):
    from api.db.sampling import sample_rows

    result = sample_rows("nope")
    assert "nope" in result.error
    assert "track" in result.error and "invoice_totals" in result.error


def test_ac5_listing_is_capped_with_an_omitted_count(monkeypatch):
    """Chinook's 12 fit under the cap of 25, so the cap is exercised directly."""
    from api.db import sampling

    monkeypatch.setattr(sampling, "MAX_LISTED_RELATIONS", 3)
    result = sampling._unknown("nope", tuple(f"t{i}" for i in range(10)))
    assert "t0, t1, t2" in result.error
    assert "and 7 more" in result.error


def test_ac10_ac11_module_opens_no_connection_of_its_own():
    """Everything reaches the database through `execute_sql`, so Gate 2, the
    read-only transaction, the timeout, streaming and the row cap are inherited
    rather than reimplemented."""
    import inspect

    from api.db import sampling

    source = inspect.getsource(sampling)
    assert "get_engine" not in source
    assert "sqlalchemy" not in source.lower()
    assert "execute_sql(query)" in source


def test_ac12_returns_an_execution_result(configured_database):
    from api.db.execution import ExecutionResult
    from api.db.sampling import sample_rows

    assert isinstance(sample_rows("track"), ExecutionResult)


def test_ac16_returns_at_most_the_sample_size(configured_database):
    from api.db.sampling import sample_rows

    assert sample_rows("track").row_count == SAMPLE_ROW_COUNT
    assert sample_rows("genre").row_count == SAMPLE_ROW_COUNT


def test_ac13_repeated_calls_return_identical_rows(configured_database):
    """Iteration 5 puts these values into the prompt. If they varied between
    runs, two eval runs would differ for reasons unrelated to the change under
    test, and an accuracy delta would stop being attributable."""
    from api.db.sampling import sample_rows

    for relation in ("track", "playlist_track", "invoice_totals"):
        results = {sample_rows(relation).rows for _ in range(3)}
        assert len(results) == 1, f"{relation} sampled non-deterministically"


def test_ac18_empty_relation_returns_columns_and_no_rows(configured_database):
    """Chinook has no empty relation, so this drives the pipeline directly with
    a query that cannot match — the same shape `sample_rows` produces."""
    from api.db.execution import execute_sql

    result = execute_sql(
        'SELECT * FROM "track" WHERE 1 = 0 ORDER BY "track_id" LIMIT 3'
    )
    assert result.ok is True
    assert result.rows == () and len(result.columns) == 9


def test_get_schema_failure_becomes_a_connection_error(monkeypatch):
    """Without the catalog there is no allowlist, so there is no safe way to
    continue — but AC17 says return, never raise."""
    from api.db import sampling
    from api.db.execution import CATEGORY_CONNECTION_ERROR
    from api.db.introspection import SchemaIntrospectionError

    def _boom():
        raise SchemaIntrospectionError("catalog unavailable")

    monkeypatch.setattr(sampling, "get_schema", _boom)
    result = sampling.sample_rows("track")
    assert result.ok is False
    assert result.category == CATEGORY_CONNECTION_ERROR


def test_ac6_allowlist_runs_before_any_sql_is_built(monkeypatch, configured_database):
    """Ordering, asserted rather than assumed.

    There must be no moment where an unvalidated identifier exists inside a
    query — not even one that is discarded. "Build then check" would be one edit
    away from "build then execute".
    """
    from api.db import sampling

    def _explode(*args, **kwargs):
        raise AssertionError("a query was built for an unknown relation")

    monkeypatch.setattr(sampling, "_build_sample_query", _explode)

    result = sampling.sample_rows("pg_stat_activity")
    assert result.category == sampling.CATEGORY_UNKNOWN_RELATION


def test_ac8_quoting_is_unconditional():
    """Every identifier is quoted, including lowercase names that need no
    quoting. SQLAlchemy's preparer quotes only when it judges it necessary —
    measured, it renders `track` and `pg_shadow` bare — which makes output vary
    by name. Uniform output is easier to test and to read in a log.
    """
    assert '"track"' in _build_sample_query("track", ["track_id"])
    assert '"track_id"' in _build_sample_query("track", ["track_id"])
    assert '"genre"' in _build_sample_query("genre", ["genre_id"])
    # no bare identifier survives
    assert "FROM track " not in _build_sample_query("track", ["track_id"])
