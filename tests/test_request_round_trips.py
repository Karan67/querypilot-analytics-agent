"""Iteration 9 T2 — what one request actually costs the database, end to end.

**AC3, and it exists before the changes it measures.** `012-board-plan.md` §1
puts this task first for one reason: a round-trip assertion written *after* the
schema cache is removed pins whatever the code ended up doing. `HANDOFF.md` §6
records the version of that mistake which cost real time — nineteen green tests
of `cached_schema()` did not notice the request path had stopped calling it,
because every one of them called it directly. Counting inside a module is what
already existed and is exactly what missed it.

So this counts statements the database was asked to run **through the endpoint**,
with only the provider faked, and it asserts the **composition** rather than a
total. A total alone stays green when one read is removed and another is added,
which is precisely the change T3 and T4 made.

---

## What T2 found: the plan predicted 9 and the answer was 18

**Kept as the record of why this task came first.** `012-board-plan.md` §1
tabulated a miss at 9 round trips. That was the *warm* miss: the first request
after a boot found the schema cache empty, so `_deployed_fingerprints()` paid
the probe *and* a full introspection before `answer()`'s own read found the
cache warm. The distinction did not exist on paper and did on the wire:

| | probe | introspection | generated `SELECT` | total |
|---|---|---|---|---|
| **cold miss** — cache empty | 2 | 1 | 1 | **18** |
| **warm miss** — new question, cache warm | 2 | 0 | 1 | **9** |
| **answer-cache hit** | 1 | 0 | 0 | **3** |

Each unit above is one `execute_sql()`, which is three statements: the read-only
transaction, the statement timeout, and the query itself. That the preamble is
two thirds of the cost of a catalog read is itself worth seeing, and is invisible
from inside any module.

## What each task moves, and what it is now

**These tests are *supposed* to go red at T3 and T4.** Being updated with a
stated new expectation is the instrument working; quietly widening them to pass
both ways is the failure mode. Every row below was measured, not predicted —
the "before" row is what T2 found and the T3 row is what T3 produced.

| | cold miss | warm miss | hit |
|---|---|---|---|
| T2, as found | 18 | 9 | 3 |
| T3 — one schema read | 15 | 6 | 3 |
| **T4 — no cache (current)** | **12** | **12** | **9** |

**T3 removed a read; T4 changed what a read costs.** A hit was untouched by T3
because it only ever made one read. After T4 the cold and warm columns converge,
because without a cache there is no such thing as a warm one: every request
introspects (3 queries, 9 statements) and a miss adds the generated `SELECT`.

Every figure above was measured, including T4's, which matched the prediction
`012-board-plan.md` §1 made before either change. The hit is where the cost
landed: `012-board.md` §2.1 measured it at 6ms, essentially all of it the probe
T4 deleted, and 9 round trips of introspection now replace 3 of probe.

**A warm miss went 9 → 6 → 12.** Both movements are deliberate and neither is a
regression to fix by restoring the cache: T3 removed a duplicate read, and T4
traded 13ms for 718 lines of machinery on a request whose measured range is
1,431–5,901ms.
"""

from __future__ import annotations

import contextlib

import pytest
import sqlalchemy
from fastapi.testclient import TestClient

from api.db.engine import get_engine
from api.main import app

#: One `execute_sql()` is three statements: `SET TRANSACTION READ ONLY`,
#: `SET LOCAL statement_timeout`, and the query. Named rather than spelled `3`
#: at each site, so a change to the preamble reads as one edit.
STATEMENTS_PER_EXECUTE_SQL = 3

#: Substrings that identify each query this path can issue. Matched against the
#: statement text the driver was handed, which is a compile-time constant in
#: every case here -- no user input reaches any of them.
_PROBE = "c.relkind::text || '|' || a.attname"
_RELATIONS = "SELECT c.relname, c.relkind::text FROM pg_class"
_COLUMNS = "a.attnum, format_type"
_FOREIGN_KEYS = "FROM pg_constraint con"
_PREAMBLE = ("SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout")


class _RoundTrips:
    """Statements the database was actually asked to run, in order."""

    def __init__(self):
        self.statements: list[str] = []

    @property
    def n(self) -> int:
        return len(self.statements)

    def count(self, needle: str) -> int:
        return sum(1 for s in self.statements if needle in s)

    @property
    def composition(self) -> dict[str, int]:
        """What the request was made of, by query kind.

        The residual `other` is what makes this an assertion rather than a
        checklist: a statement nobody anticipated lands there and fails the
        comparison, instead of passing unnoticed because no key counted it.
        """
        known = {
            "probe": self.count(_PROBE),
            "introspect_relations": self.count(_RELATIONS),
            "introspect_columns": self.count(_COLUMNS),
            "introspect_foreign_keys": self.count(_FOREIGN_KEYS),
            "generated_select": self.count("FROM track"),
        }
        preamble = sum(self.count(p) for p in _PREAMBLE)
        known["other"] = self.n - sum(known.values()) - preamble
        return known

    def __repr__(self) -> str:  # shown when an assertion fails
        return f"<{self.n} round trips: {[s[:60] for s in self.statements]}>"


@contextlib.contextmanager
def counting_round_trips():
    """Count statements issued on the shared engine.

    Moved here from `tests/test_schema_cache.py` before T4 deleted that file.
    The harness was the one thing in it worth keeping: it is the only way this
    project counts what the database was really asked to do.
    """
    counter = _RoundTrips()
    engine = get_engine()

    def _record(conn, cursor, statement, parameters, context, executemany):
        counter.statements.append(" ".join(str(statement).split()))

    sqlalchemy.event.listen(engine, "before_cursor_execute", _record)
    try:
        yield counter
    finally:
        sqlalchemy.event.remove(engine, "before_cursor_execute", _record)


class _StubProvider:
    """One fixed reply, so the loop runs for real and nothing is billed.

    The reply is a valid `ACTION:` line, so Gate 2, the read-only transaction
    and the row cap all run exactly as they do in production. A double that
    short-circuited the agent would measure the endpoint and not the path.
    """

    def __init__(self):
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return "ACTION: execute_sql\nSELECT count(*) AS n FROM track"


@pytest.fixture
def client(monkeypatch):
    # Patched where `api.main` looked it up, for the reason `_answer` in
    # `tests/test_ask_endpoint.py` gives: patching the factory's home would
    # leave the already-imported name pointing at the real one.
    monkeypatch.setattr("api.main.get_provider", lambda: _StubProvider())
    return TestClient(app)


def _ask(client, question: str):
    response = client.post("/ask", json={"question": question})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"], f"the stub's answer did not succeed: {body.get('error')}"
    return body


# --- the three shapes --------------------------------------------------------


def test_a_miss_introspects_once_and_runs_the_query(client, configured_database):
    """Any miss, cold or warm — there is no longer a difference (T4).

    **One schema read, 12 round trips.** `_deployed_fingerprints()` introspects
    to build the answer-cache key and hands the schema on; `answer()` does not
    read again (T3), and no probe runs because there is no cache left to
    validate (T4).

    Nine of the twelve are the schema: three catalog queries, each paying the
    read-only transaction and the statement timeout that every `execute_sql()`
    pays. Six of those nine statements are preamble.
    """
    with counting_round_trips() as trips:
        body = _ask(client, "How many tracks?")

    assert body["cache_hit"] is False
    assert trips.composition == {
        "probe": 0,
        "introspect_relations": 1,
        "introspect_columns": 1,
        "introspect_foreign_keys": 1,
        "generated_select": 1,
        "other": 0,
    }, trips
    assert trips.n == 4 * STATEMENTS_PER_EXECUTE_SQL


def test_a_second_question_costs_the_same_as_the_first(client, configured_database):
    """**The convergence T4 produced, asserted rather than assumed.**

    Before T4 this was the cheap case: the schema cache was warm, so a second
    question cost 6 round trips against a first question's 15. Now both cost 12,
    because every request introspects.

    Asserted as its own test because the *disappearance* of a cheap path is the
    kind of change a total over one request cannot show. If a memo of the schema
    is ever added back, this is the test that will go green in a way somebody
    has to explain.
    """
    _ask(client, "How many tracks?")  # warms the schema cache

    with counting_round_trips() as trips:
        body = _ask(client, "How many tracks are there in total?")

    assert body["cache_hit"] is False
    assert trips.composition == {
        "probe": 0,
        "introspect_relations": 1,
        "introspect_columns": 1,
        "introspect_foreign_keys": 1,
        "generated_select": 1,
        "other": 0,
    }, trips
    assert trips.n == 4 * STATEMENTS_PER_EXECUTE_SQL


def test_an_answer_cache_hit_still_introspects_to_build_its_key(
    client, configured_database
):
    """**The path B-14 cost the most, and it is worth seeing plainly.**

    A cached answer still reads the whole catalog, because the cache key is
    built from the schema fingerprint and the key has to exist before the cache
    can be consulted. `012-board.md` §2.1 measured this request at 6ms when a
    5.32ms probe answered that question; it is now a 20ms introspection.

    What still does not happen is the generated query: the rows come from the
    answer cache, which is the saving that cache exists for.
    """
    _ask(client, "How many tracks?")

    with counting_round_trips() as trips:
        body = _ask(client, "How many tracks?")

    assert body["cache_hit"] is True
    assert trips.composition == {
        "probe": 0,
        "introspect_relations": 1,
        "introspect_columns": 1,
        "introspect_foreign_keys": 1,
        "generated_select": 0,
        "other": 0,
    }, trips
    assert trips.n == 3 * STATEMENTS_PER_EXECUTE_SQL


# --- the property the two path tasks move ------------------------------------


def test_the_request_path_reads_the_schema_once_on_a_miss(
    client, configured_database
):
    """**The property T3 delivered, stated on its own.**

    Expressed as *reads*, not as a total, so it keeps meaning the same thing
    after T4 replaces each read with a different set of statements: a read is a
    probe today and will be an introspection afterwards. **This assertion is
    therefore expected to survive T4 unchanged** -- if it fails there, T4 has
    added a read back rather than merely changing what one costs, which is a
    different and worse thing than the round-trip totals going up.

    It is deliberately insensitive to *how* the schema is read. Proven so: at T2
    a mutation swapping the cache for a direct introspection left this green and
    turned all three composition tests red. The pair covers both questions.
    """
    _ask(client, "How many tracks?")  # warm, so a read is exactly one probe

    with counting_round_trips() as trips:
        _ask(client, "Something else entirely?")

    reads = trips.count(_PROBE) + trips.count(_RELATIONS)
    assert reads == 1, (
        f"a miss makes {reads} schema reads and should make exactly one; "
        f"before T3 it made two: {trips}"
    )


def test_a_hit_never_touches_the_answered_table(client, configured_database):
    """Gate 3's row cap and the generated query are skipped entirely on a hit.

    A separate assertion from the composition above because it is the property a
    reader cares about -- a cached answer costs no query -- rather than an
    arithmetic fact about a dict.
    """
    _ask(client, "How many tracks?")

    with counting_round_trips() as trips:
        _ask(client, "How many tracks?")

    assert trips.count("FROM track") == 0, trips
