"""Iteration 6 T2 — the result-shape classifier.

The important test here is not any single classification, it is
`test_the_corpus_distribution_matches_the_spec`: it re-derives
`009-frontend.md` §2.3's numbers from the live database through the *product's*
classifier. That measurement is the entire justification for retiring the
charter's "chart chosen automatically from the shape of the result set", so if
a reseed or a dataset change moves it, the spec's argument has quietly stopped
being true and this fails rather than letting it rot.
"""

from __future__ import annotations

import datetime
import decimal

import pytest

from api.agent.tools import execute_sql
from api.http.shapes import (
    SHAPE_CHARTABLE,
    SHAPE_EMPTY,
    SHAPE_LIST,
    SHAPE_SCALAR,
    SHAPE_TABLE,
    SHAPES,
    chart_series,
    classify,
)
from evals.dataset import load_dataset


# --- the unit cases ---------------------------------------------------------


def test_one_row_one_column_is_scalar():
    assert classify(["count"], [[3503]]) == SHAPE_SCALAR


def test_many_rows_one_column_is_a_list():
    assert classify(["name"], [["Rock"], ["Jazz"]]) == SHAPE_LIST


def test_label_and_measure_is_chartable():
    assert classify(["genre", "n"], [["Rock", 1297], ["Jazz", 130]]) == SHAPE_CHARTABLE


def test_a_single_row_is_never_chartable():
    """One bar is not a chart, and AC9 says the scalar case renders as itself."""
    assert classify(["genre", "n"], [["Rock", 1297]]) == SHAPE_TABLE


def test_two_measures_are_not_chartable():
    """Two numbers give no label to put on the axis."""
    assert classify(["a", "b"], [[1, 2], [3, 4]]) == SHAPE_TABLE


def test_three_columns_are_a_table():
    assert classify(["a", "b", "c"], [["x", 1, 2], ["y", 3, 4]]) == SHAPE_TABLE


def test_empty_result_has_its_own_shape():
    """Zero rows is a legitimate answer, not a failure or a degenerate table.

    The plan was silent on this and it was decided at T2: folding it into
    `TABLE` would make the renderer draw a headed, bodyless grid, and pushes a
    conditional into the UI that the classifier is better placed to answer.
    No corpus question produces one -- the smallest gold result is a single row
    -- so this behaviour is chosen rather than measured.
    """
    assert classify(["name"], []) == SHAPE_EMPTY


def test_empty_is_distinct_from_every_other_shape():
    """Guards the fold-back: if someone later returns `TABLE` for zero rows to
    simplify the renderer, the UI silently loses its "no results matched"
    branch and this is what says so."""
    assert SHAPE_EMPTY not in (SHAPE_SCALAR, SHAPE_LIST, SHAPE_TABLE, SHAPE_CHARTABLE)
    assert SHAPE_EMPTY in SHAPES


def test_booleans_are_not_measures():
    """`isinstance(True, int)` is True, so this is a real trap rather than a
    hypothetical one: without the explicit exclusion this offers a bar chart of
    true/false drawn as 1/0."""
    assert classify(["name", "flag"], [["a", True], ["b", False]]) == SHAPE_TABLE


def test_a_leading_null_does_not_hide_a_measure():
    """Typing the column off `rows[0]` alone misreads any column whose first
    value is NULL, and silently declines to offer a chart that is warranted."""
    rows = [["a", None], ["b", 12], ["c", 7]]
    assert classify(["name", "n"], rows) == SHAPE_CHARTABLE


def test_decimal_counts_as_a_measure():
    rows = [["a", decimal.Decimal("1.5")], ["b", decimal.Decimal("2.5")]]
    assert classify(["name", "avg"], rows) == SHAPE_CHARTABLE


def test_a_temporal_label_is_still_chartable():
    """No corpus result has a date column (`009` §2.5), so this branch is
    untested by the data and is pinned here instead. A date is a label like any
    other; the bar chart does not care that it sorts."""
    rows = [[datetime.date(2013, 1, 1), 10], [datetime.date(2013, 2, 1), 20]]
    assert classify(["month", "n"], rows) == SHAPE_CHARTABLE


# --- which column is the bar drawn from -------------------------------------


def test_chart_series_finds_the_measure_either_way_round():
    assert chart_series(["genre", "n"], [["Rock", 1297], ["Jazz", 130]]) == (0, 1)
    assert chart_series(["n", "genre"], [[1297, "Rock"], [130, "Jazz"]]) == (1, 0)


def test_chart_series_refuses_a_result_it_cannot_chart():
    with pytest.raises(ValueError):
        chart_series(["count"], [[3503]])


# --- the pinned corpus distribution -----------------------------------------


def test_the_corpus_distribution_matches_the_spec():
    """`009-frontend.md` §2.3, re-derived through the product's own classifier.

    28 scalar and 10 chartable are the two numbers the charter amendment
    quotes. If this fails, either the database was reseeded or the dataset
    changed, and the spec's argument for retiring "chosen automatically" needs
    re-checking before the numbers are edited to match.
    """
    counts: dict[str, int] = {}
    for question in load_dataset().questions:
        result = execute_sql(question.gold_sql)
        assert result.ok, f"{question.id}: gold query failed ({result.category})"
        shape = classify(result.columns, result.rows)
        counts[shape] = counts.get(shape, 0) + 1

    assert counts[SHAPE_SCALAR] == 28
    assert counts[SHAPE_CHARTABLE] == 10
    assert counts[SHAPE_LIST] == 3
    assert counts[SHAPE_TABLE] == 9
    assert sum(counts.values()) == 50


def test_easy_010_is_chartable_on_a_column_of_identifiers():
    """**The false positive, pinned deliberately.**

    "List the id and name of every playlist" classifies as chartable because a
    playlist id is numeric. This is not a bug to be fixed by a cleverer
    heuristic -- it is the measured reason (`009` §2.4) the chart is a toggle
    the human controls rather than something drawn automatically. If a future
    change makes this test fail by "fixing" the classification, the toggle's
    justification has to be re-argued, not silently strengthened.
    """
    question = next(q for q in load_dataset().questions if q.id == "easy-010")
    result = execute_sql(question.gold_sql)
    assert classify(result.columns, result.rows) == SHAPE_CHARTABLE
