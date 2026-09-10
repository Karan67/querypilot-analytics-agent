"""Iteration 7 T2 — the operational history store.

Two tests here carry the weight. `test_a_write_failure_never_raises` is
resolved D-2 — *an observability failure must never cascade into user-facing
downtime* — and `test_concurrent_writers_do_not_lose_rows` is the concurrency
guarantee the plan made mandatory rather than advisory, because FastAPI serves
on a thread pool and the writes genuinely race.
"""

from __future__ import annotations

import concurrent.futures
import sqlite3

import pytest

from api.store import history
from api.store.history import AskRecord, record_ask, recent, steps_for


def _record(**overrides) -> AskRecord:
    base = dict(
        question="How many tracks are in the library?",
        ok=True,
        total_ms=812,
        provider_ms=653,
        sql="SELECT count(*) FROM track",
        category="",
        shape="scalar",
        row_count=1,
        attempts_used=1,
        total_tokens=1069,
        prompt_tokens=1000,
        usage_measured=True,
        provider_calls=1,
        model="openai/gpt-oss-120b",
    )
    base.update(overrides)
    return AskRecord(**base)


# --- the round trip ---------------------------------------------------------


def test_an_answer_survives_a_round_trip():
    ask_id = record_ask(_record())
    assert ask_id

    rows = recent()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == ask_id
    assert row["question"] == "How many tracks are in the library?"
    assert row["ok"] == 1
    assert row["sql"] == "SELECT count(*) FROM track"
    assert row["shape"] == "scalar"


def test_the_two_durations_are_kept_apart():
    """AC2. 010 §2.2 measured the provider at 94.2% of wall clock, so a single
    duration column would hide the only number that moves and invite work on
    the 6% that cannot matter."""
    record_ask(_record(total_ms=10393, provider_ms=10231))
    row = recent()[0]
    assert row["total_ms"] == 10393
    assert row["provider_ms"] == 10231
    assert row["total_ms"] > row["provider_ms"]


def test_the_record_says_which_instrument_measured_the_cost():
    """AC1, following D-1's standing precedent: a locally counted number and a
    provider-billed number are different quantities, and a record that does not
    say which it holds is not a measurement."""
    record_ask(_record(total_tokens=1069, usage_measured=True))
    assert recent()[0]["usage_measured"] == 1

    record_ask(_record(total_tokens=1100, usage_measured=False))
    assert recent()[0]["usage_measured"] == 0


def test_the_rate_limit_snapshot_explains_a_slow_answer():
    """AC3. Without these columns a latency log cannot tell throttling from a
    hard question -- and 010 §2.3 measured a fourteen-fold climb caused entirely
    by throttling, with the work held constant."""
    record_ask(_record(total_ms=10393, provider_ms=10231, tpm_remaining=112))
    row = recent()[0]
    assert row["tpm_remaining"] == 112


def test_the_trace_is_persisted_per_attempt():
    """Charter §1's claim is *read the error, revise*. A history that records
    only the final answer cannot show that ever happened."""
    steps = (
        {
            "attempt": 1,
            "action": "execute_sql",
            "ok": False,
            "category": "database_error",
            "error": 'column "artist_name" does not exist',
            "sql": "SELECT artist_name FROM track",
        },
        {"attempt": 2, "action": "execute_sql", "ok": True, "sql": "SELECT name FROM artist"},
    )
    ask_id = record_ask(_record(attempts_used=2, steps=steps))

    persisted = steps_for(ask_id)
    assert [s["attempt"] for s in persisted] == [1, 2]
    assert persisted[0]["ok"] == 0
    assert "artist_name" in persisted[0]["error"]
    assert persisted[1]["ok"] == 1


def test_each_answer_gets_its_own_id():
    """The id feedback will attach to in Iteration 8 -- an *answer*, never a
    question string, because the agent may answer the same question differently
    next time."""
    first = record_ask(_record())
    second = record_ask(_record())
    assert first != second
    assert len({row["id"] for row in recent()}) == 2


def test_recent_returns_newest_first():
    for i in range(3):
        record_ask(_record(question=f"question {i}"))
    assert [row["question"] for row in recent()][0] == "question 2"


# --- resolved D-2: recording must never break answering ---------------------


def test_a_write_failure_never_raises(monkeypatch):
    """**Resolved D-2.** The user asked a question and got an answer; losing the
    log entry is our problem, not theirs.

    The mutation this guards against is removing the `except` in `record_ask`,
    which turns a full volume or a locked database into a failed answer.
    """
    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(history, "_connect", explode)
    assert record_ask(_record()) is None


def test_a_write_failure_is_visible_to_health(monkeypatch):
    """The other half of D-2. Swallowing the error is right; swallowing it
    *silently* would leave the system amnesiac while looking healthy, which is
    exactly what a health check exists to prevent."""
    assert history.degraded() == ""

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(history, "_connect", explode)
    record_ask(_record())
    assert "readonly" in history.degraded()


def test_the_degraded_flag_clears_after_a_successful_write(monkeypatch):
    """A store that recovers must stop reporting itself broken, or the health
    check becomes a permanent alarm nobody reads.

    **`monkeypatch.undo()` used to be the second half of this test, and it was
    writing a real database to `C:\\data` on the development machine.** CI found
    it on the second pipeline run this repository ever had, as
    ``PermissionError: [Errno 13] Permission denied: '/data'`` -- because a
    Linux runner cannot create `/data` and Windows happily creates `C:\\data`.

    `monkeypatch` is one function-scoped instance shared with every fixture that
    asked for it, so `undo()` here also reverted `isolated_history_store`'s
    redirection of `DEFAULT_PATH` to `tmp_path`. The store then fell back to its
    real default and the write succeeded against the developer's filesystem. The
    assertion still passed, which is why nothing noticed for an iteration.

    This is the seventh instance of the trap `HANDOFF` section 6 names: *shared
    state a test can reach will eventually be written by one*. The isolation was
    autouse specifically so no test had to remember it -- and this test did not
    forget, it *undid* it. So the fix restores the one attribute it patched
    rather than everything anybody patched.
    """
    real_connect = history._connect

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(history, "_connect", explode)
    record_ask(_record())
    assert history.degraded()

    monkeypatch.setattr(history, "_connect", real_connect)
    record_ask(_record())
    assert history.degraded() == ""


def test_an_unanticipated_exception_is_also_caught(monkeypatch):
    """The `except` is deliberately total. A narrower one is a list of failures
    somebody guessed, and the one that matters is the one missing from it."""
    def explode(*args, **kwargs):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(history, "_connect", explode)
    assert record_ask(_record()) is None
    assert "RuntimeError" in history.degraded()


# --- the concurrency guarantee the plan made mandatory ----------------------


def test_concurrent_writers_do_not_lose_rows():
    """FastAPI serves on a thread pool, so these writes genuinely race.

    **This test found a real defect, and only because it flaked.** At 8 threads
    it passed almost always; under the D-2 mutation it failed intermittently,
    which is what prompted looking. The cause was `_connect` applying the schema
    on every connection -- `executescript()` takes a write lock even for a
    reader -- producing `database is locked` on about 0.4% of writes at 16
    threads. Frequent enough to drop real telemetry, rare enough to pass a suite.

    Two fixes were tried and the first was wrong: `BEGIN IMMEDIATE` made it
    *worse*, because taking the write lock earlier moves contention rather than
    removing it. What works is serialising writes in-process, which is sound
    because there is exactly one API process.

    Sized to actually contend rather than to look thorough: 24 threads is three
    times the default worker count, and the assertion is that **nothing is
    dropped**, not that it usually works.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
        ids = list(pool.map(lambda i: record_ask(_record(question=f"q{i}")), range(150)))

    dropped = [i for i, v in enumerate(ids) if v is None]
    assert not dropped, f"{len(dropped)} concurrent writes dropped: {history.degraded()}"
    assert len(set(ids)) == 150
    assert len(recent(limit=500)) == 150


def test_the_schema_is_applied_once_per_path_not_once_per_connection(monkeypatch):
    """The defect above, asserted by **counting the calls**.

    The first version of this test asserted that `_initialised` stopped growing,
    and a mutation that ran `executescript` on every connection **passed it** --
    because the mutation still populated the set. It was asserting a side effect
    of the fix rather than the fix.

    Counting is the honest form: per-connection `executescript()` is invisible
    in behaviour and shows up only as rare lock contention, so the call count is
    the only thing that discriminates.
    """
    calls = {"n": 0}
    real_sql = history._SCHEMA.read_text(encoding="utf-8")

    class CountingSchema:
        """`sqlite3.Connection` is a C type and cannot be patched, so the count
        is taken one step earlier: `read_text` is called exactly once per
        `executescript`, on the line that performs it."""

        def read_text(self, *args, **kwargs):
            calls["n"] += 1
            return real_sql

    monkeypatch.setattr(history, "_SCHEMA", CountingSchema())

    record_ask(_record())
    assert calls["n"] == 1, "the first write should apply the schema exactly once"

    for _ in range(5):
        record_ask(_record())
    recent()

    assert calls["n"] == 1, (
        f"the schema was re-applied {calls['n']} times; executescript takes a "
        f"write lock on every connection and is what produced `database is "
        f"locked` under contention"
    )


def test_writes_are_serialised_within_the_process():
    """The fix is a lock, and a lock that is not held is not a fix.

    Guards the mutation of removing `_write_lock` from `record_ask`, which
    reintroduces a race that only shows up as rare dropped telemetry.
    """
    import ast
    import pathlib as _pathlib

    tree = ast.parse(_pathlib.Path("api/store/history.py").read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "record_ask"
    )
    names = {
        n.id for n in ast.walk(fn) if isinstance(n, ast.Name)
    }
    assert "_write_lock" in names, "record_ask no longer takes the write lock"


def test_the_busy_timeout_is_short_enough_to_stay_a_telemetry_problem():
    """Contention here must not become the latency this iteration measures.

    A long timeout converts a logging problem into a user-visible delay, which
    is the opposite of D-2's intent.
    """
    assert 0 < history.BUSY_TIMEOUT_SECONDS <= 5


def test_a_reader_is_allowed_to_fail_where_a_writer_is_not(monkeypatch):
    """Asymmetric on purpose. A reader asking for history and getting an error
    is being told the truth; a questioner getting an error because logging
    failed is being punished for our problem."""
    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("no such table")

    monkeypatch.setattr(history, "_connect", explode)
    with pytest.raises(sqlite3.OperationalError):
        recent()


# --- the path trap ----------------------------------------------------------


def test_the_path_resolves_at_call_time(tmp_path, monkeypatch):
    """T5's `EVALS_PATH` and B-5's ledger both wrote real files because a module
    constant was bound once as a default argument, making `monkeypatch`
    useless. This asserts the fix rather than trusting it."""
    elsewhere = tmp_path / "nested" / "other.db"
    monkeypatch.setattr(history, "DEFAULT_PATH", elsewhere)

    record_ask(_record(question="written elsewhere"))

    assert elsewhere.exists()
    assert recent()[0]["question"] == "written elsewhere"


def test_the_store_creates_its_directory():
    """`/data` is a fresh named volume on first boot, and a store that assumes
    its parent exists works on a developer's host and fails in the container."""
    from api.store.history import DEFAULT_PATH

    record_ask(_record())
    assert DEFAULT_PATH.parent.is_dir()


# --- the boundary -----------------------------------------------------------


def test_the_store_does_not_import_the_agent():
    """`AskRecord` is plain fields on purpose. A storage layer that knows the
    agent's types has to change when the agent does -- the same reasoning that
    makes `api/http/shapes.py` take columns and rows rather than an
    `ExecutionResult`."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("api/store/history.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not any(name.startswith("api.agent") for name in imported)
    assert not any(name.startswith("api.db") for name in imported), (
        "the history store must not reach the analytics database"
    )
