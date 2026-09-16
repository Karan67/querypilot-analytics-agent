"""Iteration 6 T3 — the JSON boundary.

The test that matters is `test_decimal_survives_as_an_exact_string`, and the
values in it are chosen rather than picked. See its docstring: the value the
plan originally specified does **not** discriminate on precision, and a test
built on it would have proved less than it appeared to.
"""

from __future__ import annotations

import datetime
import decimal
import json

import pytest

from api.agent.tools import execute_sql
from api.db.introspection import Column, ForeignKey, Schema, Table
from api.http.serialization import encode_rows, schema_to_dict, to_jsonable


# --- D-1: Decimal crosses as an exact string --------------------------------


def test_decimal_survives_as_an_exact_string():
    """**The mutation this must catch is encoding via `float()`.**

    `2328.60` is the real total of every Chinook invoice (`easy-008`), and a
    float round trip renders it `2328.6` -- a money column losing its cents on
    the way to the screen. The 25-digit value collapses to `1.0`.

    `4.0000005` is included because the plan named it, but on its own it would
    have been a weak test: `str(float(Decimal("4.0000005")))` is *also*
    `"4.0000005"`, so it catches the float mutation only by the returned type
    changing, never by the precision loss it is supposed to be about. That is
    `HANDOFF.md` §6's trap in a new costume -- a discriminating value inherited
    from a different comparison, where it did discriminate.
    """
    assert to_jsonable(decimal.Decimal("2328.60")) == "2328.60"
    assert to_jsonable(decimal.Decimal("1.000000000000000000000001")) == (
        "1.000000000000000000000001"
    )
    assert to_jsonable(decimal.Decimal("4.0000005")) == "4.0000005"


def test_the_float_encoding_would_actually_lose_these_values():
    """Proves the test above is not vacuous.

    If `float()` happened to round-trip all three, the assertions would pass
    under the mutation and the defence would be untested. This asserts the
    mutation is genuinely destructive for the first two values, so a green
    `test_decimal_survives_as_an_exact_string` means something.
    """
    assert str(float(decimal.Decimal("2328.60"))) == "2328.6"
    assert str(float(decimal.Decimal("1.000000000000000000000001"))) == "1.0"
    # ... and documents that the third one does not, which is why it is not
    # relied on alone.
    assert str(float(decimal.Decimal("4.0000005"))) == "4.0000005"


def test_a_decimal_is_json_serialisable_after_encoding():
    """`json.dumps` refuses a raw `Decimal`; that refusal is the whole reason
    this module exists."""
    assert json.dumps(to_jsonable(decimal.Decimal("1.5"))) == '"1.5"'


# --- types the schema can actually produce ----------------------------------


def test_timestamps_become_iso_8601():
    value = datetime.datetime(2013, 1, 2, 3, 4, 5)
    assert to_jsonable(value) == "2013-01-02T03:04:05"


def test_a_datetime_is_not_truncated_to_a_date():
    """A timestamp keeps its time component.

    **This test's original docstring was wrong and a mutation proved it.** It
    claimed `datetime` had to be matched before `date` or timestamps would be
    truncated. Swapping the two branches changed nothing: `isoformat`
    dispatches on the real type, so a `datetime` caught by the `date` branch
    still renders in full. The module now has one polymorphic branch.

    What this does guard is a caller reaching for `value.date()`, or slicing
    the ISO string to ten characters -- both of which would silently drop the
    time from every `timestamp` column in the schema, of which there are four.
    """
    encoded = to_jsonable(datetime.datetime(2013, 1, 2, 3, 4, 5))
    assert encoded == "2013-01-02T03:04:05"
    assert encoded != "2013-01-02"


def test_dates_and_times_encode():
    assert to_jsonable(datetime.date(2013, 1, 2)) == "2013-01-02"
    assert to_jsonable(datetime.time(3, 4, 5)) == "03:04:05"


def test_an_interval_is_readable():
    """`max(invoice_date) - min(invoice_date)` is a plausible question."""
    assert to_jsonable(datetime.timedelta(days=2557)) == "2557 days, 0:00:00"


def test_booleans_stay_booleans():
    """Exact-type dispatch, not an isinstance ladder: `isinstance(True, int)`
    is True, so an ordered ladder with `int` first would emit `1`."""
    assert to_jsonable(True) is True
    assert json.dumps(to_jsonable(True)) == "true"


def test_primitives_pass_through_untouched():
    assert to_jsonable("Rock") == "Rock"
    assert to_jsonable(3503) == 3503
    assert to_jsonable(None) is None
    assert to_jsonable(1.5) == 1.5


def test_an_unanticipated_type_becomes_its_string_form():
    """Pinned because it is a decision, not an accident.

    The schema reaches five column types and all are handled explicitly, so
    this path means an expression produced something unforeseen. On a display
    boundary a string is more useful than refusing the answer, and it cannot
    corrupt anything downstream. If that should instead be an error, this test
    is where the change announces itself.
    """

    class Unknown:
        def __str__(self) -> str:
            return "surprising"

    assert to_jsonable(Unknown()) == "surprising"


# --- whole result sets ------------------------------------------------------


def test_encode_rows_preserves_shape_and_order():
    rows = [["Rock", decimal.Decimal("1.50")], ["Jazz", decimal.Decimal("2.25")]]
    assert encode_rows(rows) == [["Rock", "1.50"], ["Jazz", "2.25"]]


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_a_real_result_set_round_trips_through_json():
    """End to end against the live database, on the question whose answer is a
    money column -- the case D-1 was decided for."""
    result = execute_sql("SELECT sum(total) FROM invoice")
    assert result.ok
    encoded = encode_rows(result.rows)
    assert encoded == [["2328.60"]]
    assert json.loads(json.dumps(encoded)) == [["2328.60"]]


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_every_corpus_result_is_json_serialisable():
    """The boundary must not raise on anything the benchmark can produce.

    Broader than the unit cases on purpose: it is the only test here that would
    notice a column type the schema gains later.
    """
    from evals.dataset import load_dataset

    for question in load_dataset().questions:
        result = execute_sql(question.gold_sql)
        assert result.ok, question.id
        json.dumps(encode_rows(result.rows))


# --- 018-ui-redesign.md T3: GET /schema's JSON shape -------------------------


def test_schema_to_dict_is_the_narrow_contract_ac4_names():
    """AC4/§7 Q-C: `name`, `kind`, and each column's `name`/`type` -- nothing
    from `foreign_keys`, even though `Table` carries it.

    **The mutation this must catch is `dataclasses.asdict(schema)`.** That
    would pass every other test here (it's a strict superset of the fields
    asserted) while silently widening the response contract the spec
    deliberately narrowed -- exactly the "a default elsewhere stands in for
    the code under test" shape `HANDOFF.md` §6 keeps recording, just with the
    permissive replacement written directly into the function instead of
    somewhere upstream of it.
    """
    schema = Schema(
        tables=(
            Table(
                name="album",
                kind="table",
                columns=(
                    Column(name="album_id", type="INTEGER", nullable=False, primary_key=True),
                    Column(name="title", type="VARCHAR(160)", nullable=False, primary_key=False),
                ),
                foreign_keys=(
                    ForeignKey(columns=("artist_id",), referred_table="artist", referred_columns=("artist_id",)),
                ),
            ),
        )
    )

    encoded = schema_to_dict(schema)

    assert encoded == {
        "tables": [
            {
                "name": "album",
                "kind": "table",
                "columns": [
                    {"name": "album_id", "type": "INTEGER"},
                    {"name": "title", "type": "VARCHAR(160)"},
                ],
            }
        ]
    }
    # The narrowness itself, stated as its own assertion rather than only
    # implied by the equality above: a reader changing the equality's shape
    # later would not necessarily notice they had reintroduced the field.
    assert "foreign_keys" not in encoded["tables"][0]


def test_schema_to_dict_preserves_table_order():
    """AC1 of `001-schema-tool.md` (alphabetical, kinds interleaved) is a
    property of `get_schema()`'s return, not of this function -- but this
    function must not silently re-sort or re-group what it is given, or a
    correct `Schema` would still render wrong."""
    schema = Schema(
        tables=(
            Table(name="zebra", kind="view", columns=(), foreign_keys=()),
            Table(name="album", kind="table", columns=(), foreign_keys=()),
        )
    )

    encoded = schema_to_dict(schema)

    assert [table["name"] for table in encoded["tables"]] == ["zebra", "album"]
