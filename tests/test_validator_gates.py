"""Gate independence — do Gates 1 and 2 hold each other up, or is one carrying?

Task T9 of `specs/002-sql-validation-plan.md`. This is the only test module that
sends *forbidden* SQL to a real database, and it needs a running Postgres.

---

**Deliberate, narrow exemption from a standing rule.**
`specs/000-project.md` §4 says no code path may execute SQL without passing
through the validator, tests included. This module breaks that on purpose, and
it is the one place where doing so is the point: proving Gate 1 refuses these
payloads *independently* requires actually handing them to Postgres. Verifying
through the validator would only prove the validator agrees with itself.

Three things keep it safe:

1. The connection is `querypilot_ro`, which holds `SELECT` and nothing else.
2. Every statement runs inside a transaction that is **always rolled back**,
   including on success. PostgreSQL makes DDL transactional, so even a
   hypothetical Gate 1 failure would leave nothing behind.
3. A row count is asserted unchanged around the whole module.

The payloads are a hardcoded corpus, never model output. If this module ever
needs to execute *generated* SQL, that is a different decision requiring its own
spec entry.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

#: Valid PostgreSQL that Gate 2 rejects. Only statements Postgres will actually
#: parse belong here -- the point is what its *privilege* system does with them.
GATE_TWO_REJECTS = [
    pytest.param("DELETE FROM track", id="delete"),
    pytest.param("UPDATE track SET name = 'pwned'", id="update"),
    pytest.param("INSERT INTO track (track_id, name, media_type_id, milliseconds, unit_price) "
                 "VALUES (999999, 'x', 1, 1, 1)", id="insert"),
    pytest.param("DROP TABLE track", id="drop"),
    pytest.param("TRUNCATE TABLE track", id="truncate"),
    pytest.param("CREATE TABLE evil (id int)", id="create"),
    pytest.param("SELECT * INTO evil FROM track", id="select-into"),
    pytest.param("WITH gone AS (DELETE FROM track RETURNING *) SELECT * FROM gone",
                 id="delete-cte"),
]

#: PostgreSQL refuses these two ways, and the distinction is not cosmetic:
#: a write refusal is "permission denied", while DDL on someone else's object
#: is "must be owner". A test asserting only the first silently stops covering
#: DROP.
REFUSAL_PHRASES = ("permission denied", "must be owner")


def _attempt(engine, sql: str) -> str | None:
    """Run `sql` in an always-rolled-back transaction.

    Returns the database's error message, or None if it was allowed to run.
    """
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(sql))
            return None
        except SQLAlchemyError as exc:
            return str(exc)
        finally:
            # Unconditional. On the success path this is what makes the test
            # safe rather than merely lucky.
            transaction.rollback()


@pytest.fixture
def engine(configured_database):
    from api.db.engine import get_engine

    return get_engine()


@pytest.mark.parametrize("sql", GATE_TWO_REJECTS)
def test_gate_one_independently_refuses_what_gate_two_rejects(engine, sql):
    """Defence in depth is a claim about *independence*. If Gate 2 were removed
    tomorrow, these must still fail — that is the difference between two gates
    and one gate with a spare."""
    from api.safety.validator import validate_sql

    ok, _ = validate_sql(sql)
    assert ok is False, "corpus drifted: Gate 2 no longer rejects this"

    error = _attempt(engine, sql)
    assert error is not None, f"GATE 1 ALLOWED THIS: {sql}"
    assert any(phrase in error.lower() for phrase in REFUSAL_PHRASES), (
        f"Gate 1 refused, but not via a privilege or ownership check, so this "
        f"may be incidental rather than a guarantee: {error[:200]}"
    )


def test_gate_one_neutralises_grant_without_raising(engine):
    """`GRANT ALL ON track TO PUBLIC` **does not error** as `querypilot_ro`.

    PostgreSQL emits a warning -- "no privileges were granted" -- and grants
    nothing. An earlier version of this module had GRANT in the corpus above and
    failed, which was the test asserting the wrong property.

    The lesson is worth keeping: **Gate 1's guarantee is about effect, not about
    raising.** A test that only checks "did it throw" would report a false pass
    the day some other statement becomes a silent no-op instead of an error, so
    this one checks the privilege afterwards.
    """
    error = _attempt(engine, "GRANT ALL ON track TO PUBLIC")
    assert error is None, (
        "GRANT now errors for this role. Stronger than documented -- move it "
        "back into GATE_TWO_REJECTS and delete this test."
    )

    with engine.connect() as conn:
        granted = conn.execute(
            text("SELECT has_table_privilege('public', 'track', 'INSERT')")
        ).scalar_one()
    assert granted is False, "GRANT actually granted something to PUBLIC"

    from api.safety.validator import validate_sql

    ok, reason = validate_sql("GRANT ALL ON track TO PUBLIC")
    assert ok is False and "GRANT" in reason


def test_the_data_is_untouched_afterwards(engine):
    """Belt and braces over the rollbacks above."""
    with engine.connect() as conn:
        tracks = conn.execute(text("SELECT count(*) FROM track")).scalar_one()
    assert tracks == 3503


def test_gate_one_does_not_cover_everything_gate_two_does(engine):
    """The documented seam between the gates, pinned as a test.

    `SET statement_timeout = 0` is refused by Gate 2 (AC14) but **allowed by
    Postgres for any role** — a session may raise its own timeout. So Gate 1
    does *not* back up Gate 2 here.

    This is why `specs/000-project.md` §4 records that Gate 3's timeout is a
    session default rather than a ceiling, and that it holds only because Gate 2
    leaves no route to issue a SET. If AC14 were ever relaxed, nothing else
    would catch it — and this test is what says so out loud.
    """
    error = _attempt(engine, "SET statement_timeout = 0")
    assert error is None, (
        "Postgres now refuses SET for this role. That is stronger than assumed "
        "-- update 000-project.md section 4, which currently states Gate 3 "
        "depends on Gate 2 for this."
    )

    from api.safety.validator import validate_sql

    ok, reason = validate_sql("SET statement_timeout = 0")
    assert ok is False and "SET" in reason, (
        "Gate 2 is the only thing standing between the agent and disabling "
        "Gate 3. It just stopped doing that."
    )


# --- parser agreement ------------------------------------------------------

#: Characters that plausibly terminate a `--` comment. If sqlglot and Postgres
#: ever disagree about one of these, an attacker can hide a stacked statement
#: from Gate 2 that Postgres will happily run.
COMMENT_TERMINATOR_CANDIDATES = {
    "LF": "\n",
    "CR": "\r",
    "VT": "\x0b",
    "FF": "\x0c",
    "FS": "\x1c",
    "GS": "\x1d",
    "RS": "\x1e",
    "NEL": "\u0085",
    "LINE SEPARATOR": "\u2028",
    "PARAGRAPH SEPARATOR": "\u2029",
}


@pytest.mark.parametrize("name,char", sorted(COMMENT_TERMINATOR_CANDIDATES.items()))
def test_sqlglot_and_postgres_agree_on_comment_termination(engine, name, char):
    """A parser-differential regression test, and the sharpest edge in Gate 2.

    Gate 2 decides "one statement or two" using sqlglot's idea of where a `--`
    comment ends. Postgres then applies *its* idea. Any character where sqlglot
    says "still a comment" and Postgres says "code resumes here" is a bypass:
    `SELECT 1 --<char>; DROP TABLE track` passes validation as one harmless
    statement and arrives at a server that sees two.

    Measured at sqlglot 27.29.0 / PostgreSQL 16 they agree on all ten: only LF
    and CR terminate, for both. This test exists so a version bump on either
    side cannot change that quietly.
    """
    import sqlglot

    # Postgres: `SELECT 1 --<char>, 2` yields two columns if the comment ended.
    with engine.connect() as conn:
        result = conn.execute(text(f"SELECT 1 --{char}, 2"))
        postgres_ends_comment = len(result.keys()) == 2

    # sqlglot: two statements means it also saw the comment end.
    parsed = sqlglot.parse(f"SELECT 1 --{char}; SELECT 2", dialect="postgres")
    sqlglot_ends_comment = len([p for p in parsed if p is not None]) > 1

    assert postgres_ends_comment == sqlglot_ends_comment, (
        f"PARSER DIFFERENTIAL on {name}: postgres_ends_comment="
        f"{postgres_ends_comment}, sqlglot_ends_comment={sqlglot_ends_comment}. "
        f"If Postgres ends the comment and sqlglot does not, a stacked "
        f"statement can be hidden from Gate 2 and executed anyway."
    )


# --- T9: gate independence through execute_sql -----------------------------


@pytest.mark.parametrize("sql", GATE_TWO_REJECTS)
def test_execute_sql_stops_writes_at_gate_two(configured_database, sql):
    """The full pipeline: nothing on this corpus reaches the database at all.

    `test_gate_one_independently_refuses_what_gate_two_rejects` above proves
    Gate 1 would also refuse them. This proves they never get that far — which
    is the ordering the whole design depends on.
    """
    from api.db.execution import CATEGORY_GATE_VIOLATION, CATEGORY_REJECTED, execute_sql

    result = execute_sql(sql)
    assert result.ok is False
    assert result.category == CATEGORY_REJECTED, (
        f"{sql!r} was not stopped by Gate 2; it reached the database and came "
        f"back as {result.category!r}"
    )
    assert result.category != CATEGORY_GATE_VIOLATION


def test_the_three_gates_refuse_in_different_languages(configured_database, engine):
    """Defence in depth is a claim about independence, and the clearest evidence
    is that each gate refuses in its own vocabulary:

      Gate 2  -> "Statement type DELETE is not permitted"   (before execution)
      Gate 1b -> SQLSTATE 25006, read-only transaction      (independent of role)
      Gate 1  -> SQLSTATE 42501, permission denied          (independent of both)

    If any two of these ever produced the same signal, one of them would have
    stopped being a separate layer.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    from api.db.execution import execute_sql
    from api.safety.validator import validate_sql

    sql = "DELETE FROM track"

    ok, reason = validate_sql(sql)
    assert ok is False and "DELETE" in reason

    assert execute_sql(sql).category == "rejected"

    # Gate 1b, reached only by bypassing Gate 2 deliberately.
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            with pytest.raises(SQLAlchemyError) as caught:
                conn.execute(text(sql))
            assert getattr(caught.value.orig, "sqlstate", None) == "25006"
        finally:
            trans.rollback()

    # Gate 1, reached by bypassing both.
    error = _attempt(engine, sql)
    assert error is not None and "permission denied" in error.lower()


def test_data_is_still_untouched_after_the_execute_sql_tests(engine):
    from sqlalchemy import text

    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM track")).scalar_one() == 3503
