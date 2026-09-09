"""The JSON boundary — the one place database values become JSON primitives.

`ExecutionResult` deliberately holds **native Python types** and says why:

    `rows` holds native Python types -- `Decimal` for NUMERIC, `datetime` for
    TIMESTAMP. Converting to JSON primitives happens at the API boundary, not
    here: `Decimal` to `float` silently loses precision, and an analytics
    product should not degrade its numbers on the way out of the database.

This is that boundary. It is one module and one function on purpose (spec §5):
two encoders that disagree about `Decimal` is the drift `HANDOFF.md` §6 keeps
recording.

**`Decimal` becomes a JSON string** (plan D-1). The product's claim is
auditability, so the number on screen must be the number in the database. A
JSON *number* would not achieve that even at full precision, because every
browser parses it into a float64 at the only point it would be read — the
fidelity would be lost in the client, invisibly. The chart is explicitly
allowed to be approximate: `app.js` parses these strings to float for SVG bar
widths, where a rounding error is a sub-pixel difference.

**What this costs, stated rather than discovered later:** a consumer cannot do
arithmetic on a `numeric` column without parsing it first. That is the correct
trade for a display surface and would be the wrong one for a data API, which
this is not.
"""

from __future__ import annotations

import datetime
import decimal
from collections.abc import Sequence

#: Types that are already JSON primitives and pass through untouched.
#:
#: `bool` is listed **before** `int` matters nowhere here because this is an
#: exact-type check rather than an `isinstance` chain -- which is itself the
#: reason it is exact. `isinstance(True, int)` is True, so an `isinstance`
#: ladder would encode `True` as an integer if `int` came first.
_PASSTHROUGH = (str, int, float, bool, type(None))


def to_jsonable(value: object) -> object:
    """One database value, as something `json.dumps` accepts.

    Ordering is by exact type first, so `bool` can never be mistaken for `int`.
    """
    if type(value) in _PASSTHROUGH:
        return value

    # Precision-preserving, per D-1. `str` on a Decimal is exact and keeps the
    # scale the database chose: Decimal("2328.60") stays "2328.60" rather than
    # becoming 2328.6, which is what a float round trip does to a money column.
    if isinstance(value, decimal.Decimal):
        return str(value)

    # `datetime` subclasses `date`, and `isoformat` dispatches on the real
    # type, so one branch correctly handles date, datetime and time alike --
    # a `datetime` matched here still returns its full timestamp.
    #
    # An earlier version of this module ordered `datetime` before `date` and
    # claimed the order was load-bearing. It is not: a mutation swapping them
    # changed nothing, which is how the false rationale was found. The risk
    # this branch actually carries is a caller reaching for `value.date()` or
    # slicing the string, and that is what the test pins.
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()

    # `max(invoice_date) - min(invoice_date)` is a plausible question and comes
    # back as a timedelta. `str` gives "2557 days, 0:00:00", which is what a
    # reader wants; an ISO-8601 duration would be more correct and less useful.
    if isinstance(value, datetime.timedelta):
        return str(value)

    # Deliberate, not a leftover. The schema reaches five column types --
    # varchar, integer, bigint, numeric, timestamp -- all handled above, so
    # arriving here means an expression produced something unanticipated. This
    # is a *display* boundary: showing the value's string form is strictly more
    # useful than refusing the whole answer, and it cannot corrupt anything
    # downstream because the destination is a screen.
    #
    # Tested rather than assumed, so the behaviour is chosen and pinned instead
    # of being whatever happens to occur.
    return str(value)


def encode_rows(rows: Sequence[Sequence[object]]) -> list[list[object]]:
    """Every cell of a result set, JSON-ready."""
    return [[to_jsonable(cell) for cell in row] for row in rows]
