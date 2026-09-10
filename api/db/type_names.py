"""Translate `pg_catalog` type spellings into the ones the prompt already uses.

**This module exists to protect the measurement record, not to be elegant.**

B-10 replaces SQLAlchemy's `Inspector` with three catalog queries, because the
`Inspector` reached the database around Gate 2 at 52 statements a call. The
obstacle was never the queries; it was that the two sources spell types
differently, and the rendered schema goes into the system prompt, and the prompt
is hashed into the fingerprint that every `EVALS.md` entry carries. A naive
replacement therefore retires the comparability of the entire benchmark record
(`011-ship.md` section 2.4) -- the same cost that deferred B-4 to its own
milestone.

So the rule is byte identity, and AC9 makes it a gate rather than a goal: if the
rendered schema differs, the task reverts and B-10 goes back to the board.

## What is verified, and what is a best effort

Chinook's whole schema needs **five base families**, measured against the live
catalog on 2026-09-10:

| `format_type()` | this module | occurrences |
|---|---|---|
| `bigint` | `BIGINT` | 1 |
| `integer` | `INTEGER` | 26 |
| `numeric` | `NUMERIC` | 1 |
| `numeric(10,2)` | `NUMERIC(10, 2)` | 3 |
| `character varying(N)` | `VARCHAR(N)` | 34 |
| `timestamp without time zone` | `TIMESTAMP` | 4 |

Those five are proved to the byte by `tests/fixtures/rendered_schema_*.txt`,
frozen from the `Inspector` before it was removed. **Everything else is a
documented best effort**, and the honest statement of the maintenance question
section 2.4 raised: a real warehouse brings `text`, `boolean`, `date`, `jsonb`,
`uuid`, arrays and domains, and each is a line in a mapping nobody will remember
to update. Five is Chinook's answer, with a number on one side and none on the
other.

**Note the space in `NUMERIC(10, 2)`.** SQLAlchemy renders modifiers with
`", "`; Postgres emits `numeric(10,2)`. Three money columns depend on that
scale being visible to the model, and one character decides whether eight
iterations of recorded accuracy stay comparable.

## The oracle that looked authoritative and was wrong

The obvious way to verify more than five families is SQLAlchemy's own
reflection table, `PGDialect.ischema_names` -- 50 entries mapping catalog names
to type classes -- compiled through the dialect's type compiler. That was tried
and **it disagrees with the `Inspector` on one of the five families**:

    ischema_names["timestamp without time zone"] -> TIMESTAMP WITHOUT TIME ZONE
    what the Inspector actually produced          -> TIMESTAMP

because the reflection path does not use that table directly for temporal
types; it parses the modifier out and constructs `TIMESTAMP(timezone=False)`,
which compiles to the short form. A verification oracle that is wrong on 20% of
the cases it is meant to check is worse than no oracle, since it would have been
believed. The frozen renderings are the only trustworthy evidence, which is why
D-3 accepted them.
"""

from __future__ import annotations

import re

#: The five families measured against Chinook (section 2.4). Keys are what
#: `format_type(atttypid, atttypmod)` returns with any modifier removed; values
#: are the spelling SQLAlchemy's type compiler produced for the same column.
#:
#: Deliberately not extended past what was measured. Resolved D-7 confines T5 to
#: the type-family mapping, and an unmeasured entry here would be indistinguish-
#: able from a measured one to the next reader -- which is how a table of facts
#: becomes a table of guesses.
BASE_SPELLINGS = {
    "bigint": "BIGINT",
    "integer": "INTEGER",
    "numeric": "NUMERIC",
    "character varying": "VARCHAR",
    "timestamp without time zone": "TIMESTAMP",
}

#: The parenthesised modifier group, wherever it appears.
#:
#: Not anchored to the end, because Postgres puts it in the *middle* of some
#: temporal types: `timestamp(3) without time zone`. A trailing-only pattern
#: would leave the base as `timestamp(3) without time zone`, miss the mapping,
#: and fall through to the uppercase path -- producing a plausible-looking wrong
#: answer instead of an obviously wrong one. Chinook has no such column, so this
#: is a case reasoned about rather than observed, and it is tested with a
#: constructed input.
_MODIFIER = re.compile(r"\(([^)]*)\)")


def split_catalog_type(catalog_type: str) -> tuple[str, tuple[str, ...]]:
    """Split a `format_type()` string into its base name and its modifiers.

    ``"numeric(10,2)"`` becomes ``("numeric", ("10", "2"))`` and
    ``"timestamp(3) without time zone"`` becomes
    ``("timestamp without time zone", ("3",))``.
    """
    match = _MODIFIER.search(catalog_type)
    if match is None:
        return " ".join(catalog_type.split()), ()

    without_modifier = catalog_type[: match.start()] + catalog_type[match.end() :]
    base = " ".join(without_modifier.split())
    modifiers = tuple(part.strip() for part in match.group(1).split(",") if part.strip())
    return base, modifiers


def sqlalchemy_spelling(catalog_type: str) -> str:
    """Render `catalog_type` the way SQLAlchemy's type compiler rendered it.

    Args:
        catalog_type: the output of ``format_type(a.atttypid, a.atttypmod)``.

    Returns:
        The spelling the `Inspector` produced for the same column, for the five
        families in :data:`BASE_SPELLINGS`. For anything else, the base name
        uppercased with its modifiers reattached -- correct for `text`,
        `boolean`, `date`, `uuid`, `jsonb` and the other single-word types, and
        **not verified**, because Chinook cannot exercise it. `character(N)` is
        a known divergence in that set: SQLAlchemy renders `CHAR(N)`.

    Never raises. A schema that fails to render is a schema the agent cannot
    see, and refusing an unrecognised type would take the whole database down
    over one column -- so an unknown type degrades to a readable approximation
    rather than to an outage. The cost is stated above rather than hidden.
    """
    base, modifiers = split_catalog_type(catalog_type)
    name = BASE_SPELLINGS.get(base, base.upper())
    if not modifiers:
        return name
    # ", " and not ",": SQLAlchemy's separator, and the whole reason
    # `NUMERIC(10, 2)` still hashes to the fingerprint EVALS.md was recorded
    # against.
    return f"{name}({', '.join(modifiers)})"
