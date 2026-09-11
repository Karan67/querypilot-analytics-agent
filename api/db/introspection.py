"""Structural introspection of the target database.

Implements the schema tool specified in `specs/001-schema-tool.md`. Per the
convention in `specs/000-project.md` §5, all real logic lives here and
`api/agent/tools.py` holds only a thin registered wrapper that delegates to it.

Everything below is frozen and tuple-based, for two reasons: a caller cannot
mutate the schema map it was handed, and equality is structural — which is what
AC13 (determinism) is asserted with.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError

from api.db.execution import MAX_ROWS, execute_sql
from api.db.type_names import sqlalchemy_spelling

#: Values for `Table.kind` (AC2). Constants rather than bare literals so a typo
#: is an AttributeError at import time instead of a silently wrong comparison.
KIND_TABLE = "table"
KIND_VIEW = "view"

#: The single schema this project reads. Fixed here rather than accepted as a
#: parameter — see AC14 and the docstring of `get_schema()`.
SCHEMA_NAME = "public"


class SchemaIntrospectionError(RuntimeError):
    """Raised when the database catalog cannot be read.

    Deliberately distinct from SQLAlchemy's exception hierarchy so a caller can
    tell "the catalog is unavailable" from "a query failed".

    Per AC17 this is raised rather than returning an empty `Schema`. A database
    with genuinely no relations is indistinguishable from a broken
    introspection, and guessing wrong is not a symmetric mistake: an empty map
    returned as success makes the model hallucinate an entire database, and the
    fault then surfaces as bad SQL rather than as an outage.
    """


@dataclass(frozen=True)
class Column:
    """One column of a table or a view.

    `type` is SQLAlchemy's rendering — ``VARCHAR(200)``, ``NUMERIC(10, 2)`` —
    which preserves length and precision where `information_schema.data_type`
    would flatten both to ``character varying`` / ``numeric`` (AC5). Since
    B-10 it is produced by `api/db/type_names.py` from `format_type()` rather
    than by SQLAlchemy itself, **byte-identically**, because this string is
    hashed into the prompt fingerprint every `EVALS.md` entry carries. Computed
    view columns genuinely have no declared precision and render bare
    (``NUMERIC``, ``BIGINT``); that is the catalog's answer and is never
    synthesised here.

    `nullable` is only meaningful when the parent relation is a table. Postgres
    does not propagate ``NOT NULL`` through a view, so every view column reports
    ``True`` regardless of the underlying column (AC4).

    `primary_key` is ``True`` for *every* column of a composite key, not just
    the first (AC7), and always ``False`` on views, which have none (AC11).
    """

    name: str
    type: str
    nullable: bool
    primary_key: bool


@dataclass(frozen=True)
class ForeignKey:
    """One foreign key constraint.

    `columns` and `referred_columns` are tuples because composite keys are real
    and because tuples make the containing `Table` hashable and comparable.

    A self-referencing key — ``employee.reports_to → employee.employee_id`` — is
    an ordinary instance whose `referred_table` equals the owning table's name.
    It needs no special case (AC10); the acceptance criterion exists to stop
    someone filtering these out later, not because the mapping is hard.
    """

    columns: tuple[str, ...]
    referred_table: str
    referred_columns: tuple[str, ...]


@dataclass(frozen=True)
class Table:
    """One relation: a base table or a view (AC2).

    `columns` are in ordinal order as declared, never alphabetical (AC3).
    `foreign_keys` is always empty for views (AC11).
    """

    name: str
    kind: str
    columns: tuple[Column, ...]
    foreign_keys: tuple[ForeignKey, ...]


@dataclass(frozen=True)
class Schema:
    """The structural map of one database schema.

    `tables` is sorted alphabetically by name with kinds interleaved, not
    grouped tables-then-views (AC1). The ordering is explicit rather than
    incidental: AC13 and eval reproducibility both depend on it.
    """

    tables: tuple[Table, ...]


#: Relations in the target schema, and which kind each is (AC2).
#:
#: `relkind IN ('r', 'v')` and not `'m'`: materialized views were a declared
#: non-goal at Iteration 1 (`001-schema-tool.md` section 4), and the
#: `Inspector` path did not call `get_materialized_view_names()` either. Keeping
#: the omission is what makes this replacement byte-identical rather than
#: merely equivalent.
RELATIONS_SQL = """
SELECT c.relname, c.relkind::text
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public'
   AND c.relkind IN ('r', 'v')
"""

#: Every column of every relation, with its declared type, nullability and
#: primary-key membership.
#:
#: `format_type(atttypid, atttypmod)` is the one function that preserves length
#: and precision -- `information_schema.columns.data_type` flattens
#: `character varying(200)` to `character varying` and `numeric(10,2)` to
#: `numeric`, which is AC5's whole subject. `api/db/type_names.py` then converts
#: the spelling.
#:
#: `attnum` comes back so ordinal order can be restored in Python (AC3) without
#: an `ORDER BY`: row order is not a property of a schema, and the code should
#: not depend on the server supplying one. (The retired `schema_cache.py` reached
#: the same conclusion about its probe, and sorted in Python for the added reason
#: that `ORDER BY` would have made a hash depend on the server's collation.)
#:
#: The primary key arrives as a `CASE` over `pk.conkey` rather than as a second
#: query, because membership must be tested against the **whole** key --
#: `attnum = ANY (pk.conkey)`, not `conkey[1]`. Testing only the first element
#: is exactly the composite-key bug AC7 exists to catch, and `playlist_track`
#: would expose it. The `LEFT JOIN` is what makes views work: they have no
#: primary-key constraint, so `conkey` is NULL and every column reports `f`
#: (AC11).
#:
#: `relkind` and `attnotnull` are cast to text explicitly, because
#: `text || "char"` is ambiguous in PostgreSQL and fails at *execution* rather
#: than at parse time, so it passes Gate 2 and then falls over. This was first
#: paid for in `schema_cache.py`, retired at Iteration 9 T4; the lesson outlived
#: the module.
COLUMNS_SQL = """
SELECT c.relname,
       a.attname,
       a.attnum,
       format_type(a.atttypid, a.atttypmod),
       a.attnotnull::text,
       CASE WHEN pk.conkey IS NULL THEN 'f'
            WHEN a.attnum = ANY (pk.conkey) THEN 't'
            ELSE 'f' END
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid
  LEFT JOIN pg_constraint pk
         ON pk.conrelid = c.oid AND pk.contype = 'p'
 WHERE n.nspname = 'public'
   AND c.relkind IN ('r', 'v')
   AND a.attnum > 0
   AND NOT a.attisdropped
"""

#: Foreign keys, one row per constrained column.
#:
#: `unnest(conkey, confkey) WITH ORDINALITY` is the load-bearing part. The two
#: arrays are parallel and their **order is the constraint's** -- a composite
#: key on `(a, b)` referencing `(x, y)` means `a -> x` and `b -> y`, and pairing
#: them by position is the only way to keep that. Unnesting them separately
#: would produce a cross product; joining `attnum` back without ordinality would
#: return them in whatever order the catalog scan yields. Chinook has no
#: composite foreign key, so this is correctness reasoned about rather than
#: observed here, and `tests/test_type_names.py` covers the pairing.
FOREIGN_KEYS_SQL = """
SELECT src.relname,
       con.conname,
       keys.ord,
       src_att.attname,
       tgt.relname,
       tgt_att.attname
  FROM pg_constraint con
  JOIN pg_class src ON src.oid = con.conrelid
  JOIN pg_class tgt ON tgt.oid = con.confrelid
  JOIN pg_namespace n ON n.oid = con.connamespace
  CROSS JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY
       AS keys(src_attnum, tgt_attnum, ord)
  JOIN pg_attribute src_att
    ON src_att.attrelid = con.conrelid AND src_att.attnum = keys.src_attnum
  JOIN pg_attribute tgt_att
    ON tgt_att.attrelid = con.confrelid AND tgt_att.attnum = keys.tgt_attnum
 WHERE con.contype = 'f'
   AND n.nspname = 'public'
   AND con.conname IS NOT NULL
"""

#: `relkind` values, mapped to the `Table.kind` contract.
_KINDS = {"r": KIND_TABLE, "v": KIND_VIEW}


def _rows(sql: str, what: str) -> tuple[tuple, ...]:
    """Run one catalog query through Gate 2, or explain why the schema is unknown.

    **Truncation is a failure here, and it is a new one.** The `Inspector` had
    no row cap; `execute_sql()` applies Gate 3's `MAX_ROWS`, so a schema with
    more than a thousand columns would come back as a *prefix*. Returning that
    would be the worst available outcome -- a schema that looks complete, is
    stable across calls, and silently omits relations the model then cannot
    query. The retired `schema_cache.py` reached the same conclusion about its
    probe: a signature it cannot verify is never trusted.

    So this raises, and the message says which query and what the cap is,
    because the fix is a different introspection strategy rather than a retry.
    """
    result = execute_sql(sql)

    if not result.ok:
        raise SchemaIntrospectionError(
            f"Could not read {what} of '{SCHEMA_NAME}': {result.error}"
        )

    if result.truncated:
        raise SchemaIntrospectionError(
            f"Reading {what} of '{SCHEMA_NAME}' returned more than "
            f"{MAX_ROWS:,} rows and was truncated by Gate 3. A partial schema "
            f"is worse than none: it is stable, it looks complete, and it hides "
            f"relations the model will then never query. This database needs an "
            f"introspection strategy that pages."
        )

    return result.rows


def get_schema() -> Schema:
    """Return the structural map of the target database.

    Takes no arguments (AC14). The schema name is module configuration, not a
    parameter, so nothing supplied by a user or a model can reach an
    identifier — this is what makes the tool injection-free, and a future
    signature of ``get_schema(schema_name)`` would forfeit it.

    Reads catalog metadata only; it selects no rows from any user relation
    (AC16).

    **Three statements, through `execute_sql()`** (B-10, Iteration 8 T5).
    Until now this function used SQLAlchemy's `Inspector`, which asked the
    database per relation — columns, primary key, foreign keys, kind, for each
    of twelve relations — at **52 statements and 99ms a call**, and did so over
    a raw connection that never passed Gate 2. Charter §4 says every database
    read goes through `execute_sql()` and has exactly one recorded exemption;
    this was not it, it was a hole, and it had been open since Iteration 1.

    The output is **byte-identical** to what the `Inspector` produced, which is
    AC9 and is not a courtesy: the rendered schema is hashed into the prompt
    fingerprint that every `EVALS.md` entry carries, so anything else would
    retire eight iterations of comparable measurements.

    Raises:
        SchemaIntrospectionError: if the catalog cannot be read, if a query is
            truncated by Gate 3, or if it comes back empty. See AC17 — an empty
            map is never returned as success.
    """
    try:
        relation_kinds = {
            str(name): _KINDS[str(relkind)]
            for name, relkind in _rows(RELATIONS_SQL, "the relation list")
            if str(relkind) in _KINDS
        }

        # name -> attnum -> Column, so ordinal order is restored by sorting the
        # keys rather than by trusting the order rows arrived in.
        columns: dict[str, dict[int, Column]] = {name: {} for name in relation_kinds}
        for relname, attname, attnum, catalog_type, notnull, is_pk in _rows(
            COLUMNS_SQL, "the column list"
        ):
            if str(relname) not in columns:
                # A relation whose kind this module does not report. Skipped
                # rather than raising: the relation list is the authority on
                # what is in the schema, and disagreeing with it here would
                # make the two queries' results depend on each other.
                continue
            columns[str(relname)][int(attnum)] = Column(
                name=str(attname),
                type=sqlalchemy_spelling(str(catalog_type)),
                nullable=str(notnull) != "true",
                primary_key=str(is_pk) == "t",
            )

        foreign_keys: dict[str, list[tuple[str, int, str, str, str]]] = {
            name: [] for name in relation_kinds
        }
        for relname, conname, ordinal, column, referred_table, referred_column in _rows(
            FOREIGN_KEYS_SQL, "the foreign keys"
        ):
            if str(relname) not in foreign_keys:
                continue
            foreign_keys[str(relname)].append(
                (
                    str(conname),
                    int(ordinal),
                    str(column),
                    str(referred_table),
                    str(referred_column),
                )
            )

        relations = [
            Table(
                name=name,
                kind=kind,
                columns=tuple(
                    columns[name][attnum] for attnum in sorted(columns[name])
                ),
                foreign_keys=_group_foreign_keys(foreign_keys[name]),
            )
            for name, kind in relation_kinds.items()
        ]

        # Sorted after the merge, so kinds interleave alphabetically instead of
        # grouping tables-then-views (AC1). Explicit rather than inherited from
        # the catalog, because AC13 and eval reproducibility depend on it.
        tables = tuple(sorted(relations, key=lambda relation: relation.name))
    except SchemaIntrospectionError:
        # Ours already — do not re-wrap. SchemaIntrospectionError subclasses
        # RuntimeError, so without this it would be caught by the clause below.
        raise
    except (SQLAlchemyError, RuntimeError) as exc:
        # SQLAlchemyError: retained because `execute_sql()` reports failures as
        # return values rather than exceptions, but `get_engine()` still raises.
        # RuntimeError: get_engine() with QUERYPILOT_DATABASE_URL unset.
        # Chained so the original Postgres text survives in the traceback — the
        # agent reads this message from Iteration 4, and "could not connect to
        # server" is actionable where "schema error" is not.
        raise SchemaIntrospectionError(
            f"Could not read the schema of '{SCHEMA_NAME}': {exc}"
        ) from exc

    if not tables:
        raise SchemaIntrospectionError(
            f"Schema '{SCHEMA_NAME}' contains no relations. Refusing to return an "
            f"empty schema: a database with genuinely no tables is "
            f"indistinguishable from a failed introspection, and returning "
            f"nothing would make the model invent one."
        )

    return Schema(tables=tables)


def _group_foreign_keys(
    rows: list[tuple[str, int, str, str, str]],
) -> tuple[ForeignKey, ...]:
    """Collapse per-column rows into one `ForeignKey` per constraint.

    **Grouped by constraint name, which is the only correct key.** The first
    draft of this grouped by *referred table*, and that is wrong in a way
    Chinook cannot show: two separate single-column keys onto the same table are
    two constraints, and grouping them by target would fuse them into one
    bogus composite key. `track` has three keys onto three different tables, so
    it passes either way -- which is precisely why the mistake would have
    survived byte-identity against these fixtures.

    Within a constraint the columns are ordered by the constraint's own
    ordinality, which is what makes a composite key mean `(a, b) -> (x, y)`
    rather than an unordered pair of pairs.

    Sorted by `(columns, referred_table, referred_columns)`, reproducing the
    `Inspector` path's explicit sort exactly. That sort existed because the
    `Inspector` documented no stable order; here the reason is the same and the
    source is different, and AC13 would be flaky rather than false without it.
    """
    by_constraint: dict[str, list[tuple[int, str, str, str]]] = {}
    for conname, ordinal, column, referred_table, referred_column in rows:
        by_constraint.setdefault(conname, []).append(
            (ordinal, column, referred_table, referred_column)
        )

    keys = []
    for members in by_constraint.values():
        ordered = sorted(members)
        keys.append(
            ForeignKey(
                columns=tuple(column for _, column, _, _ in ordered),
                referred_table=ordered[0][2],
                referred_columns=tuple(referred for _, _, _, referred in ordered),
            )
        )

    return tuple(
        sorted(
            keys,
            key=lambda fk: (fk.columns, fk.referred_table, fk.referred_columns),
        )
    )
