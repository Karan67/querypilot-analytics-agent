"""Writing and reading the operational history.

Three rules shape this module, and all three came out of the plan rather than
out of implementation convenience:

**Recording must never break answering (resolved D-2).** The user asked a
question and got an answer; losing the log entry is our problem, not theirs. So
`record_ask()` catches everything, records the failure in a module-level flag
that `/health` reports, and returns rather than raising. *An observability
failure must never cascade into user-facing downtime.*

**One connection per write, with a short busy timeout.** FastAPI serves requests
on a thread pool, so writes genuinely race; a long timeout would convert
contention into the latency this iteration exists to measure. A lock still
contended after `BUSY_TIMEOUT_SECONDS` drops the row, per the rule above.

**The path resolves at call time.** `DEFAULT_PATH` is read inside each function,
never bound as a default argument. That trap cost T5 a fabricated entry in the
real `EVALS.md`, and cost B-5 6,800 tokens of fake spend in the real ledger,
because a module constant bound once at import makes `monkeypatch` useless.
"""

from __future__ import annotations

import contextlib
import logging
import os
import pathlib
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("querypilot")

#: Where the database lives. `/data` is the named volume in compose; the host
#: default keeps a developer's own runs out of the repository working tree.
#:
#: Read through a function, never captured at import, so tests can redirect it.
DEFAULT_PATH = pathlib.Path(
    os.environ.get("QUERYPILOT_HISTORY_PATH") or "/data/querypilot.db"
)

#: Deliberately short. Contention here is a telemetry problem, and waiting on it
#: would turn one into a latency problem -- in the iteration whose whole subject
#: is latency. Two seconds is long enough to absorb a concurrent writer and
#: short enough that a genuinely stuck lock drops the row instead of holding a
#: user's answer hostage.
BUSY_TIMEOUT_SECONDS = 2.0

#: Set when a write fails, cleared when one succeeds. `/health` reports it, per
#: D-2: a store that is unreachable for every request leaves the system silently
#: amnesiac while looking perfectly healthy.
_degraded: str = ""

_SCHEMA = pathlib.Path(__file__).resolve().parent / "schema.sql"

#: Paths whose schema has been applied, and the lock guarding that set.
#:
#: **Applying the schema on every connection was a measured defect, not a
#: theoretical one.** `executescript()` issues an implicit COMMIT and takes a
#: write lock even when the caller only intends to read, so every connection
#: contended with every other. At 16 threads and 960 writes it produced
#: `OperationalError: database is locked` four times -- about 0.4%, rare enough
#: to pass a test suite and frequent enough to drop real telemetry.
#:
#: Found by the D-2 mutation: with the guard removed the concurrency test failed
#: intermittently, which is the only reason anyone looked.
_initialised: set[pathlib.Path] = set()
_init_lock = threading.Lock()

#: Serialises writes **inside this process**, which is where the contention
#: actually is: one API container, one thread pool, many request threads.
#:
#: Measured rather than assumed, and the first two attempts were wrong. Applying
#: the schema per connection produced `database is locked` on ~0.4% of writes at
#: 16 threads; taking the lock earlier with `BEGIN IMMEDIATE` made it *worse*,
#: because it moves the same contention forward rather than removing it.
#:
#: A write here is sub-millisecond, so serialising costs nothing measurable and
#: removes the whole class of error. `BUSY_TIMEOUT_SECONDS` stays as the defence
#: for a second process -- a `sqlite3` CLI session, or a future sidecar -- which
#: this lock cannot see.
_write_lock = threading.Lock()


@dataclass(frozen=True)
class AskRecord:
    """One answered question, in plain fields.

    Deliberately **not** an `AgentResult`. The store does not import the agent,
    for the reason `api/http/shapes.py` takes `columns` and `rows` rather than an
    `ExecutionResult`: a storage layer that knows the agent's types is one that
    has to change when the agent does.
    """

    question: str
    ok: bool
    total_ms: int
    provider_ms: int
    sql: str = ""
    category: str = ""
    shape: str = ""
    row_count: int | None = None
    attempts_used: int = 0
    total_tokens: int | None = None
    prompt_tokens: int | None = None
    usage_measured: bool = False
    provider_calls: int = 0
    cache_hit: bool = False
    tpm_remaining: int | None = None
    rpd_remaining: int | None = None
    model: str = ""
    schema_fp: str = ""
    prompt_fp: str = ""
    steps: tuple[dict, ...] = field(default_factory=tuple)


def degraded() -> str:
    """Why the last write failed, or `""` if the store is healthy."""
    return _degraded


@contextlib.contextmanager
def _connect(path: pathlib.Path | None = None) -> Iterator[sqlite3.Connection]:
    """One connection, schema applied, closed on the way out.

    A connection per operation rather than a shared one: `sqlite3` objects are
    not safe to share across threads by default, and FastAPI's thread pool is
    exactly the situation that finds out.
    """
    path = DEFAULT_PATH if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)
    try:
        conn.row_factory = sqlite3.Row
        # WAL lets a reader run while a writer holds the lock, which is the
        # whole contention story here: the history view (T7) must not block an
        # answer being recorded.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _ensure_schema(conn, path)
        yield conn
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection, path: pathlib.Path) -> None:
    """Apply the schema once per path, not once per connection.

    Double-checked under a lock: the fast path is a set membership test, and
    only the first connection to a given database pays for `executescript`.
    """
    if path in _initialised:
        return
    with _init_lock:
        if path in _initialised:
            return
        conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
        conn.commit()
        _initialised.add(path)


def record_ask(record: AskRecord, path: pathlib.Path | None = None) -> str | None:
    """Persist one answered question. **Never raises** (resolved D-2).

    Returns the new row's id, or `None` if the write failed — the caller may use
    the id in its response but must not depend on getting one.
    """
    global _degraded
    ask_id = str(uuid.uuid4())
    try:
        with _write_lock, _connect(path) as conn:
            conn.execute(
                """
                INSERT INTO ask (
                    id, asked_at, question, ok, sql, category, shape, row_count,
                    attempts_used, total_ms, provider_ms, total_tokens,
                    prompt_tokens, usage_measured, provider_calls, cache_hit,
                    tpm_remaining, rpd_remaining, model, schema_fp, prompt_fp
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ask_id,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    record.question,
                    int(record.ok),
                    record.sql,
                    record.category,
                    record.shape,
                    record.row_count,
                    record.attempts_used,
                    record.total_ms,
                    record.provider_ms,
                    record.total_tokens,
                    record.prompt_tokens,
                    int(record.usage_measured),
                    record.provider_calls,
                    int(record.cache_hit),
                    record.tpm_remaining,
                    record.rpd_remaining,
                    record.model,
                    record.schema_fp,
                    record.prompt_fp,
                ),
            )
            conn.executemany(
                """
                INSERT INTO step (ask_id, attempt, action, ok, category, error, sql)
                VALUES (?,?,?,?,?,?,?)
                """,
                [
                    (
                        ask_id,
                        step.get("attempt", 0),
                        step.get("action", ""),
                        int(bool(step.get("ok"))),
                        step.get("category", ""),
                        step.get("error", ""),
                        step.get("sql", ""),
                    )
                    for step in record.steps
                ],
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - deliberately total; see D-2
        # Everything, including a full volume, a locked database, a permission
        # error on /data, and anything sqlite3 raises that nobody anticipated.
        # A narrower except here would be a list of failures somebody guessed,
        # and the one that mattered would be the one missing from it.
        _degraded = f"{type(exc).__name__}: {exc}"
        logger.warning("history write failed, answer unaffected: %s", _degraded)
        return None

    _degraded = ""
    return ask_id


def recent(limit: int = 50, path: pathlib.Path | None = None) -> list[sqlite3.Row]:
    """The most recent questions, newest first. Raises if the store is broken.

    Unlike `record_ask`, this one is allowed to fail: a reader asking for
    history and getting an error is being told the truth, whereas a *questioner*
    getting an error because logging failed is being punished for our problem.
    """
    with _connect(path) as conn:
        return list(
            conn.execute(
                "SELECT * FROM ask ORDER BY asked_at DESC, rowid DESC LIMIT ?",
                (limit,),
            )
        )


def steps_for(ask_id: str, path: pathlib.Path | None = None) -> list[sqlite3.Row]:
    with _connect(path) as conn:
        return list(
            conn.execute(
                "SELECT * FROM step WHERE ask_id = ? ORDER BY attempt", (ask_id,)
            )
        )


def steps_for_ids(
    ask_ids: Sequence[str], path: pathlib.Path | None = None
) -> dict[str, list[sqlite3.Row]]:
    """Traces for many answers at once, grouped by `ask_id`.

    **One query, not one per row.** The T7 reader shows fifty answers with their
    traces, and calling `steps_for` in a loop would be fifty-one queries to
    render one page -- each opening its own connection, since `_connect` is
    per-operation by design. That is the classic N+1, and it is worth avoiding
    here specifically because the reader competes for the same write lock the
    answers are being recorded through.

    The `IN` list is built from placeholders rather than interpolated. These ids
    are uuids this process generated, so nothing hostile can reach them, but a
    query assembled by string formatting is a habit that outlives the context
    that made it safe.
    """
    if not ask_ids:
        return {}

    placeholders = ",".join("?" for _ in ask_ids)
    with _connect(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM step WHERE ask_id IN ({placeholders}) ORDER BY ask_id, attempt",
            tuple(ask_ids),
        )
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(row["ask_id"], []).append(row)
        return grouped
