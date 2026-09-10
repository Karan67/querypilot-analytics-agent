"""B-10: the same bytes, through Gate 2 (Iteration 8 T5, `011-ship.md` AC8/AC9).

`api/db/introspection.py` reached the database around Gate 2 from Iteration 1
until now, through SQLAlchemy's `Inspector`, at 52 statements a call. Charter §4
says every database read goes through `execute_sql()` and records exactly one
exemption; this was not that exemption, it was a hole.

**AC9 is a gate, not a goal.** The rendered schema is hashed into the prompt
fingerprint that every `EVALS.md` entry carries, so a replacement that renders
*equivalently* rather than *identically* retires eight iterations of comparable
measurement. The plan's §4.3 said in advance that a mismatch means the task
reverts and B-10 returns to the board — not that the assertion gets widened.

## How byte identity is proved without running the code being removed

The comparison cannot be made live. Rendering "both ways" means calling the
`Inspector`, and the `Inspector` is the Gate 2 bypass being removed — a test
that calls it is a test that bypasses the safety layer, which charter §4 forbids
for tests explicitly.

So the comparison happened **once**, during implementation, and its output is
frozen (resolved D-3):

1. `tests/fixtures/rendered_schema_{ddl,compact,compact-abbrev}.txt` — the three
   renderings, produced by the `Inspector` immediately before it was deleted.
   All three, not the two the plan named: freezing the third cost nothing and
   `compact-abbrev` is the only one that exercises the type *classifier*.
2. `tests/fixtures/introspected_schema.json` — the structural map behind them,
   so a mismatch can be read as "this column's type changed" instead of "the
   bytes differ somewhere".
3. The fingerprints, checked to reproduce — two independent confirmations
   before the old path went.

The residual risk D-3 names is that the fixtures are wrong at the moment they
are written. That is what the fingerprints answer: `0d280c367c5e` and
`91036a089282` were recorded in `EVALS.md` across earlier iterations, by code
that no longer exists, and they still reproduce.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from api.agent.fingerprints import live_schema_fingerprint, loop_prompt_fingerprint
from api.agent.prompts import (
    SCHEMA_COMPACT,
    SCHEMA_COMPACT_ABBREV,
    SCHEMA_DDL,
    SCHEMA_RENDERINGS,
    render_schema,
)
from api.db.type_names import BASE_SPELLINGS, split_catalog_type, sqlalchemy_spelling

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"

#: Recorded in `EVALS.md` by earlier iterations, under the `Inspector`.
RECORDED_PROMPT_FINGERPRINTS = {False: "0d280c367c5e", True: "91036a089282"}

#: Captured from the `Inspector` at T5, immediately before it was replaced.
FROZEN_SCHEMA_FINGERPRINTS = {
    SCHEMA_DDL: "6cac588c545e",
    SCHEMA_COMPACT: "c0418e1ed384",
    SCHEMA_COMPACT_ABBREV: "b14e98c4ab01",
}


# ---------------------------------------------------------------------------
# AC9: the bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rendering", SCHEMA_RENDERINGS)
def test_ac9_the_rendered_schema_is_byte_identical(schema, rendering):
    """**The gate.** If this fails, §4.3 says revert; it does not say widen."""
    expected = (FIXTURES / f"rendered_schema_{rendering}.txt").read_bytes()
    actual = render_schema(schema, rendering).encode("utf-8")

    assert actual == expected, (
        f"the {rendering} rendering changed. The prompt fingerprint is computed "
        f"over this text, so EVALS.md's entries stop being comparable. Plan "
        f"§4.3: revert the task and return B-10 to the board."
    )


def test_every_rendering_has_a_frozen_fixture():
    """The vacuity guard on the gate above.

    `test_ac9_...` is parametrized over `SCHEMA_RENDERINGS`, so a rendering
    added later would silently need a fixture — and `read_bytes()` on a missing
    file raises, which is a red test rather than a green one. This exists for
    the subtler case: someone deleting a fixture *and* a rendering together, or
    the constant shrinking, leaving a gate that guards less than it claims.
    """
    assert len(SCHEMA_RENDERINGS) == 3, (
        f"renderings changed to {SCHEMA_RENDERINGS}; freeze a fixture for any "
        f"new one from a known-good schema before trusting this file"
    )
    for rendering in SCHEMA_RENDERINGS:
        path = FIXTURES / f"rendered_schema_{rendering}.txt"
        assert path.exists(), f"no frozen rendering for {rendering}"
        assert path.stat().st_size > 500, f"{path.name} is suspiciously small"


def test_ac9_the_structural_map_is_unchanged_field_by_field(schema):
    """The same claim at field granularity, so a failure names the column.

    Byte identity is the criterion; this is the diagnostic. A one-character
    type change fails both, and only this one says which column moved.
    """
    frozen = json.loads((FIXTURES / "introspected_schema.json").read_text("utf-8"))
    current = [
        {
            "name": table.name,
            "kind": table.kind,
            "columns": [
                {
                    "name": column.name,
                    "type": column.type,
                    "nullable": column.nullable,
                    "primary_key": column.primary_key,
                }
                for column in table.columns
            ],
            "foreign_keys": [
                {
                    "columns": list(fk.columns),
                    "referred_table": fk.referred_table,
                    "referred_columns": list(fk.referred_columns),
                }
                for fk in table.foreign_keys
            ],
        }
        for table in schema.tables
    ]

    assert current == frozen


@pytest.mark.parametrize("rendering,expected", sorted(FROZEN_SCHEMA_FINGERPRINTS.items()))
def test_the_schema_fingerprints_reproduce(schema, rendering, expected):
    assert live_schema_fingerprint(schema, rendering) == expected


#: What `EVALS.md` records in its entries, via
#: `evals.run_evals.schema_fingerprint()` — a **different recipe** from
#: `live_schema_fingerprint` above, and the one the benchmark record carries.
#:
#: Found while writing this file: the two functions produce different values for
#: the same rendering (`e0b31c713530` versus `c0418e1ed384` for `compact`), and
#: only the first appears in `EVALS.md`. Pinning the wrong one would have been a
#: gate that looked right and guarded nothing the record depends on. Nothing
#: pinned these three before T5.
RECORDED_EVAL_SCHEMA_FINGERPRINTS = {
    SCHEMA_DDL: "f289a58e7ef7",
    SCHEMA_COMPACT: "e0b31c713530",
    SCHEMA_COMPACT_ABBREV: "be7d49123608",
}


@pytest.mark.parametrize(
    "rendering,expected", sorted(RECORDED_EVAL_SCHEMA_FINGERPRINTS.items())
)
def test_the_fingerprints_evals_md_records_reproduce(
    configured_database, rendering, expected
):
    """**The values that decide whether the benchmark record survives B-10.**

    `EVALS.md` entry after entry carries `| Schema fingerprint | e0b31c713530 |`
    for the adopted `compact` rendering, and `008-prompt-tuning-plan.md` quotes
    it three times. That hash is computed from the rendered schema, which is
    built from `get_schema()`, which this task rewrote — so this is the
    assertion Q-C's ruling was actually about when it chose a byte-identical
    translation layer over re-baselining.
    """
    from evals.run_evals import schema_fingerprint

    assert schema_fingerprint(rendering) == expected


@pytest.mark.parametrize(
    "glossary,expected", sorted(RECORDED_PROMPT_FINGERPRINTS.items())
)
def test_the_recorded_prompt_fingerprints_reproduce(glossary, expected):
    """**The independent confirmation D-3 relied on.**

    These two values are in `EVALS.md`, written by earlier iterations, by the
    code path this task deleted. They are the reason the frozen fixtures can be
    trusted: if the fixtures had captured a wrong schema, these would not
    reproduce.
    """
    assert loop_prompt_fingerprint(glossary=glossary) == expected


# ---------------------------------------------------------------------------
# The translator, on cases Chinook has and cases it does not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "catalog_type,expected",
    [
        # The five measured families (section 2.4).
        ("bigint", "BIGINT"),
        ("integer", "INTEGER"),
        ("numeric", "NUMERIC"),
        ("numeric(10,2)", "NUMERIC(10, 2)"),
        ("character varying(200)", "VARCHAR(200)"),
        ("character varying(40)", "VARCHAR(40)"),
        ("timestamp without time zone", "TIMESTAMP"),
    ],
)
def test_the_measured_families_translate(catalog_type, expected):
    assert sqlalchemy_spelling(catalog_type) == expected


def test_the_space_after_the_comma_is_the_whole_point():
    """`numeric(10,2)` -> `NUMERIC(10, 2)`, with the space.

    Called out in its own test because it is one character and it decides
    whether the prompt fingerprint holds. Postgres emits no space; SQLAlchemy's
    type compiler joins modifiers with `", "`. Three money columns carry this
    scale, and `unit_price NUMERIC(10, 2)` versus `NUMERIC(10,2)` is a different
    prompt and a different hash.
    """
    assert sqlalchemy_spelling("numeric(10,2)") == "NUMERIC(10, 2)"
    assert sqlalchemy_spelling("numeric(10,2)") != "NUMERIC(10,2)"


def test_a_modifier_in_the_middle_is_not_mangled():
    """`timestamp(3) without time zone` — Postgres puts the modifier inside.

    Chinook has no such column, so this is reasoned about rather than observed,
    and it is the case a trailing-anchored regex gets wrong. A pattern matching
    only at the end would leave the base as the whole string, miss
    `BASE_SPELLINGS`, and fall through to the uppercase path — producing
    `TIMESTAMP(3) WITHOUT TIME ZONE`, which looks plausible and is not what
    SQLAlchemy renders.
    """
    assert split_catalog_type("timestamp(3) without time zone") == (
        "timestamp without time zone",
        ("3",),
    )
    assert sqlalchemy_spelling("timestamp(3) without time zone") == "TIMESTAMP(3)"


def test_an_unmapped_type_degrades_rather_than_raising():
    """A schema that fails to render is a schema the agent cannot see.

    Refusing an unrecognised type would take the whole database down over one
    column, so the fallback uppercases. It is right for the single-word types a
    warehouse actually brings, and `api/db/type_names.py` states plainly that it
    is unverified — including the one known divergence, `character(N)`, which
    SQLAlchemy renders as `CHAR(N)`.
    """
    assert sqlalchemy_spelling("text") == "TEXT"
    assert sqlalchemy_spelling("boolean") == "BOOLEAN"
    assert sqlalchemy_spelling("jsonb") == "JSONB"
    assert sqlalchemy_spelling("double precision") == "DOUBLE PRECISION"

    # Named so the gap is a fact in the suite rather than only a comment.
    assert sqlalchemy_spelling("character(10)") == "CHARACTER(10)", (
        "documented divergence: SQLAlchemy renders CHAR(10). Chinook has no "
        "CHAR column, so this is recorded rather than fixed on a guess."
    )


def test_the_mapping_holds_only_what_was_measured():
    """Five families, and a test that says so.

    Resolved D-7 confines T5 to the type-family mapping. An entry added later
    without a measurement behind it would be indistinguishable from these,
    which is how a table of facts becomes a table of guesses — so growing the
    map is made a deliberate act that fails this test first.
    """
    assert set(BASE_SPELLINGS) == {
        "bigint",
        "integer",
        "numeric",
        "character varying",
        "timestamp without time zone",
    }


# ---------------------------------------------------------------------------
# The two things Chinook structurally cannot prove
# ---------------------------------------------------------------------------


def test_two_keys_onto_the_same_table_stay_two_keys():
    """**The bug the frozen fixtures cannot catch, so it is caught here.**

    The first draft of `_group_foreign_keys` grouped rows by *referred table*.
    That is wrong: two separate single-column foreign keys onto the same table
    are two constraints, and grouping them by target fuses them into one bogus
    composite key — `(a, b) -> (x, y)` where no such constraint exists, which
    would then be rendered into the prompt.

    Chinook passes either way. `track` has three keys onto three *different*
    tables and `employee` has one self-reference, so no relation here has two
    keys onto one target — which is exactly why byte identity against the
    fixtures would have signed off on the defect. Constructed rows are the only
    way to see it.
    """
    from api.db.introspection import _group_foreign_keys

    rows = [
        ("orders_billing_fkey", 1, "billing_address_id", "address", "address_id"),
        ("orders_shipping_fkey", 1, "shipping_address_id", "address", "address_id"),
    ]

    keys = _group_foreign_keys(rows)

    assert len(keys) == 2, f"two constraints were fused into {keys}"
    assert {key.columns for key in keys} == {
        ("billing_address_id",),
        ("shipping_address_id",),
    }
    for key in keys:
        assert key.referred_table == "address"
        assert key.referred_columns == ("address_id",)


def test_a_composite_key_keeps_the_constraints_own_column_order():
    """`(a, b) -> (x, y)` must not become `(a, b) -> (y, x)`.

    The pairing comes from `unnest(conkey, confkey) WITH ORDINALITY`, and the
    ordinal is what carries it. Rows are handed over deliberately out of order,
    because the catalog does not promise one and a version that trusted arrival
    order would pass against a database that happened to supply it.
    """
    from api.db.introspection import _group_foreign_keys

    rows = [
        ("line_item_order_fkey", 2, "line_no", "order_line", "line_no"),
        ("line_item_order_fkey", 1, "order_id", "order_line", "order_id"),
    ]

    keys = _group_foreign_keys(rows)

    assert len(keys) == 1
    assert keys[0].columns == ("order_id", "line_no")
    assert keys[0].referred_columns == ("order_id", "line_no")


def test_columns_are_ordered_by_attnum_and_not_by_arrival(monkeypatch):
    """**A surviving mutation, and the oldest shape in this repository.**

    Emitting `columns[name].values()` — dictionary insertion order — instead of
    sorting by `attnum` left every test green, including byte identity against
    the frozen fixtures. Postgres simply *happens* to return these rows in
    `attnum` order for this query, so the database's incidental behaviour stood
    in for the code under test. That is exactly the recurring defect
    `HANDOFF` §6 opens with, and a different plan — a parallel scan, a changed
    join order, a different server — would break AC3 with nothing to catch it.

    The query deliberately has no `ORDER BY`, following `schema_cache.py`: row
    order is not a property of a schema and the code should not depend on the
    server supplying one. This test is what makes that claim true rather than
    merely stated, by supplying rows in the wrong order on purpose.
    """
    from api.db import introspection
    from api.db.execution import ExecutionResult

    def fake(sql):
        if sql is introspection.RELATIONS_SQL:
            return ExecutionResult(ok=True, rows=(("thing", "r"),))
        if sql is introspection.COLUMNS_SQL:
            # attnum 3, 1, 2 — the order a scan is entitled to return.
            return ExecutionResult(
                ok=True,
                rows=(
                    ("thing", "third", 3, "integer", "false", "f"),
                    ("thing", "first", 1, "integer", "true", "t"),
                    ("thing", "second", 2, "integer", "false", "f"),
                ),
            )
        return ExecutionResult(ok=True, rows=())

    monkeypatch.setattr(introspection, "execute_sql", fake)

    columns = introspection.get_schema().tables[0].columns
    assert [column.name for column in columns] == ["first", "second", "third"], (
        "columns must come back in ordinal order (AC3) regardless of the order "
        "the catalog scan returned them in"
    )


def test_foreign_keys_come_back_sorted_regardless_of_arrival_order():
    """**The second surviving mutation.**

    Dropping the explicit sort in `_group_foreign_keys` left everything green:
    Chinook's rows arrive in an order whose grouping already matches the sorted
    order, so the sort was doing nothing observable. AC13 is determinism, and a
    sort that is only ever a no-op on the one schema in the suite is not
    evidence of it.

    Rows are supplied in reverse, so insertion order and sorted order differ.
    """
    from api.db.introspection import _group_foreign_keys

    rows = [
        ("z_fkey", 1, "zeta_id", "zeta", "id"),
        ("m_fkey", 1, "mu_id", "mu", "id"),
        ("a_fkey", 1, "alpha_id", "alpha", "id"),
    ]

    keys = _group_foreign_keys(rows)

    assert [key.columns[0] for key in keys] == ["alpha_id", "mu_id", "zeta_id"], (
        "foreign keys must be sorted, not returned in catalog order"
    )


def test_a_truncated_catalog_read_is_never_returned_as_a_schema(monkeypatch):
    """**A failure mode B-10 introduced, and Chinook is 931 rows short of it.**

    The `Inspector` had no row cap. `execute_sql()` applies Gate 3's
    `MAX_ROWS = 1000`, so a schema with more than a thousand columns comes back
    as a *prefix* — and a prefix is the worst possible answer: it is stable
    across calls, it looks complete, and it silently omits relations the model
    will then never query. Chinook's column read is 69 rows, so nothing here
    would ever notice.

    `schema_cache.py` reached the same conclusion about its probe: a signature
    it cannot verify is never trusted. This is the same rule one layer down.
    """
    from api.db import introspection
    from api.db.execution import ExecutionResult

    monkeypatch.setattr(
        introspection,
        "execute_sql",
        lambda sql: ExecutionResult(
            ok=True,
            columns=("relname", "relkind"),
            rows=(("album", "r"),),
            truncated=True,
        ),
    )

    with pytest.raises(introspection.SchemaIntrospectionError, match="truncated"):
        introspection.get_schema()


def test_a_failed_catalog_read_names_which_query_failed(monkeypatch):
    """Three queries now, so "schema error" alone sends someone to the wrong one."""
    from api.db import introspection
    from api.db.execution import ExecutionResult

    monkeypatch.setattr(
        introspection,
        "execute_sql",
        lambda sql: ExecutionResult(ok=False, category="database_error", error="boom"),
    )

    with pytest.raises(introspection.SchemaIntrospectionError) as caught:
        introspection.get_schema()

    assert "relation list" in str(caught.value)
    assert "boom" in str(caught.value)


# ---------------------------------------------------------------------------
# AC8: the bypass is gone, and stays gone
# ---------------------------------------------------------------------------


def api_modules() -> list[pathlib.Path]:
    """Every module the Inspector ban applies to.

    **Shared with the vacuity guard on purpose, and that was a surviving
    mutation.** The first version of these two tests each built their own list,
    so replacing the scan's iterable with `[]` left the ban vacuous *and* the
    guard green — the guard was checking a different list from the one the ban
    walked. A vacuity guard that does not share the thing it guards is
    decoration.
    """
    return sorted((REPO_ROOT / "api").rglob("*.py"))


def test_ac8_nothing_under_api_uses_the_sqlalchemy_inspector():
    """Charter §4, structurally.

    The `Inspector` is how `get_schema()` read the database without passing
    Gate 2 for seven iterations. Deleting it is the task; keeping it deleted is
    this test, because the next person needing a catalog fact will reach for
    `inspect()` exactly as Iteration 1 did.

    Asserted against the parsed AST rather than the text, for this
    repository's most repeated reason: a grep for "inspect" matches the
    paragraph in `api/db/introspection.py` explaining why the `Inspector` was
    removed.

    **It counts what it scanned, and that is not decoration.** An absence
    assertion passes hardest when it inspects nothing, and a *separate* vacuity
    test cannot close that: neutering this loop while leaving `api_modules()`
    intact left the ban vacuous and the guard green. That mutation survived
    until this counter existed. A test that asserts a negative has to prove its
    own work, in the same test.
    """
    offenders = []
    scanned = 0
    for path in api_modules():
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "sqlalchemy"
            ):
                for alias in node.names:
                    if alias.name in {"inspect", "Inspector"}:
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                            f"imports {alias.name}"
                        )
            elif isinstance(node, ast.Attribute) and node.attr == "inspect":
                if isinstance(node.value, ast.Name) and node.value.id == "sqlalchemy":
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                        f"uses sqlalchemy.inspect"
                    )

    assert scanned > 15, (
        f"this ban only inspected {scanned} modules, so its verdict means "
        f"nothing; the walk is broken rather than api/ being clean"
    )
    assert not offenders, (
        "SQLAlchemy's Inspector reads the catalog over a raw connection, "
        "bypassing Gate 2. Route catalog reads through execute_sql() as "
        "api/db/introspection.py does: " + ", ".join(offenders)
    )


def test_the_inspector_scan_is_not_vacuous():
    """The guard on the guard above.

    That test passes when it finds nothing, so a walk over an empty file list
    passes hardest. This requires the scan to be looking at the modules that
    would actually contain the violation — and it reads the **same** helper the
    ban reads, which is the fix for the mutation that got past the first
    version of this pair.
    """
    modules = api_modules()
    assert len(modules) > 15, f"only {len(modules)} modules found under api/"
    assert (REPO_ROOT / "api" / "db" / "introspection.py") in modules
    assert (REPO_ROOT / "api" / "db" / "type_names.py") in modules


def test_introspection_reads_the_catalog_through_execute_sql():
    """AC8 positively, not only as an absence.

    The three queries must go through `execute_sql()`. A version that imported
    `get_engine` and ran them itself would satisfy the absence test above and
    still bypass Gate 2 — which is the exact shape of the defect being fixed,
    wearing different clothes.
    """
    from api.db import introspection

    source = (REPO_ROOT / "api" / "db" / "introspection.py").read_text("utf-8")
    tree = ast.parse(source)

    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "execute_sql" in imported, "introspection must use the gated executor"
    assert "get_engine" not in imported, (
        "importing get_engine here is how the Gate 2 bypass comes back: the "
        "engine executes whatever it is given, unvalidated"
    )

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "execute_sql" in called

    for name in ("RELATIONS_SQL", "COLUMNS_SQL", "FOREIGN_KEYS_SQL"):
        assert hasattr(introspection, name), f"{name} is gone; three queries expected"
