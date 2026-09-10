-- Operational history. See api/store/__init__.py for why this is not Postgres.
--
-- Applied with executescript() on every connect; every statement is IF NOT
-- EXISTS, so opening an existing database is a no-op rather than a migration.

CREATE TABLE IF NOT EXISTS ask (
    id              TEXT PRIMARY KEY,
    asked_at        TEXT    NOT NULL,
    question        TEXT    NOT NULL,
    ok              INTEGER NOT NULL,
    sql             TEXT    NOT NULL DEFAULT '',
    category        TEXT    NOT NULL DEFAULT '',
    shape           TEXT    NOT NULL DEFAULT '',
    row_count       INTEGER,
    attempts_used   INTEGER NOT NULL DEFAULT 0,

    -- AC1/AC2. Kept apart because 010 section 2.2 measured the provider at
    -- 94.2% of wall clock: a single duration would hide the only number that
    -- moves, and invite optimising the 6% that cannot matter.
    total_ms        INTEGER NOT NULL,
    provider_ms     INTEGER NOT NULL,

    -- AC1. `usage_measured` follows D-1's standing precedent: a locally counted
    -- number and a provider-billed number are different quantities, and a
    -- record that does not say which it holds is not a measurement.
    total_tokens    INTEGER,
    prompt_tokens   INTEGER,
    usage_measured  INTEGER NOT NULL DEFAULT 0,
    provider_calls  INTEGER NOT NULL DEFAULT 0,
    cache_hit       INTEGER NOT NULL DEFAULT 0,

    -- AC3. What explains a slow answer. 010 section 2.3 measured a fourteen-fold
    -- latency climb with the work held constant; without these two columns a
    -- latency log cannot tell throttling from a hard question.
    tpm_remaining   INTEGER,
    rpd_remaining   INTEGER,

    -- Fingerprints, so a row stays comparable the way an EVALS.md entry does.
    model           TEXT    NOT NULL DEFAULT '',
    schema_fp       TEXT    NOT NULL DEFAULT '',
    prompt_fp       TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ask_asked_at ON ask (asked_at DESC);

-- What a person thought of an answer (Iteration 8 T6, AC13). Attached to the
-- answer id and never to the question text, which is why `ask.id` was made a
-- uuid returned in the /ask payload at Iteration 7 T3 rather than left an
-- implementation detail: the same question asked twice is two answers, and a
-- mark belongs to the one that was actually shown.
--
-- **Append-only, for the reason EVALS.md is.** Several marks on one answer are
-- allowed and none replaces another. A mark is a record of what somebody
-- thought at a moment, and letting a later one overwrite an earlier one would
-- quietly destroy the disagreement -- which, on a corpus that section 2.6
-- measured as containing zero bad answers, is the most interesting signal there
-- could be.
--
-- `rating` is an INTEGER holding -1 or 1 (resolved D-6). Two values cannot be
-- averaged into something that looks like an accuracy figure, which is the
-- pressure AC14 exists to resist, and the column type means a finer scale can
-- arrive later without a migration.
--
-- The REFERENCES clause is **documentation, not enforcement**: this store never
-- issues `PRAGMA foreign_keys=ON`, so SQLite does not check it -- the same is
-- already true of `step` below. Unknown ids are refused in the application
-- instead, which is also what produces AC13's 404 rather than an
-- IntegrityError. Turning the pragma on is a change to how the whole store
-- behaves and is deliberately not bundled into this task.
CREATE TABLE IF NOT EXISTS feedback (
    id         TEXT    PRIMARY KEY,
    ask_id     TEXT    NOT NULL REFERENCES ask (id),
    created_at TEXT    NOT NULL,
    rating     INTEGER NOT NULL,
    note       TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS feedback_ask ON feedback (ask_id);

-- The trace: one row per attempt. This is what makes a retry visible after the
-- fact, which is charter section 1's whole claim about being an agent.
CREATE TABLE IF NOT EXISTS step (
    ask_id   TEXT    NOT NULL REFERENCES ask (id),
    attempt  INTEGER NOT NULL,
    action   TEXT    NOT NULL,
    ok       INTEGER NOT NULL DEFAULT 0,
    category TEXT    NOT NULL DEFAULT '',
    error    TEXT    NOT NULL DEFAULT '',
    sql      TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (ask_id, attempt)
);
