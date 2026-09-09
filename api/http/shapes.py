"""What shape is this result, and can it be charted?

The UI renders by shape, so this is the module that decides whether a viewer
sees a large number, a table, or a table with a chart toggle. It is deliberately
**one classifier, used by both the product and the measurements** that justify
it: `009-frontend.md` §2.3 reports a distribution over the eval corpus, and
`tests/test_result_shapes.py` pins that distribution by importing *this* code.
Two implementations of "is this a scalar" that could drift apart is the failure
`HANDOFF.md` §6 keeps warning about.

**`CHARTABLE` does not mean "draw a chart".** Resolved Q-B settled that the
human decides; this classifier only says a chart is *offerable*. It cannot tell
a measure from an identifier, and it is not asked to: `easy-010` ("List the id
and name of every playlist") classifies as `CHARTABLE` on a column of playlist
**ids**, and that is correct behaviour for a rule that exists to populate a
toggle rather than to draw something unasked. A classifier confident enough to
chart that automatically is worse than no classifier, which is why the charter's
"chosen automatically" was retired at Iteration 6 T1.
"""

from __future__ import annotations

import datetime
import decimal
from collections.abc import Sequence

#: One row, one column. **The majority case: 28 of 50 corpus questions, 56%**
#: (`009-frontend.md` §2.3). Rendered as the number itself, never as a
#: single-bar chart.
SHAPE_SCALAR = "scalar"

#: Multiple rows, one column. A list of names, not a measurement.
SHAPE_LIST = "list"

#: Anything else. Rendered as a plain table; at 55 rows for the largest result
#: in the corpus there is nothing to paginate.
SHAPE_TABLE = "table"

#: Columns, but no rows. **A legitimate answer, not a failure** — "no customers
#: in Antarctica" is a correct response to a well-formed question, and `ok` is
#: still True.
#:
#: Given its own shape rather than folded into `TABLE` (decided at T2, after the
#: plan was silent on it) so the renderer can say *"no results matched"* instead
#: of drawing a blank grid with headers. The alternative pushed a conditional
#: into the UI that the classifier is better placed to answer.
#:
#: No corpus question produces one, so nothing about this is measured — the
#: smallest gold result is a single row.
SHAPE_EMPTY = "empty"

#: Multiple rows, two columns, exactly one of them numeric — a label and a
#: number. **10 of 50 corpus questions**, which is the ceiling on how often a
#: chart is even offered.
SHAPE_CHARTABLE = "chartable"

SHAPES = (SHAPE_SCALAR, SHAPE_LIST, SHAPE_TABLE, SHAPE_CHARTABLE, SHAPE_EMPTY)


def _is_numeric(value: object) -> bool:
    """Is this a number a bar could be drawn from?

    `bool` is excluded explicitly, and the exclusion is the whole reason this
    is a function. `isinstance(True, int)` is `True` in Python, so a boolean
    column would otherwise read as the measure of a two-column result and offer
    a bar chart of true/false rendered as 1/0.
    """
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float, decimal.Decimal))


def _is_temporal(value: object) -> bool:
    """`datetime` is a subclass of `date`, so this catches both."""
    return isinstance(value, datetime.date)


def _column_sample(rows: Sequence[Sequence[object]], index: int) -> object:
    """The first non-NULL value in a column, or `None` if it is entirely NULL.

    Reading types off `rows[0]` alone is wrong for any column whose first value
    happens to be NULL -- `type(None)` is not numeric, so a real measure column
    would be misread as text and the chart never offered. Scanning costs
    nothing at 55 rows.
    """
    for row in rows:
        if index < len(row) and row[index] is not None:
            return row[index]
    return None


def classify(columns: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """Classify one result set.

    Takes `columns` and `rows` rather than an `ExecutionResult` so that the
    boundary does not depend on the database layer's types, and so a test can
    hand it a literal.
    """
    n_rows, n_cols = len(rows), len(columns)

    # Columns, but nothing to show. Its own shape so the renderer can say "no
    # results matched" rather than draw a headed, bodyless grid.
    if n_rows == 0:
        return SHAPE_EMPTY

    if n_cols == 1:
        return SHAPE_SCALAR if n_rows == 1 else SHAPE_LIST

    if n_rows == 1:
        return SHAPE_TABLE

    if n_cols == 2:
        samples = [_column_sample(rows, i) for i in range(2)]
        if sum(1 for value in samples if _is_numeric(value)) == 1:
            return SHAPE_CHARTABLE

    return SHAPE_TABLE


def chart_series(
    columns: Sequence[str], rows: Sequence[Sequence[object]]
) -> tuple[int, int]:
    """`(label_index, measure_index)` for a `CHARTABLE` result.

    Separated from `classify` so the caller never has to re-derive which column
    the bar length comes from, and so getting it backwards is a test failure in
    one place rather than a silent transposition in the renderer.

    Raises:
        ValueError: if the result is not `CHARTABLE`. Refusing beats returning
            `(0, 1)` and letting a caller draw bars from a text column.
    """
    if classify(columns, rows) != SHAPE_CHARTABLE:
        raise ValueError("chart_series is only defined for a chartable result")
    measure = next(
        i for i in range(2) if _is_numeric(_column_sample(rows, i))
    )
    return (1 - measure, measure)
