"""Schema rendering tests — `specs/008-prompt-tuning.md` AC6, AC7 (Iteration 5 T2).

Three renderings of the same structure, and the machinery that stops two of them
being compared by accident.

**Mostly pure.** Rendering is a function of its arguments, so the structural
tests use a hand-built schema and need no database. The token measurements need
the real Chinook schema, because a token count of an invented schema would
measure nothing anybody cares about.
"""

from __future__ import annotations

import pytest

from api.agent.glossary import GLOSSARY_HEADER
from api.agent.prompts import (
    ADOPTED_RENDERING,
    COMPACT_LEGEND,
    NOT_NULL_MARK,
    SCHEMA_COMPACT,
    SCHEMA_COMPACT_ABBREV,
    SCHEMA_DDL,
    SCHEMA_FULL,
    SCHEMA_RENDERINGS,
    SCHEMA_WITHHELD,
    TYPE_ABBREVIATIONS,
    abbreviate_type,
    build_loop_system,
    render_schema,
    render_schema_ddl,
)
from api.db.introspection import KIND_TABLE, KIND_VIEW, Column, ForeignKey, Schema, Table

pytestmark = pytest.mark.usefixtures("configured_database")


def _fixture_schema() -> Schema:
    """A schema exercising every feature the compact form can emit.

    Hand-built rather than introspected: these assertions are about the
    *renderer*, and a fixture that changes when somebody reseeds the database
    would turn a rendering bug and a data change into the same red test.
    """
    return Schema(
        tables=(
            Table(
                name="parent",
                kind=KIND_TABLE,
                columns=(
                    Column(name="parent_id", type="INTEGER", nullable=False, primary_key=True),
                    Column(name="label", type="VARCHAR(40)", nullable=True, primary_key=False),
                ),
                foreign_keys=(),
            ),
            Table(
                name="child",
                kind=KIND_TABLE,
                columns=(
                    Column(name="parent_id", type="INTEGER", nullable=False, primary_key=True),
                    Column(name="seq", type="INTEGER", nullable=False, primary_key=True),
                    Column(name="amount", type="NUMERIC(10, 2)", nullable=True, primary_key=False),
                ),
                foreign_keys=(
                    ForeignKey(
                        columns=("parent_id",),
                        referred_table="parent",
                        referred_columns=("parent_id",),
                    ),
                ),
            ),
            Table(
                name="summary",
                kind=KIND_VIEW,
                # `nullable=False` on a VIEW column is deliberately a state that
                # live introspection does not currently produce -- Postgres
                # reports every view column nullable (`001` AC4).
                #
                # **Without it the AC4 test is vacuous.** Caught by mutation:
                # deleting the renderer's `not is_view` guard left the test
                # green, because a fixture whose view columns are all nullable
                # cannot tell a working guard from a missing one. That is the
                # same shape as the `0.1 + 0.2` decimal test this project has
                # been bitten by before, and it is why the fixture asserts the
                # renderer's *contract* rather than mirroring the catalog.
                columns=(
                    Column(name="parent_id", type="INTEGER", nullable=True, primary_key=False),
                    Column(name="total", type="NUMERIC", nullable=False, primary_key=False),
                ),
                foreign_keys=(),
            ),
        )
    )


# --- AC6: the renderings exist and are selectable ---------------------------


def test_ddl_rendering_is_byte_identical_to_the_iteration_4_renderer():
    """**The baseline must not move.**

    Iterations 3 and 4 measured every recorded number against
    `render_schema_ddl`. If routing it through the new dispatcher changed one
    byte, those numbers would silently stop being reproducible — the exact
    failure AC7 exists to prevent, arriving through the back door.
    """
    schema = _fixture_schema()
    assert render_schema(schema, SCHEMA_DDL) == render_schema_ddl(schema)


def test_an_unknown_rendering_is_refused():
    with pytest.raises(ValueError, match="unknown schema rendering"):
        render_schema(_fixture_schema(), "compct")


def test_build_loop_system_refuses_an_unknown_rendering_even_when_withheld():
    """A withheld run ignores the rendering, so validating it there is the only
    thing stopping `--rendering complct` passing in one mode and failing in the
    other — with the passing run recorded under a rendering that never ran."""
    with pytest.raises(ValueError, match="unknown schema rendering"):
        build_loop_system(_fixture_schema(), SCHEMA_WITHHELD, "complct")


def test_every_declared_rendering_actually_renders():
    schema = _fixture_schema()
    for rendering in SCHEMA_RENDERINGS:
        assert render_schema(schema, rendering).strip()


def test_renderings_are_deterministic():
    """Two calls, same string. A prompt that varies between passes makes an
    accuracy delta unattributable (`001` AC13)."""
    schema = _fixture_schema()
    for rendering in SCHEMA_RENDERINGS:
        assert render_schema(schema, rendering) == render_schema(schema, rendering)


def test_no_two_renderings_produce_the_same_text():
    schema = _fixture_schema()
    rendered = {r: render_schema(schema, r) for r in SCHEMA_RENDERINGS}
    assert len(set(rendered.values())) == len(SCHEMA_RENDERINGS)


# --- what the compact form carries (resolved T2 decision) -------------------


def test_compact_names_every_relation_and_column():
    """The floor. A compaction that dropped a column would score badly for a
    reason no token count would explain."""
    schema = _fixture_schema()
    compact = render_schema(schema, SCHEMA_COMPACT)
    for table in schema.tables:
        assert table.name in compact
        for column in table.columns:
            assert column.name in compact


def test_compact_marks_not_null_on_tables():
    compact = render_schema(_fixture_schema(), SCHEMA_COMPACT)
    assert f"parent_id INTEGER{NOT_NULL_MARK}" in compact
    assert f"label VARCHAR(40){NOT_NULL_MARK}" not in compact, "label is nullable"


def test_ac4_compact_never_marks_not_null_on_a_view():
    """`001` AC4: Postgres does not propagate NOT NULL through a view, so
    claiming it would tell the model something untrue about the data.

    **The fixture's view carries a `nullable=False` column on purpose** — a
    state live introspection never produces. Handed a schema that claims a view
    column is NOT NULL, the renderer must still refuse to say so. Asserting it
    against the live schema instead proves nothing, because every real view
    column is already nullable and the guard cannot be observed to do anything.
    """
    schema = _fixture_schema()
    view = next(t for t in schema.tables if t.kind == KIND_VIEW)
    assert any(not c.nullable for c in view.columns), (
        "the fixture no longer exercises the guard; this test is vacuous again"
    )

    compact = render_schema(schema, SCHEMA_COMPACT)
    view_line = next(ln for ln in compact.splitlines() if ln.startswith("view summary"))
    assert NOT_NULL_MARK not in view_line


def test_ac4_the_ddl_renderer_also_never_claims_not_null_on_a_view():
    """The same guard, on the older renderer — which was equally untested.

    `tests/test_prompts.py` asserts this against the *live* schema, where every
    view column is nullable anyway, so deleting the DDL renderer's guard leaves
    that test green. Found while mutation-testing the compact renderer, and
    fixed here rather than left as a known-vacuous assertion elsewhere.
    """
    rendered = render_schema_ddl(_fixture_schema())
    view_block = rendered.split("CREATE VIEW summary")[1]
    assert "NOT NULL" not in view_block


def test_compact_distinguishes_a_view_from_a_table():
    compact = render_schema(_fixture_schema(), SCHEMA_COMPACT)
    assert "view summary(" in compact
    assert "view parent(" not in compact


def test_compact_carries_the_foreign_key_target_column():
    """Kept at +24 tokens despite being derivable in Chinook, where every FK
    points at the referenced table's primary key.

    Chinook is the fixture, not the deployment target: a real schema may key on
    a non-primary unique column, and a rendering that is only unambiguous on a
    tidy database is not unambiguous.
    """
    compact = render_schema(_fixture_schema(), SCHEMA_COMPACT)
    assert "FK[parent_id->parent.parent_id]" in compact


def test_compact_renders_a_composite_primary_key_whole():
    """`001` AC7: every column of a composite key, not just the first."""
    compact = render_schema(_fixture_schema(), SCHEMA_COMPACT)
    assert "PK[parent_id,seq]" in compact


def test_compact_includes_a_legend_explaining_its_notation():
    """A terse notation nobody explained is a guessing game. The legend is
    charged once per prompt, which is what lets the notation stay terse."""
    compact = render_schema(_fixture_schema(), SCHEMA_COMPACT)
    assert COMPACT_LEGEND in compact
    assert NOT_NULL_MARK in COMPACT_LEGEND
    assert "view" in COMPACT_LEGEND


# --- abbreviation -----------------------------------------------------------


def test_abbreviation_reduces_types_to_classes():
    abbrev = render_schema(_fixture_schema(), SCHEMA_COMPACT_ABBREV)
    assert "parent_id int" in abbrev
    assert "label str" in abbrev
    assert "INTEGER" not in abbrev
    assert "VARCHAR" not in abbrev


def test_abbreviation_discards_numeric_scale():
    """**The known cost, asserted rather than hoped about.**

    `NUMERIC(10, 2)` becomes `num`, so three money columns lose their scale.
    This test exists so the loss is a documented property of the rendering
    rather than something discovered from a bad eval number at T7.
    """
    abbrev = render_schema(_fixture_schema(), SCHEMA_COMPACT_ABBREV)
    assert "amount num" in abbrev
    assert "10, 2" not in abbrev


def test_an_unrecognised_type_is_never_guessed_at():
    """Falls through to its own text. A type mapped to the wrong class tells the
    model something false; a long accurate type merely costs tokens."""
    assert abbreviate_type("JSONB") == "jsonb"
    assert abbreviate_type("GEOGRAPHY(POINT, 4326)") == "geography(point, 4326)"


def test_abbreviation_prefixes_are_ordered_longest_first_where_they_overlap():
    """Ordering is load-bearing for any pair where one prefix contains another.

    Asserted as a property of the table rather than by spot-checking two types,
    so an entry added later cannot silently shadow one above it.
    """
    for index, (prefix, _) in enumerate(TYPE_ABBREVIATIONS):
        for earlier, _ in TYPE_ABBREVIATIONS[:index]:
            assert not prefix.startswith(earlier) or prefix == earlier, (
                f"{prefix!r} is shadowed by {earlier!r} earlier in the table"
            )


# --- AC7: fingerprints ------------------------------------------------------


def test_ac7_every_rendering_has_a_distinct_fingerprint():
    """No two renderings can be compared by accident."""
    from evals.run_evals import schema_fingerprint

    prints = {r: schema_fingerprint(r) for r in SCHEMA_RENDERINGS}
    assert len(set(prints.values())) == len(SCHEMA_RENDERINGS), prints


def test_ac7_the_fingerprint_is_derived_from_output_not_from_the_name():
    """**The blind spot this closes.**

    A name-only hash would distinguish `compact` from `ddl` but would not notice
    the renderer itself being edited — which is the state the project was in
    until now: changing `render_schema_ddl` produced an identical fingerprint
    and a silently incomparable number.
    """
    import evals.run_evals as runner

    before = runner.schema_fingerprint(SCHEMA_COMPACT)
    original = runner._FINGERPRINT_SCHEMA
    try:
        runner._FINGERPRINT_SCHEMA = Schema(
            tables=(
                Table(
                    name="parent",
                    kind=KIND_TABLE,
                    columns=(
                        Column(
                            name="parent_id",
                            type="BIGINT",  # changed
                            nullable=False,
                            primary_key=True,
                        ),
                    ),
                    foreign_keys=(),
                ),
            )
        )
        assert runner.schema_fingerprint(SCHEMA_COMPACT) != before
    finally:
        runner._FINGERPRINT_SCHEMA = original

    assert runner.schema_fingerprint(SCHEMA_COMPACT) == before, "restored"


def test_the_prompt_fingerprint_is_unchanged_by_this_iteration():
    """**Iteration 4's recorded numbers stay comparable.**

    All three loop entries in `EVALS.md` carry `0d280c367c5e`. The rendering was
    given its own field precisely so that adding it would not re-baseline them,
    and this pins that promise: a later refactor folding rendering into the
    prompt hash fails here and has to say so in `EVALS.md` instead.
    """
    from evals.run_evals import STRATEGY_LOOP, STRATEGY_SINGLE_SHOT, fingerprint_for

    assert fingerprint_for(STRATEGY_SINGLE_SHOT) == "f971d8787f0c"
    assert fingerprint_for(STRATEGY_LOOP) == "0d280c367c5e"


# --- the assembled prompt ---------------------------------------------------


def test_the_rendering_reaches_the_assembled_prompt(schema):
    ddl = build_loop_system(schema, SCHEMA_FULL, SCHEMA_DDL)
    compact = build_loop_system(schema, SCHEMA_FULL, SCHEMA_COMPACT)

    assert "CREATE TABLE track" in ddl
    assert "CREATE TABLE" not in compact
    assert "track(" in compact


def test_d2_adopted_compact():
    """**The decision itself, not the wiring.**

    `test_the_default_rendering_is_the_adopted_one` below compares the default
    against `ADOPTED_RENDERING`, which holds for any value the constant takes --
    it proves the default is wired to the constant and nothing about which
    rendering was chosen. This line is the other half: D-2's pre-registered rule
    selected `compact` on the 2026-09-04 dev-split A/B, and changing that should
    be a deliberate act that edits a test, not a one-word edit that nothing
    notices.
    """
    assert ADOPTED_RENDERING == SCHEMA_COMPACT


def test_the_default_rendering_is_the_adopted_one(schema):
    """**The adoption is the default, or it is not an adoption.**

    This test read `SCHEMA_DDL` until T7, on the grounds that a caller
    predating Iteration 5 should get a byte-identical prompt. D-2 selected
    `compact` on a dev-split A/B and the pre-registered rule is what decides
    this, so the assertion is re-stated rather than relaxed -- it still pins
    an exact equality, to a different value.

    The reproducibility the old rationale wanted is not lost; it is asserted
    directly below, where it does not depend on a default at all.
    """
    assert build_loop_system(schema, SCHEMA_FULL) == build_loop_system(
        schema, SCHEMA_FULL, ADOPTED_RENDERING
    )
    assert ADOPTED_RENDERING in SCHEMA_RENDERINGS


def test_iteration_4s_prompt_is_still_reproducible(schema):
    """What the old default-is-DDL test was really protecting.

    A recorded number is only comparable to a later one if the prompt it was
    measured against can still be built. Iteration 4's prompt is DDL with no
    glossary, and after T3 and T7 *neither* of those is a default any more --
    so the guarantee has to be asserted explicitly or it is not a guarantee.
    """
    iteration_4 = build_loop_system(schema, SCHEMA_FULL, SCHEMA_DDL, glossary=False)

    assert "CREATE TABLE track" in iteration_4
    assert GLOSSARY_HEADER not in iteration_4
    # `FK[` is unique to the compact form. `track(` is not -- DDL writes
    # `REFERENCES track(track_id)`, which is what the first version of this
    # assertion tripped over.
    assert "FK[" not in iteration_4
    assert COMPACT_LEGEND not in iteration_4


def test_compaction_actually_reduces_the_measured_prompt(schema):
    """**Measured with a real tokenizer, on the real schema.**

    The whole justification for this task is a token saving, so it is asserted
    rather than assumed — and asserted as an ordering rather than as fixed
    numbers, which would break on every legend edit without catching anything a
    comparison does not.

    Measured at T2 on Chinook: ddl 925 tokens, compact 737 (−20.3%),
    compact-abbrev 635 (−31.4%).
    """
    tiktoken = pytest.importorskip("tiktoken")
    encoding = tiktoken.get_encoding("o200k_base")

    def count(rendering: str) -> int:
        return len(encoding.encode(build_loop_system(schema, SCHEMA_FULL, rendering)))

    ddl, compact, abbrev = (count(r) for r in SCHEMA_RENDERINGS)
    assert abbrev < compact < ddl, (ddl, compact, abbrev)
