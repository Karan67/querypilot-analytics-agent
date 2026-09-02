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

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Inspector
from sqlalchemy.exc import SQLAlchemyError

from api.db.engine import get_engine

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
    would flatten both to ``character varying`` / ``numeric`` (AC5). Computed
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


def _build_relation(inspector: Inspector, name: str, kind: str) -> Table:
    """Map one relation from the inspector's loose dicts into the contract."""
    pk = inspector.get_pk_constraint(name, schema=SCHEMA_NAME)
    # A view yields {"constrained_columns": [], "name": None}, and the whole
    # dict can be absent. Both degrade to "no primary key" rather than raising
    # (AC11) — code that indexes into constrained_columns crashes on views.
    pk_columns = frozenset((pk or {}).get("constrained_columns") or ())

    columns = tuple(
        Column(
            name=col["name"],
            type=str(col["type"]),
            nullable=bool(col["nullable"]),
            # Membership against the whole key. Testing only [0] is exactly the
            # composite-key bug AC7 exists to catch.
            primary_key=col["name"] in pk_columns,
        )
        for col in inspector.get_columns(name, schema=SCHEMA_NAME)
    )

    foreign_keys = tuple(
        sorted(
            (
                ForeignKey(
                    columns=tuple(fk["constrained_columns"]),
                    referred_table=fk["referred_table"],
                    referred_columns=tuple(fk["referred_columns"]),
                )
                for fk in inspector.get_foreign_keys(name, schema=SCHEMA_NAME) or ()
            ),
            # Inspector does not document a stable order for foreign keys, so
            # sort explicitly. AC13 would otherwise be flaky rather than false.
            key=lambda fk: (fk.columns, fk.referred_table, fk.referred_columns),
        )
    )

    return Table(name=name, kind=kind, columns=columns, foreign_keys=foreign_keys)


def get_schema() -> Schema:
    """Return the structural map of the target database.

    Takes no arguments (AC14). The schema name is module configuration, not a
    parameter, so nothing supplied by a user or a model can reach an
    identifier — this is what makes the tool injection-free, and a future
    signature of ``get_schema(schema_name)`` would forfeit it.

    Runs over the read-only engine and reads catalog metadata only; it selects
    no rows from any user relation (AC16).

    Raises:
        SchemaIntrospectionError: if the catalog cannot be read, or if it comes
            back empty. See AC17 — an empty map is never returned as success.
    """
    try:
        inspector = sa_inspect(get_engine())
        # Two calls rather than one filtered query: get_table_names() returns
        # base tables only and views arrive separately. There is no table_type
        # column to switch on here, so `kind` is assigned at merge time rather
        # than read off a catalog row (AC2).
        #
        # get_materialized_view_names() is deliberately NOT called. Matviews are
        # a non-goal for Iteration 1 (spec §4); leaving them out is a decision,
        # not an omission.
        relations = [
            _build_relation(inspector, name, kind)
            for kind, names in (
                (KIND_TABLE, inspector.get_table_names(schema=SCHEMA_NAME)),
                (KIND_VIEW, inspector.get_view_names(schema=SCHEMA_NAME)),
            )
            for name in names
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
        # SQLAlchemyError: refused connection, auth failure, timeout.
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
