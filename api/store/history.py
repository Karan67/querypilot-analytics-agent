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


class UnknownAsk(LookupError):
    """No answer with that id, so there is nothing to attach a mark to.

    Its own exception rather than a `None` return, because the caller must
    distinguish it from a store failure: one is a `404` and the other a `503`,
    and collapsing them would tell a user their mark was rejected when the
    database was simply unreachable.
    """


def record_feedback(
    ask_id: str,
    rating: int,
    note: str = "",
    path: pathlib.Path | None = None,
) -> str:
    """Attach one mark to one answer, and **raise** if it cannot (AC13).

    Returns the new row's id.

    **The opposite policy to `record_ask`, deliberately.** That one swallows
    every failure, because a person who asked a question and got an answer must
    not be punished for our logging breaking -- the write is incidental to what
    they wanted. Here the write *is* what they wanted. Reporting success on a
    mark that was never stored would be the same lie as returning an empty list
    from a broken `/history/data`, which T7 refused for the same reason.

    Raises:
        UnknownAsk: no answer has this id.
        Exception: whatever the store raised. Not caught here: the endpoint
            turns it into a 503, and a mark that failed to store must say so.
    """
    with _write_lock, _connect(path) as conn:
        # Checked rather than left to the foreign key, because SQLite does not
        # enforce one without `PRAGMA foreign_keys=ON` and because this is the
        # only way to tell "unknown id" from "write failed" -- a 404 from a 503.
        exists = conn.execute("SELECT 1 FROM ask WHERE id = ?", (ask_id,)).fetchone()
        if exists is None:
            raise UnknownAsk(ask_id)

        feedback_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO feedback (id, ask_id, created_at, rating, note)
            VALUES (?,?,?,?,?)
            """,
            (
                feedback_id,
                ask_id,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                int(rating),
                note,
            ),
        )
        conn.commit()
        return feedback_id


def feedback_for_ids(
    ask_ids: Sequence[str], path: pathlib.Path | None = None
) -> dict[str, list[sqlite3.Row]]:
    """Marks for many answers at once, grouped by `ask_id`.

    One query, not one per row, for the reason `steps_for_ids` gives: the reader
    shows fifty answers and would otherwise open fifty-one connections to render
    one page, competing for the write lock that answers are recorded through.

    Returns every mark, oldest first. **Not a count, not a score, not a
    latest-wins single value** (AC14): the caller is handed the marks
    themselves, so there is no reduced number for a template to mistake for an
    accuracy figure.
    """
    if not ask_ids:
        return {}

    placeholders = ",".join("?" for _ in ask_ids)
    with _connect(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM feedback WHERE ask_id IN ({placeholders}) "
            f"ORDER BY ask_id, created_at, rowid",
            tuple(ask_ids),
        )
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(row["ask_id"], []).append(row)
        return grouped


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


# --- Iteration 12 T9: the daily spend ceiling --------------------------------
#
# Authentication (Iteration 10) narrows who can spend the project's quota; it
# does not bound how much one identity spends, or what a browser tab left
# reloading costs before anyone notices. This is the part that does.

#: Env-configurable, read at call time like everything else in this module --
#: `os.environ.get` bound once as a default would make `monkeypatch` useless,
#: which is the exact trap this file's own docstring names.
IDENTITY_LIMIT_ENV = "QUERYPILOT_DAILY_IDENTITY_LIMIT"
GLOBAL_LIMIT_ENV = "QUERYPILOT_DAILY_GLOBAL_LIMIT"

#: 50 and 150 (spec section 7 Q-G), against a key measured at roughly 180
#: questions/day. Three identities at 50 each reach the global ceiling before
#: any one of them reaches its own, which `SpendDecision.category` exists to
#: make legible rather than let read as a bug.
DEFAULT_IDENTITY_LIMIT = 50
DEFAULT_GLOBAL_LIMIT = 150

#: The question was refused because *this identity* reached its own ceiling.
CATEGORY_IDENTITY_DAILY_LIMIT = "identity_daily_limit"
#: The question was refused because the *deployment* reached its ceiling,
#: while this identity had room left in its own. Named separately from the one
#: above because the fix is different -- the caller cannot do anything about
#: someone else's spend -- and because a message that did not distinguish them
#: would read as a bug the first time it fired for the "wrong" reason.
CATEGORY_GLOBAL_DAILY_LIMIT = "global_daily_limit"
#: The ledger itself could not be read or written. Not the caller's fault and
#: not a caller-throttling condition -- `api/http/errors.py` maps it to `503`
#: on the same reasoning D-2 already applies to an unreachable database:
#: silently allowing the question through would be a claim, made with no
#: evidence, that the ceiling was not exceeded.
CATEGORY_LEDGER_UNAVAILABLE = "ledger_unavailable"


@dataclass(frozen=True)
class SpendDecision:
    """Whether one question may proceed, and why not if it may not."""

    allowed: bool
    identity_count: int
    global_count: int
    identity_limit: int
    global_limit: int
    #: `""` when allowed, else one of the three categories above.
    category: str = ""


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using the default %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%d is not positive; using the default %d", name, value, default)
        return default
    return value


def reserve_question(
    identity: str, path: pathlib.Path | None = None, *, now: datetime | None = None
) -> SpendDecision:
    """Atomically check and, if there is room, record one question.

    Checked, not incremented, on a refusal. A caller who is told no and
    retries later must not have paid for the attempt that was refused --
    counting refused questions against the ceiling would let a caller who is
    refused near the limit tip over it purely by trying again to see the
    message a second time.

    Order of the two checks is deliberate and asymmetric. Identity is
    checked first. Three identities at 50 each reach the global ceiling of 150
    with none of them individually over their own limit -- if the global check
    ran first, the caller who happened to ask last would see a refusal that has
    nothing to do with anything they did. Checking identity first means a
    caller is only ever told "you" before being told "everyone", and the
    `category` on the result says which one actually fired.

    A ledger failure fails closed, matching this project's one other spend
    gate. The README calls HTTP Basic itself "a spend gate, not transport
    security"; this is the other half of the same sentence. Silently letting a
    question through because the ledger could not be read would be an
    availability claim -- "no, the ceiling was not exceeded" -- made with no
    evidence for it, which is the same lie D-2 already refuses to tell about an
    unreachable database. `record_ask`'s fail-open policy does not apply here:
    that function protects observability, where a lost row costs nothing; this
    one protects the same free-tier key the auth perimeter exists to protect,
    at the exact moment -- sustained load -- it is most likely to need to.
    """
    identity_limit = _positive_int_env(IDENTITY_LIMIT_ENV, DEFAULT_IDENTITY_LIMIT)
    global_limit = _positive_int_env(GLOBAL_LIMIT_ENV, DEFAULT_GLOBAL_LIMIT)
    usage_date = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")

    try:
        with _write_lock, _connect(path) as conn:
            row = conn.execute(
                "SELECT question_count FROM daily_usage "
                "WHERE usage_date = ? AND identity = ?",
                (usage_date, identity),
            ).fetchone()
            identity_count = row["question_count"] if row else 0

            total_row = conn.execute(
                "SELECT COALESCE(SUM(question_count), 0) AS total "
                "FROM daily_usage WHERE usage_date = ?",
                (usage_date,),
            ).fetchone()
            global_count = total_row["total"]

            if identity_count + 1 > identity_limit:
                return SpendDecision(
                    False, identity_count, global_count, identity_limit,
                    global_limit, CATEGORY_IDENTITY_DAILY_LIMIT,
                )
            if global_count + 1 > global_limit:
                return SpendDecision(
                    False, identity_count, global_count, identity_limit,
                    global_limit, CATEGORY_GLOBAL_DAILY_LIMIT,
                )

            conn.execute(
                "INSERT INTO daily_usage (usage_date, identity, question_count) "
                "VALUES (?, ?, 1) "
                "ON CONFLICT (usage_date, identity) "
                "DO UPDATE SET question_count = question_count + 1",
                (usage_date, identity),
            )
            conn.commit()
            return SpendDecision(
                True, identity_count + 1, global_count + 1, identity_limit, global_limit,
            )
    except sqlite3.Error as exc:
        logger.error("daily spend ceiling unavailable, refusing the question: %s", exc)
        return SpendDecision(
            False, -1, -1, identity_limit, global_limit, CATEGORY_LEDGER_UNAVAILABLE,
        )


def spend_status(path: pathlib.Path | None = None, *, now: datetime | None = None) -> dict:
    """Today's totals against the ceiling, for `/health`. Never raises.

    Read-only, and deliberately not the same code path as `reserve_question`:
    a health check that itself wrote a row would inflate the very count it is
    reporting on.
    """
    global_limit = _positive_int_env(GLOBAL_LIMIT_ENV, DEFAULT_GLOBAL_LIMIT)
    identity_limit = _positive_int_env(IDENTITY_LIMIT_ENV, DEFAULT_IDENTITY_LIMIT)
    usage_date = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    try:
        with _connect(path) as conn:
            total_row = conn.execute(
                "SELECT COALESCE(SUM(question_count), 0) AS total "
                "FROM daily_usage WHERE usage_date = ?",
                (usage_date,),
            ).fetchone()
            return {
                "available": True,
                "global_count": total_row["total"],
                "global_limit": global_limit,
                "identity_limit": identity_limit,
                "error": "",
            }
    except sqlite3.Error as exc:
        return {
            "available": False,
            "global_count": None,
            "global_limit": global_limit,
            "identity_limit": identity_limit,
            "error": f"{type(exc).__name__}: {exc}",
        }
