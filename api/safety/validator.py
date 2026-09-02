"""Gate 2 of the safety layer: static validation of candidate SQL.

Implements `specs/002-sql-validation.md`. Per `specs/000-project.md` §5 the real
logic lives here and `api/agent/tools.py` holds only a registered wrapper.

**Parsing, never pattern-matching.** A regex keyword blocklist is defeated by
casing, comments, whitespace, string splitting and encoding; shipping one would
be worse than shipping nothing, because it would look like protection. Every
claim this project makes about blocking DDL/DML rests on this module operating
on a parse tree.

This module is a pure function of its input string. It opens no connection and
reads no catalog (AC20), so its tests need no fixture.

Gates run cheapest-first::

    length  →  parse  →  statement count  →  root allow-list  →  tree deny-list
     AC27       AC5         AC3, AC4        AC1, AC25, AC26   AC6-AC11, AC14-15

**Fail closed.** Anything not positively established as a single read-only
query is rejected. The root check is an allow-list, not a blocklist, so
statement types nobody anticipated are refused by default rather than admitted
by oversight.
"""

from __future__ import annotations

# `re` is used in exactly one place: tidying sqlglot's own error text before it
# reaches the agent. It never touches the SQL, and it never participates in a
# validation decision. If a regex ever appears in a code path that decides
# whether a query is safe, that is the failure this module exists to prevent.
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

#: Parse as PostgreSQL. Not optional: Postgres-specific syntax misparses under
#: sqlglot's default dialect, and a misparse is a silent hole rather than a
#: visible failure.
DIALECT = "postgres"

#: Maximum accepted input length, in characters (AC27, resolved Q-D).
#:
#: A denial-of-service guard, checked before parsing because it costs one
#: comparison where parsing an enormous or deeply nested string does not. Far
#: above anything the eval suite produces — if this ever fires on legitimate
#: work, that is a signal worth investigating rather than a limit worth raising.
MAX_SQL_LENGTH = 10_000

#: Root allow-list (AC1). Closed by construction: anything not positively an
#: allowed query type is refused, so nothing needs to be anticipated.
#:
#: Two entries covers set operations too — `exp.Union`, `exp.Intersect` and
#: `exp.Except` all subclass `exp.SetOperation` (AC25), so a set operation
#: sqlglot adds later is admitted without an edit here.
ALLOWED_ROOTS: tuple[type[exp.Expression], ...] = (exp.Select, exp.SetOperation)

#: What may appear as a branch of a set operation (AC25).
#:
#: Wider than ALLOWED_ROOTS, and deliberately so. Measured against sqlglot
#: 27.29.0, the branches of legitimate read-only set operations come back as::
#:
#:     SELECT 1 UNION SELECT 2            -> Select,    Select
#:     (SELECT 1) UNION (SELECT 2)        -> Subquery,  Subquery
#:     SELECT 1 UNION SELECT 2 UNION ...  -> Union,     Select
#:     SELECT 1 UNION VALUES (1)          -> Select,    Values
#:
#: Restricting branches to `Select`/`SetOperation` -- the obvious reading of
#: "each branch must itself be a query" -- would reject the last two. `VALUES`
#: and a parenthesised query are both read-only and both legitimate, and
#: over-blocking is as much a defect here as under-blocking (AC18).
ALLOWED_BRANCHES: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.SetOperation,
    exp.Subquery,
    exp.Values,
)

#: Tree deny-list (AC6-AC11, AC14, AC15), applied to every node.
#:
#: **This must stay explicitly enumerated.** The obvious implementation --
#: `isinstance(node, (exp.DML, exp.DDL, exp.Command))` -- is wrong and looks
#: right: measured against sqlglot 27.29.0, `exp.Drop`, `exp.Alter`,
#: `exp.TruncateTable`, `exp.Set` and `exp.Grant` belong to *none* of those
#: families. The root allow-list happens to catch those five when they are the
#: whole statement, so relying on base classes degrades defence in depth rather
#: than opening a hole -- but it would become a hole the moment the allow-list
#: widens.
#:
#: The base classes stay in the tuple because they are correct as far as they
#: go and they carry future subclasses. AC24 is what stops this list silently
#: falling behind sqlglot.
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    # Base families: Insert, Update, Delete, Merge, Copy, Create, ...
    exp.DML,
    exp.DDL,
    exp.Command,
    # In none of the families above. Verified, not assumed -- and the list
    # grew during T3 when REVOKE turned out to be another one, which is exactly
    # the drift AC24 exists to catch.
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Set,
    exp.Grant,
    exp.Revoke,
    # SELECT ... INTO creates a table (AC11). Reachable from walk(), so it needs
    # no special-case inspection of the root's `into` argument.
    exp.Into,
)

#: Readable names for reasons (AC21). `type(node).__name__` gives
#: "TruncateTable" and "Into", which is not what the model needs to read back.
_NODE_LABELS: dict[type[exp.Expression], str] = {
    exp.Insert: "INSERT",
    exp.Update: "UPDATE",
    exp.Delete: "DELETE",
    exp.Merge: "MERGE",
    exp.Create: "CREATE",
    exp.Drop: "DROP",
    exp.Alter: "ALTER",
    exp.TruncateTable: "TRUNCATE",
    exp.Grant: "GRANT",
    exp.Revoke: "REVOKE",
    exp.Set: "SET",
    exp.Copy: "COPY",
    exp.Into: "SELECT ... INTO",
    exp.Command: "an unsupported or non-query statement",
}


def _label(node: exp.Expression) -> str:
    """Name a node for a rejection reason, preferring the most specific name.

    `exp.Command` is sqlglot's fallback for syntax it does not model, and it
    covers a lot of ground -- SET ROLE, VACUUM, DO, CALL, EXPLAIN all land here.
    It keeps the leading keyword in `this`, so the reason can say "SET" instead
    of "an unsupported statement". That matters for AC21: the agent retries on
    this text, and a generic label tells it nothing about what to change.
    """
    if isinstance(node, exp.Command):
        keyword = str(node.this).strip().upper() if node.this else ""
        return keyword or _NODE_LABELS[exp.Command]

    exact = _NODE_LABELS.get(type(node))
    if exact is not None:
        return exact
    for cls, label in _NODE_LABELS.items():
        if isinstance(node, cls):
            return label
    return type(node).__name__.upper()


def _first_disallowed_branch(node: exp.Expression) -> exp.Expression | None:
    """Depth-first scan of a set operation's branches (AC25).

    Returns the first branch that is not an allowed query form, or None. The
    scan descends through nested set operations and unwraps parenthesised
    branches, so the allow-list applies at every level rather than only at the
    root -- `(SELECT 1 UNION SELECT 2) UNION (…)` is checked all the way down.

    **This is a backstop, not the primary defence.** The tree walk in
    `validate_sql` already descends into set-operation branches, so in practice
    it rejects a smuggled write first and with a better reason. This check
    exists so that the structural guarantee survives the walk being narrowed,
    or sqlglot changing what `walk()` traverses -- on the one module where the
    project's safety claims live, that is worth a dozen lines.
    """
    if isinstance(node, exp.Subquery):
        inner = node.this
        return _first_disallowed_branch(inner) if inner is not None else None

    if not isinstance(node, exp.SetOperation):
        return None

    for side in (node.this, node.expression):
        if side is None:
            continue
        if not isinstance(side, ALLOWED_BRANCHES):
            return side
        deeper = _first_disallowed_branch(side)
        if deeper is not None:
            return deeper

    return None


#: Matches a Python class repr that sqlglot embeds in some parse errors, e.g.
#: "<class 'sqlglot.expressions.Add'>". Useful to a library author, noise to the
#: model that has to act on the message.
_CLASS_REPR = re.compile(r"<class '(?:[\w.]+\.)?(\w+)'>")


def _parse_error_summary(exc: SqlglotError) -> str:
    """Condense a sqlglot error to one useful line.

    Keeps sqlglot's position information -- "Expected table name but got None.
    Line 1, Col: 11" is actionable where "invalid syntax" is not -- but strips
    the internal class reprs, so "Required keyword: 'expression' missing for
    <class 'sqlglot.expressions.Add'>" reads as "... missing for Add".

    The agent reads this from Iteration 4 and retries against it (AC21), so
    what survives here is what it has to work with.
    """
    text = str(exc).strip()
    first_line = text.splitlines()[0] if text else ""
    return _CLASS_REPR.sub(r"\1", first_line) or exc.__class__.__name__


def validate_sql(sql: str) -> tuple[bool, str]:
    """Validate a candidate query against Gate 2.

    Args:
        sql: the candidate statement, as generated by the model.

    Returns:
        ``(True, "")`` if `sql` is exactly one read-only SELECT.
        ``(False, reason)`` otherwise, where `reason` states what was wrong in
        terms the agent can act on (AC21). Reasons are deliberately detailed:
        the consumer is our own model inside our own retry loop, and a vague
        reason produces an identical retry.

    Never raises (AC22). Hostile input — malformed, enormous, deeply nested,
    binary garbage — returns a rejection rather than propagating, because a
    crash in the validator is a denial of service on the whole API.
    """
    if not isinstance(sql, str):
        return False, f"Expected SQL as a string, got {type(sql).__name__}."

    # --- AC27: length, before anything expensive --------------------------
    if len(sql) > MAX_SQL_LENGTH:
        return False, (
            f"Query is {len(sql):,} characters; the limit is "
            f"{MAX_SQL_LENGTH:,}."
        )

    # --- AC5: parse, failing closed ---------------------------------------
    try:
        parsed = sqlglot.parse(sql, dialect=DIALECT)
    except SqlglotError as exc:
        # SqlglotError, not ParseError. The tokenizer raises TokenError — a
        # *sibling* of ParseError, not a subclass — for things like an
        # unterminated string literal, so catching ParseError alone lets
        # `'unterminated` escape as an exception and breaks AC22. Found by the
        # AC22 hostile-input test, not by reading the docs.
        #
        # Note this is the opposite conclusion to the *expression* taxonomy in
        # §5 of the spec, where base classes were insufficient and the deny-list
        # had to be enumerated. Here the base class is right: every error
        # sqlglot raises while parsing means the same thing to us — we could not
        # establish what this string is, so we refuse it. Still a named,
        # library-scoped exception, never a bare `except Exception`, which would
        # swallow real bugs in this module and report them as invalid SQL.
        return False, f"Could not parse as PostgreSQL: {_parse_error_summary(exc)}"
    except RecursionError:
        # Deeply nested input exhausts the parser's stack. Caught by name
        # rather than with a bare `except Exception`, which would swallow real
        # bugs in this module and report them to the agent as invalid SQL.
        return False, "Query is nested too deeply to parse."

    # sqlglot yields None for an empty statement, so `SELECT 1;;` comes back as
    # two entries and `""` as one. Filter before counting, or a harmless
    # trailing semicolon reads as a multi-statement payload (AC2).
    statements = [statement for statement in parsed if statement is not None]

    # --- AC4: nothing to run ----------------------------------------------
    if not statements:
        return False, (
            "No SQL statement found. Empty input, whitespace, and comment-only "
            "input are not queries."
        )

    # --- AC3: exactly one statement ---------------------------------------
    if len(statements) > 1:
        kinds = ", ".join(type(statement).__name__.upper() for statement in statements)
        return False, (
            f"Expected exactly one statement, found {len(statements)} ({kinds}). "
            f"Only a single SELECT may be executed; stacked statements are "
            f"never permitted, including when separated by comments."
        )

    statement = statements[0]

    # --- AC1: root allow-list, the primary defence -------------------------
    # Closed by construction. Rejects DROP, ALTER, TRUNCATE, SET, GRANT, COPY,
    # VACUUM, DO and EXPLAIN when they are the whole statement, and also catches
    # input that parses into something that is not a statement at all --
    # "garbage nonsense" parses cleanly as an Alias, not a ParseError.
    if not isinstance(statement, ALLOWED_ROOTS):
        # Two different failures wear the same shape here, and conflating them
        # produced useless reasons. `DELETE FROM track` is a real statement of a
        # forbidden kind. `42`, `x` and `garbage nonsense` are not statements at
        # all -- they parse to bare expressions (Literal, Column, Alias), and
        # reporting "Statement type LITERAL is not permitted" leaks a sqlglot
        # class name that means nothing to the model and suggests the wrong fix.
        if isinstance(statement, FORBIDDEN_NODES) or type(statement) in _NODE_LABELS:
            return False, (
                f"Statement type {_label(statement)} is not permitted. Only "
                f"SELECT queries may be executed, optionally combined with "
                f"UNION, INTERSECT or EXCEPT."
            )
        return False, (
            "Input is not a SQL statement; it parsed as a bare expression. "
            "Only SELECT queries may be executed, optionally combined with "
            "UNION, INTERSECT or EXCEPT."
        )

    # --- AC10, AC11: tree deny-list ---------------------------------------
    # The root being a SELECT proves nothing on its own. All of these have a
    # SELECT root and all of them write:
    #
    #     WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x
    #     WITH x AS (UPDATE t SET c = 1 RETURNING *) SELECT * FROM x
    #     WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x
    #     SELECT * INTO evil FROM t
    #
    # Only walking the whole tree finds them.
    for node in statement.walk():
        if isinstance(node, FORBIDDEN_NODES):
            return False, (
                f"Found {_label(node)} inside the query. Data-modifying and "
                f"non-query statements are not permitted anywhere, including "
                f"inside CTEs and subqueries."
            )

    # --- AC25: every branch of a set operation, recursively ----------------
    # Runs after the walk so that a smuggled write is reported with the
    # specific reason above rather than as a generic branch failure.
    disallowed = _first_disallowed_branch(statement)
    if disallowed is not None:
        return False, (
            f"A branch of this UNION / INTERSECT / EXCEPT is "
            f"{_label(disallowed)}, which is not a query. Every branch must "
            f"itself be a SELECT."
        )

    return True, ""
