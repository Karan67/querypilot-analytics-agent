"""Row sampling — showing the agent what the data actually looks like.

Implements `specs/004-sample-rows.md`. Per `specs/000-project.md` §5 the logic
lives here and `api/agent/tools.py` holds only a registered wrapper.

A schema says `customer.country` is `VARCHAR(40)`. It does not say whether the
values are ``USA``, ``United States`` or ``us``. Getting that wrong produces a
query that is syntactically perfect, passes every gate, executes cleanly, and
returns zero rows -- the worst failure mode in the project, because nothing
reports an error and the agent states "none" with confidence.

**This module solves an input problem no other tool has.** `get_schema()` takes
nothing; `validate_sql()` and `execute_sql()` take a whole statement and put it
through the AST gate. This one takes a bare *identifier*, which cannot be parsed
as a statement and — because PostgreSQL accepts no bind parameter in place of an
identifier — cannot be parameterised either. Two layers answer that:

1. **An allowlist** built from `get_schema()`. Load-bearing, not decorative:
   `pg_stat_activity`, `pg_class`, `pg_roles` and `information_schema.tables`
   are all readable by `querypilot_ro` and all pass Gate 2 as ordinary SELECTs.
   Nothing else stops them.
2. **AST construction**, never string assembly. See `_build_sample_query`.

**Sampled values are untrusted input.** This is the first tool that puts database
*content* into the model's context, which is the content-borne injection threat
in `specs/002-sql-validation.md` §2 becoming live. The defence is unchanged --
Gate 2 does not care how persuaded the model is -- but Iteration 5's prompt
design must treat these values as data, never as instructions.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlglot import exp

from api.db.execution import CATEGORY_CONNECTION_ERROR, ExecutionResult, execute_sql
from api.db.introspection import SchemaIntrospectionError, Table, get_schema

# The same dialect constant Gate 2 parses with, imported rather than repeated.
# The point of building queries with sqlglot is that one engine both generates
# and validates them, so a divergence between the two is impossible; a second
# copy of "postgres" here would quietly reintroduce that possibility.
from api.safety.validator import DIALECT

#: Rows returned per call (AC16, resolved Q-A).
#:
#: Three is enough to show a value's shape and casing while keeping the prompt
#: compact. Revisit in Iteration 5 if failed evals say it was not enough.
SAMPLE_ROW_COUNT = 3

#: How many valid relation names a rejection may list (AC5, resolved Q-D).
#:
#: Covers Chinook's 12 entirely. The cap exists for schemas with hundreds of
#: tables, where an unbounded list would crowd the schema the agent actually
#: needs out of its context.
MAX_LISTED_RELATIONS = 25

#: The one failure this tool can produce that `execute_sql()` cannot.
#:
#: Separate from a SQL error because the agent's fix differs: choose a different
#: relation, rather than rewrite a query.
CATEGORY_UNKNOWN_RELATION = "unknown_relation"


def _order_columns(table: Table) -> tuple[str, ...]:
    """Columns to sort by, so repeated calls agree (AC14, AC15).

    Determinism matters more here than it looks. Iteration 5 puts sampled values
    into the prompt, so if samples vary between runs, two eval runs differ for
    reasons unrelated to the change under test and an accuracy delta stops being
    attributable -- which is the whole premise of `EVALS.md`.

    `LIMIT` without `ORDER BY` happens to be stable on a sequential scan
    (measured: identical rows on three consecutive runs) but nothing guarantees
    it; physical order shifts after `UPDATE` or `VACUUM`.

    Primary key when there is one -- every column of a composite key, which
    `get_schema()` already supplies. Otherwise every column, which is how a view
    like `invoice_totals` stays deterministic (resolved Q-B). The sort is bounded
    by Gate 3's statement timeout.
    """
    primary_key = tuple(column.name for column in table.columns if column.primary_key)
    if primary_key:
        return primary_key
    return tuple(column.name for column in table.columns)


def _build_sample_query(relation: str, order_columns: Sequence[str]) -> str:
    """Build the sampling query as a syntax tree and generate SQL from it.

    **No string assembly anywhere** (AC7-AC9). Not an f-string, not `%`, not
    `.format()`, not concatenation. The identifier never exists inside a string
    that is later parsed -- it is a node, and sqlglot renders the whole statement
    around it.

    That is stronger than quoting a name and interpolating it into a template.
    Quoting has to be *called*; construction cannot be skipped. And because
    sqlglot both builds this and validates it at Gate 2, the two cannot disagree
    about what the result means -- the parser-divergence class of bug that
    `002` had to test PostgreSQL and sqlglot against each other to rule out.

    Containment is provable by round trip: build with the hostile relation name
    ``track"; DROP TABLE track; --`` and re-parse, and the parser reports one
    table whose name is exactly that literal string. The ``DROP TABLE`` is inside
    an identifier, not a statement.
    """
    query = exp.select(exp.Star()).from_(exp.to_identifier(relation, quoted=True))

    if order_columns:
        query = query.order_by(
            *[exp.to_identifier(column, quoted=True) for column in order_columns]
        )

    return query.limit(SAMPLE_ROW_COUNT).sql(dialect=DIALECT)


def _unknown(table_name: object, known: Sequence[str]) -> ExecutionResult:
    """Reject a relation that is not in the allowlist (AC5).

    Lists the valid names so the call is self-correcting, capped at
    MAX_LISTED_RELATIONS with the omitted count stated (resolved Q-D). The list
    is worth its tokens because the agent may not have called `get_schema()`
    yet; the cap is worth having because on a schema with hundreds of tables an
    unbounded list would crowd out the schema it actually needs.
    """
    shown = list(known)[:MAX_LISTED_RELATIONS]
    listing = ", ".join(shown)
    omitted = len(known) - len(shown)
    if omitted > 0:
        listing = f"{listing}, ... and {omitted} more"

    return ExecutionResult(
        ok=False,
        category=CATEGORY_UNKNOWN_RELATION,
        error=(
            f"Unknown relation {table_name!r}. Sampling is limited to relations "
            f"in the target schema"
            + (f": {listing}." if listing else ".")
        ),
    )


def sample_rows(table_name: str) -> ExecutionResult:
    """Return a few example rows from one known relation.

    Args:
        table_name: must match a relation reported by `get_schema()` **exactly**.

    Returns:
        The `ExecutionResult` from `execute_sql()` unchanged, or an
        `unknown_relation` failure. Never raises (AC17).

    The allowlist is checked before any SQL is constructed (AC6) -- there is no
    moment where an unvalidated identifier exists inside a query -- and it is
    the only thing preventing a system catalog from being sampled: measured,
    `pg_stat_activity`, `pg_class`, `pg_roles` and `information_schema.tables`
    are all readable by the read-only role and all pass Gate 2 as ordinary
    SELECTs.
    """
    try:
        schema = get_schema()
    except SchemaIntrospectionError as exc:
        # Without the catalog there is no allowlist, so there is no safe way to
        # continue -- but AC17 says return, never raise.
        return ExecutionResult(
            ok=False,
            category=CATEGORY_CONNECTION_ERROR,
            error=(
                "Could not read the schema, so the relation could not be "
                f"checked against it: {exc}"
            ),
        )

    relations = {table.name: table for table in schema.tables}

    # Exact comparison. No .lower(), no .strip(): PostgreSQL permits `track` and
    # `Track` as distinct relations, and folding would let one be reached by
    # asking for the other (AC2).
    if not isinstance(table_name, str) or table_name not in relations:
        return _unknown(table_name, tuple(relations))

    table = relations[table_name]
    query = _build_sample_query(table_name, _order_columns(table))

    # Through execute_sql, never the engine (AC10). Gate 2, the read-only
    # transaction, SET LOCAL statement_timeout, server-side streaming and the
    # row cap are all inherited rather than reimplemented here.
    return execute_sql(query)
