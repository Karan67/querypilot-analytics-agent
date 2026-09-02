"""Acceptance tests for `get_schema()` — specs/001-schema-tool.md.

One test per acceptance criterion, named after it. Task T4 covers AC3, AC4,
AC5, AC6, AC13, AC14, AC15 and AC16; AC7–AC10 arrive with T5, AC1/AC2/AC11 with
T6, and AC17 with T7.

No test in this file knows where the database is — see `conftest.py`.
"""

from __future__ import annotations

import inspect as pyinspect

import pytest

from api.db.introspection import (
    KIND_TABLE,
    KIND_VIEW,
    SchemaIntrospectionError,
    get_schema,
)

#: The 11 Chinook base tables, read from the live catalog on 2026-08-21.
BASE_TABLES = (
    "album",
    "artist",
    "customer",
    "employee",
    "genre",
    "invoice",
    "invoice_line",
    "media_type",
    "playlist",
    "playlist_track",
    "track",
)

#: The view added by `db/init/02_create_views.sql`.
VIEW_NAME = "invoice_totals"

#: Every relation, in the order AC1 requires: alphabetical with kinds
#: interleaved, NOT tables-then-views. `invoice_totals` falls between
#: `invoice_line` and `media_type`, which is the whole point — a grouped
#: implementation would put it last and still look sorted at a glance.
ALL_RELATIONS_IN_ORDER = [
    "album",
    "artist",
    "customer",
    "employee",
    "genre",
    "invoice",
    "invoice_line",
    "invoice_totals",
    "media_type",
    "playlist",
    "playlist_track",
    "track",
]

#: Every foreign key in the schema, read from `pg_constraint` on 2026-08-21.
#: Sets, because AC8 is about *which* keys exist, not the order they arrive in;
#: ordering is asserted separately by AC13.
#:
#: Note: all 11 are single-column. Chinook has no composite foreign key, so the
#: tuple-valued `columns` field is exercised by composite *primary* keys
#: (`playlist_track`) but never by a composite foreign key. That path is
#: designed for but untested — a known coverage gap, not an oversight.
EXPECTED_FOREIGN_KEYS = {
    "album": {(("artist_id",), "artist", ("artist_id",))},
    "artist": set(),
    "customer": {(("support_rep_id",), "employee", ("employee_id",))},
    "employee": {(("reports_to",), "employee", ("employee_id",))},
    "genre": set(),
    "invoice": {(("customer_id",), "customer", ("customer_id",))},
    "invoice_line": {
        (("invoice_id",), "invoice", ("invoice_id",)),
        (("track_id",), "track", ("track_id",)),
    },
    "media_type": set(),
    "playlist": set(),
    "playlist_track": {
        (("playlist_id",), "playlist", ("playlist_id",)),
        (("track_id",), "track", ("track_id",)),
    },
    "track": {
        (("album_id",), "album", ("album_id",)),
        (("genre_id",), "genre", ("genre_id",)),
        (("media_type_id",), "media_type", ("media_type_id",)),
    },
}


def _fk_tuples(table):
    """A relation's foreign keys as comparable plain tuples."""
    return {
        (fk.columns, fk.referred_table, fk.referred_columns)
        for fk in table.foreign_keys
    }


#: `track` in declared ordinal order — the point of AC3 is that this is *not*
#: alphabetical.
TRACK_COLUMNS_IN_ORDER = [
    "track_id",
    "name",
    "album_id",
    "media_type_id",
    "genre_id",
    "composer",
    "milliseconds",
    "bytes",
    "unit_price",
]


def test_returns_every_base_table(relations):
    missing = [name for name in BASE_TABLES if name not in relations]
    assert not missing, f"base tables absent from the schema map: {missing}"


def test_base_tables_are_marked_as_tables(relations):
    wrong = {
        name: relations[name].kind
        for name in BASE_TABLES
        if relations[name].kind != KIND_TABLE
    }
    assert not wrong, f"relations with the wrong kind: {wrong}"


def test_ac3_columns_are_in_ordinal_order(relations):
    """Ordinal order, not alphabetical. Sorting here would be a silent
    regression that no other assertion would catch."""
    assert [c.name for c in relations["track"].columns] == TRACK_COLUMNS_IN_ORDER


def test_ac4_nullability_is_accurate(track_columns):
    assert track_columns["name"].nullable is False
    assert track_columns["album_id"].nullable is True


def test_ac5_types_preserve_length_and_precision(track_columns):
    """`information_schema.data_type` would flatten these to 'character
    varying' and 'numeric', losing exactly the detail the model needs."""
    assert track_columns["name"].type == "VARCHAR(200)"
    assert track_columns["unit_price"].type == "NUMERIC(10, 2)"


def test_ac6_primary_key_is_flagged(track_columns):
    assert track_columns["track_id"].primary_key is True
    assert track_columns["name"].primary_key is False


# --- T5: keys and relationships (AC7-AC10) --------------------------------


def test_ac7_composite_primary_key_flags_every_column(relations):
    """`playlist_track`'s key is (playlist_id, track_id). A tool that reports
    only the first column of a composite key is wrong, and would still pass
    every single-column primary key assertion in this file."""
    flagged = [c.name for c in relations["playlist_track"].columns if c.primary_key]
    assert flagged == ["playlist_id", "track_id"]


def test_ac8_returns_every_foreign_key(relations):
    actual = {name: _fk_tuples(relations[name]) for name in EXPECTED_FOREIGN_KEYS}
    assert actual == EXPECTED_FOREIGN_KEYS


def test_ac8_foreign_key_count_is_eleven(relations):
    total = sum(len(relations[name].foreign_keys) for name in EXPECTED_FOREIGN_KEYS)
    assert total == 11


def test_ac8_album_has_exactly_one_foreign_key(relations):
    assert _fk_tuples(relations["album"]) == {(("artist_id",), "artist", ("artist_id",))}


def test_ac9_invoice_line_returns_both_foreign_keys(relations):
    """Returning only the first key of a multi-key table is a plausible bug
    that a single-key table cannot detect."""
    assert len(relations["invoice_line"].foreign_keys) == 2


def test_ac10_self_referencing_foreign_key_is_present(relations):
    """employee.reports_to -> employee.employee_id. Must survive: neither
    filtered out as a self-join nor recursed into."""
    assert (("reports_to",), "employee", ("employee_id",)) in _fk_tuples(
        relations["employee"]
    )


def test_ac10_self_reference_does_not_duplicate_the_relation(schema):
    """Guards the recursion half of AC10 — a naive follow-the-reference walk
    would yield `employee` more than once."""
    names = [table.name for table in schema.tables]
    assert len(names) == len(set(names))


# --- T6: views (AC1, AC2, AC11) --------------------------------------------


def test_ac1_returns_every_relation_alphabetically_with_kinds_interleaved(schema):
    assert [table.name for table in schema.tables] == ALL_RELATIONS_IN_ORDER


def test_ac2_kinds_are_distinguishable(relations):
    """A consumer must tell a view from a table without a second call — the
    Iteration 2 prompt renderer needs it, and so does the validator."""
    kinds = {name: relations[name].kind for name in ALL_RELATIONS_IN_ORDER}
    assert kinds[VIEW_NAME] == KIND_VIEW
    assert sum(1 for k in kinds.values() if k == KIND_TABLE) == 11
    assert sum(1 for k in kinds.values() if k == KIND_VIEW) == 1


def test_ac11_view_has_no_primary_key(relations):
    """Code that assumes every relation has a primary key breaks here. This is
    the case that would otherwise surface as an IndexError in production."""
    view = relations[VIEW_NAME]
    assert [c.name for c in view.columns if c.primary_key] == []


def test_ac11_view_has_no_foreign_keys(relations):
    assert relations[VIEW_NAME].foreign_keys == ()


def test_ac11_view_still_reports_its_columns(relations):
    """Having no keys must not mean having no content."""
    assert [c.name for c in relations[VIEW_NAME].columns] == [
        "invoice_id",
        "customer_id",
        "invoice_date",
        "line_count",
        "total",
    ]


def test_ac5_computed_view_columns_carry_no_synthesised_precision(relations):
    """`invoice.total` is NUMERIC(10, 2), but the view's computed `total` has no
    declared precision. The tool must report the catalog's bare answer rather
    than inventing one."""
    view_columns = {c.name: c for c in relations[VIEW_NAME].columns}
    assert view_columns["total"].type == "NUMERIC"
    assert view_columns["line_count"].type == "BIGINT"


def test_ac4_view_nullability_is_not_a_constraint(relations):
    """Postgres does not propagate NOT NULL through a view: `invoice_id` derives
    from a NOT NULL primary key yet reports nullable. Documented so the
    Iteration 2 renderer does not present this as something the model can rely
    on."""
    view_columns = {c.name: c for c in relations[VIEW_NAME].columns}
    assert view_columns["invoice_id"].nullable is True


def test_materialized_views_are_excluded(schema):
    """Non-goal for Iteration 1. Chinook has none, so this guards the decision
    rather than the data: if a matview is ever added, it must not appear
    silently via get_view_names()."""
    assert all(table.kind in (KIND_TABLE, KIND_VIEW) for table in schema.tables)


# --- behaviour -------------------------------------------------------------


def test_ac13_is_deterministic():
    """Two calls must be equal. Eval reproducibility depends on stable ordering
    of relations, columns, and foreign keys."""
    assert get_schema() == get_schema()


def test_ac13_relations_are_sorted_by_name(schema):
    names = [table.name for table in schema.tables]
    assert names == sorted(names)


def test_ac14_takes_no_parameters():
    """The property that makes this tool injection-free: no user or model input
    can reach an identifier, because there is no argument to carry it."""
    assert not pyinspect.signature(get_schema).parameters


def test_ac15_excludes_system_catalogs(relations):
    offenders = [
        name
        for name in relations
        if name.startswith(("pg_", "sql_")) or name == "information_schema"
    ]
    assert not offenders, f"system catalog relations leaked in: {offenders}"


def test_ac16_runs_over_a_read_only_connection():
    """Behavioural, not configurational: ask the connection what it can do.

    Asserting on the DSN string would only prove the test knows its own
    fixture. This proves Gate 1 is actually in force on the connection
    `get_schema()` uses.
    """
    from sqlalchemy import text

    from api.db.engine import get_engine

    with get_engine().connect() as conn:
        current_user = conn.execute(text("SELECT current_user")).scalar_one()
        can_insert = conn.execute(
            text("SELECT has_table_privilege(current_user, 'track', 'INSERT')")
        ).scalar_one()

    assert can_insert is False, (
        f"introspection connects as '{current_user}', which can INSERT — "
        f"that is not a read-only connection"
    )


def test_ac16_reads_no_user_rows(schema):
    """The schema map carries structure only. If a value from the data ever
    appeared in it, this tool would have become `sample_rows()`."""
    for table in schema.tables:
        for column in table.columns:
            assert isinstance(column.type, str)
            assert not hasattr(column, "sample_values")


# --- T7: the error path (AC17) ---------------------------------------------


@pytest.fixture
def unreachable_database(monkeypatch):
    """Point the engine at a host that does not resolve, for one test.

    A bad hostname rather than an unroutable IP: DNS fails immediately, where a
    blackholed address would sit in a connect timeout and make the suite slow
    for no extra coverage.

    The lru_cache on get_engine() is cleared on both sides so neither this test
    nor any later one inherits the other's engine.
    """
    from api.db import engine as engine_module

    engine_module.get_engine.cache_clear()
    monkeypatch.setenv(
        engine_module.DATABASE_URL_ENV,
        "postgresql+psycopg://nobody:nobody@querypilot-no-such-host:5432/nothing",
    )
    yield
    engine_module.get_engine.cache_clear()


@pytest.fixture
def missing_database_url(monkeypatch):
    """Remove the DSN entirely, for one test."""
    from api.db import engine as engine_module

    engine_module.get_engine.cache_clear()
    monkeypatch.delenv(engine_module.DATABASE_URL_ENV, raising=False)
    yield
    engine_module.get_engine.cache_clear()


def test_ac17_unreachable_database_raises_schema_error(unreachable_database):
    """Not a bare SQLAlchemyError: callers distinguish "catalog unavailable"
    from "a query failed", and from Iteration 4 the agent reads this message."""
    with pytest.raises(SchemaIntrospectionError):
        get_schema()


def test_ac17_missing_dsn_raises_schema_error(missing_database_url):
    """get_engine() raises RuntimeError here. SchemaIntrospectionError also
    subclasses RuntimeError, so this doubles as a guard that the wrapping
    clause has not started swallowing its own exception type."""
    with pytest.raises(SchemaIntrospectionError):
        get_schema()


def test_ac17_preserves_the_underlying_cause(unreachable_database):
    """The original Postgres text must survive in the traceback. 'could not
    translate host name' is actionable; 'schema error' is not, and the agent
    only ever sees what we pass through."""
    with pytest.raises(SchemaIntrospectionError) as caught:
        get_schema()

    assert caught.value.__cause__ is not None
    assert "querypilot-no-such-host" in str(caught.value)


def test_ac17_empty_schema_is_never_returned_as_success(monkeypatch):
    """An empty result must raise rather than return `Schema(tables=())`.

    This is the one place mocking is correct. Every other test here runs
    against real Postgres because the thing under test is the mapping *of*
    Postgres. Here the thing under test is our own guard clause, and Chinook
    cannot be made to have zero relations without tearing the fixture down.
    """
    from api.db import introspection

    class _EmptyInspector:
        def get_table_names(self, schema=None):
            return []

        def get_view_names(self, schema=None):
            return []

    monkeypatch.setattr(introspection, "sa_inspect", lambda engine: _EmptyInspector())

    with pytest.raises(SchemaIntrospectionError, match="no relations"):
        introspection.get_schema()


def test_ac12_every_reported_relation_is_readable_by_the_role(schema):
    """Anything `get_schema()` reports must actually be selectable.

    A relation the agent can see but cannot read is worse than one it cannot
    see at all: the model writes correct SQL, it passes validation, and it dies
    at execution with permission denied — a failure the agent cannot
    self-correct out of, because nothing about its query is wrong.

    `db/init/03_readonly_role.sh` asserts this at startup, but startup only
    happens on a fresh volume. This asserts it on every run.

    Uses has_table_privilege with the name bound as a parameter rather than
    executing `SELECT ... FROM <name>`. That keeps the test clear of the
    standing rule in specs/000-project.md §4 — no code path executes generated
    SQL without the validator, tests included — and it tests the privilege
    directly rather than inferring it from a query succeeding.
    """
    from sqlalchemy import text

    from api.db.engine import get_engine

    with get_engine().connect() as conn:
        unreadable = [
            table.name
            for table in schema.tables
            if not conn.execute(
                text("SELECT has_table_privilege(current_user, :relation, 'SELECT')"),
                {"relation": table.name},
            ).scalar_one()
        ]

    assert not unreadable, (
        f"reported by get_schema() but not readable by the role: {unreadable}. "
        f"Anything created after the GRANT in db/init/03_readonly_role.sh needs "
        f"to move ahead of it."
    )
