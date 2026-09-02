# 003 — Query Execution: Implementation and Test Plan

> **STATUS: AWAITING YOUR APPROVAL.** No application code exists against this.
> Spec: [`003-execute-sql.md`](003-execute-sql.md).
>
> **§3 is the part to read.** It proposes one addition the spec does not have,
> arising from a measurement: PostgreSQL gives read-only violations their own
> SQLSTATE, which turns them into a working canary for a Gate 2 defect. §8 asks
> you for two decisions.

---

## 1. Approach

Every mechanism below was measured through SQLAlchemy 2.0.52 / psycopg 3.2.13
against the running container while drafting. Nothing here is inferred from
documentation.

```python
sql -> validate_sql()            # AC1-AC4, before any connection
    -> engine.connect()
       .execution_options(stream_results=True)      # AC14, server-side cursor
    -> conn.begin()                                 # AC5, autocommit off
    -> SET TRANSACTION READ ONLY                    # AC5, Gate 1b
    -> SET LOCAL statement_timeout = '10s'          # AC9, Gate 3
    -> execute, fetchmany(MAX_ROWS + 1)             # AC11-AC13
    -> trans.rollback()                             # AC6, always
    -> ExecutionResult
```

---

## 2. Measured facts the implementation depends on

### Read-only: two mechanisms work, one is better

| Approach | Read-only in force | Leaks to next pool checkout |
|---|---|---|
| `execution_options(postgresql_readonly=True)` | yes | no |
| explicit `SET TRANSACTION READ ONLY` in `begin()` | yes | no |

Both satisfy AC5 and AC8. **Recommending the explicit statement**, for a reason
the table does not show: `postgresql_readonly` is implemented as a *session*
characteristic that SQLAlchemy undoes via reset-on-return. It stops leaking only
because the pool resets it. `SET TRANSACTION READ ONLY` is scoped to the
transaction by PostgreSQL itself, so it holds even if pool reset behaviour is
ever reconfigured (`pool_reset_on_return=None` is a legal setting). On a
security boundary, prefer the guarantee that does not depend on our own pool
configuration.

Confirmed in force: `CREATE TABLE` inside the transaction returned
`cannot execute CREATE TABLE in a read-only transaction`, and the next checkout
showed `read_only=off`, `timeout=10s`.

### Streaming and truncation

`execution_options(stream_results=True)` plus `fetchmany(MAX_ROWS + 1)` behaves
exactly as AC13 wants — asking for 6 over a 3,503-row table returned 6, so
`len(rows) > MAX_ROWS` is the truncation signal, with no second `COUNT(*)`.

Column names are available from `result.keys()` **even when zero rows match**
(`SELECT … WHERE 1=0` → `rows=0, columns=['track_id','name']`), so AC18 needs no
special handling.

### Errors carry SQLSTATE, and that is the whole categorisation strategy

Reached via `exc.orig.sqlstate` and `exc.orig.diag.message_hint`:

| Input | SQLSTATE | Hint |
|---|---|---|
| `SELECT artist_name FROM album` | `42703` | none |
| `SELECT * FROM tracks` | `42P01` | none |
| `SELECT count(*) FROM track GROUP BY nope` | `42703` | *Perhaps you meant to reference the column "track.name".* |
| `SELECT pg_sleep(3)` under a 250ms limit | **`57014`** | none |
| `DELETE FROM track` in a read-only transaction | **`25006`** | none |
| unreachable server | **`None`** (`orig=ConnectionTimeout`) | — |

So the mapping in AC19 is driven entirely by SQLSTATE, with **no message-text
matching anywhere** — the same discipline `002` applied to statement types:

```
57014            -> "timeout"
25006            -> see §3
sqlstate present -> "database_error"
sqlstate absent  -> "connection_error"
```

---

## 3. A case the spec did not anticipate: `25006` is a canary

PostgreSQL gives read-only violations their own SQLSTATE, `25006`
(`read_only_sql_transaction`). That is more useful than it first looks.

By the time a query reaches the transaction, Gate 2 has already accepted it as a
single read-only `SELECT`. So **a `25006` can only mean Gate 2 let a write
through.** It is not a normal database error and it is not the model's mistake —
it is a defect in the validator, of exactly the class
[`000-project.md` §2](000-project.md) calls a stop-the-line event.

Folding it into `database_error` would hand it to the retry loop, which would
dutifully ask the model to rewrite a query that Gate 2 should never have passed.
The bug would look like an accuracy problem and could sit there for iterations.

**Proposed: a distinct `gate_violation` category**, with:

- the reason stating plainly that Gate 2 accepted a statement the database
  refused as a write, and naming the SQLSTATE;
- a `logging.error` — the only place in this module that logs at error level,
  because it is the only condition that means the safety layer is wrong rather
  than the model;
- an explicit test asserting the category exists and is not reachable by any
  currently known input.

This is a spec amendment (a 26th criterion), so it is yours to accept or reject
rather than mine to add. **Decision D-1 in §8.**

---

## 4. Files

| File | Status | Contents |
|---|---|---|
| `api/db/execution.py` | **new** | `ExecutionResult`, `execute_sql()`, `MAX_ROWS`, `STATEMENT_TIMEOUT`, SQLSTATE constants |
| `api/agent/tools.py` | edit | Register `execute_sql`; third entry in `TOOLS` |
| `tests/test_execution.py` | **new** | Acceptance tests; needs a live database |
| `tests/test_validator_gates.py` | edit | Extend gate-independence coverage through `execute_sql()` |
| `tests/test_tools.py` | edit | Registry now has three entries |

Not touched: `api/safety/validator.py`, `api/db/introspection.py`,
`api/db/engine.py`, `docker-compose.yml`.

`api/db/execution.py` rather than `api/safety/` — this is database plumbing that
*consults* the safety layer, not part of it. Gate 2 stays in one file.

---

## 5. Structure

```python
MAX_ROWS = 1000                    # resolved Q-A
STATEMENT_TIMEOUT = "10s"          # resolved Q-C; matches the role default

SQLSTATE_QUERY_CANCELED = "57014"
SQLSTATE_READ_ONLY_TRANSACTION = "25006"

def execute_sql(sql: str) -> ExecutionResult:
    ok, reason = validate_sql(sql)          # AC1-AC4, before connecting
    if not ok:
        return ExecutionResult(ok=False, category="rejected", error=reason)

    try:
        with engine.connect().execution_options(stream_results=True) as conn:
            trans = conn.begin()
            try:
                conn.execute(text("SET TRANSACTION READ ONLY"))
                conn.execute(text(f"SET LOCAL statement_timeout = '{...}'"))
                result = conn.execute(text(sql))
                rows = result.fetchmany(MAX_ROWS + 1)
                ...
            finally:
                trans.rollback()            # AC6, both paths
    except SQLAlchemyError as exc:
        return _categorise(exc)
```

Two details that are easy to get wrong:

- **`SET LOCAL` is a no-op outside a transaction**, and PostgreSQL only warns.
  It must be issued after `conn.begin()`, never on an autocommit connection, or
  Gate 3 silently does nothing. A test asserts the timeout is actually in force
  rather than merely issued.
- **`STATEMENT_TIMEOUT` is interpolated into SQL.** It is a module constant, not
  input, but interpolating anything into a statement in this project deserves a
  comment saying why it is not the thing the whole safety layer exists to
  prevent. Alternative: bind it via `SET LOCAL statement_timeout = :t` — worth
  checking whether PostgreSQL accepts a parameter there, and using it if so.

---

## 6. Test plan

`tests/test_execution.py`, live database, one test per criterion.

**Success paths**
- single row, many rows, zero rows (columns still present — AC18)
- native types survive: `unit_price` is `Decimal`, `invoice_date` is `datetime`
  (resolved Q-B) — a test asserting `isinstance`, so a future "helpful"
  `float()` conversion fails loudly
- the `invoice_totals` view is queryable
- a `UNION` query executes (ties `002` AC25 to real execution)

**Row cap and truncation**
- 1,001+ row query returns exactly `MAX_ROWS`, `truncated=True`
- exactly-`MAX_ROWS` query returns `truncated=False` — the boundary, where an
  off-by-one would either lose a row or claim a false truncation
- a query with its own smaller `LIMIT` is untouched, `truncated=False`
- the executed SQL is unchanged — no injected `LIMIT` (AC15)

**Guardrails**
- `pg_sleep` beyond a short `SET LOCAL` value → `category="timeout"`,
  SQLSTATE `57014`. Uses a short timeout so the suite does not pay 10s
- write attempt through the tool → refused; asserts the read-only refusal is
  distinguishable from the privilege refusal
- after a call, a fresh connection shows `read_only=off`, `timeout=10s` (AC8)

**Failure paths**
- bad column → `database_error`, sqlstate `42703`
- bad table → `database_error`, sqlstate `42P01`
- hint preserved when offered; absent hint is not an error (AC21)
- unreachable database → `connection_error`, no sqlstate
- no message contains the DSN, password, host, or `Traceback` (AC22)

**Mutation checks** — the discipline that caught two removable defences in `002`:

| Mutation | Expected |
|---|---|
| Remove the `validate_sql()` call | A test proving `DROP TABLE track` never reaches the database goes red |
| Drop `SET TRANSACTION READ ONLY` | The AC7 test goes red |
| `commit()` instead of `rollback()` | A test goes red |
| Remove the row cap | Truncation tests go red |
| Remove `SET LOCAL statement_timeout` | The timeout test goes red |
| Return `MAX_ROWS + 1` rows | The boundary test goes red |

Any mutation that fails nothing means the defence is untested, and the test gets
written rather than the finding dropped.

---

## 7. Risks

| Risk | Handling |
|---|---|
| **A half-built `execute_sql` could run unvalidated SQL.** `002` was safe to build incrementally because it only ever *returned verdicts*; this one connects to a database | Build validation-first: the very first version calls `validate_sql` and returns `rejected` or a not-implemented failure. No intermediate state executes anything |
| `fetchmany` on a streamed result still lets the server compute the full set | Accepted, and bounded by `statement_timeout`. Client memory is what the cap protects; server work is Gate 3's problem |
| The timeout test could be slow | Short `SET LOCAL` value in tests, never the production 10s |
| A leaked connection setting would poison later queries silently | AC8 has an explicit test; both mechanisms measured clean |
| `stream_results` behaviour differs across drivers | Pinned `psycopg>=3.2,<3.3`, `sqlalchemy>=2.0,<2.1`; the test suite exercises it on every run |

---

## 8. Decisions I need from you

**D-1 — Adopt the `gate_violation` category from §3?**
It adds a 26th acceptance criterion to an approved spec. *My lean: yes.* SQLSTATE
`25006` is otherwise indistinguishable from a routine query error, and it is the
one signal that means the validator is broken rather than the model. The cost is
one category and one test; the cost of not having it is a safety defect that
looks like an accuracy problem.

**D-2 — Should `execute_sql` be registered in `TOOLS` now?**
Consistent with `get_schema` and `validate_sql`, and with the §5 convention that
`tools.py` is the agent's full surface. The counter-argument is sharper here than
it was for the other two: registering it makes "run this SQL" reachable by name
one iteration before any loop exists to call it responsibly.
*My lean: register it.* The registry is documentation, nothing dispatches from it
until Iteration 4, and a tool surface missing its most consequential entry is
misleading to the next reader.

---

## 8b. Task log — ALL COMPLETE

**All 26 acceptance criteria covered. 281 tests passing, 1 skipped.**

| Task | Status | Outcome |
|---|---|---|
| **T1** | done | Fail-closed: validates, executes nothing. Mutation removing validation failed **18 tests** |
| **T2** | done | Read-only transaction, `SET LOCAL` timeout, always-rollback |
| **T3+T4** | done | Merged, deliberately — see below |
| **T5+T6+T7** | done | SQLSTATE categorisation including `gate_violation`; they are one function |
| **T8** | done | Hostile input, determinism, no-retry |
| **T9** | done | Gate independence through `execute_sql` |
| **T10** | done | `execute_sql` registered; `TOOLS` now has three entries |

**T3 and T4 were merged**, for the reason given in §7: a T3 that did an unbounded
`fetchall()` could exhaust memory on `SELECT * FROM track, genre`, and that state
should not exist even briefly. Same judgement as `002`'s T3+T4.

### Findings

**`stream_results` must go on the statement, not the connection.** Set at
connection level it applies to *every* statement, and SQLAlchemy wraps each in
`DECLARE … CURSOR FOR` — a syntax error for `SET TRANSACTION READ ONLY`. The
guards in `_harden()` failed before any query ran. Fixed with
`text(sql).execution_options(stream_results=True)`.

**The timeout test was masked by the role default, and mutation caught it.**
`test_ac9` asserted `SHOW statement_timeout == "10s"` — which is *also* what
`db/init/03_readonly_role.sh` pins on the role. Deleting the entire `SET LOCAL`
line **failed nothing**. Gate 3's explicit call site was untested while looking
covered. Fixed by overriding the constant to `250ms`, a value no role default
supplies; the mutation now fails two tests.

That is the third time on this project that mutation testing found a defence no
review caught, and the second time a *default elsewhere in the system* was
silently standing in for the code under test.

**The off-by-one is real.** Changing `len(fetched) > MAX_ROWS` to `>=` fails
exactly the boundary test written for it — a query returning precisely
`MAX_ROWS` rows would otherwise claim a truncation that never happened.

**Connection errors leak host and port.** psycopg's message embeds
`host: 'localhost', port: 5432, hostaddr: '::1'`. AC22 is satisfied by using
`diag.message_primary` for server errors and a fixed message for connection
failures, never the raw exception text.

---

## 9. Decomposition (as executed)

One task, one testable behaviour. I stop after each.

| # | Task | Verified by |
|---|---|---|
| **T1** | `ExecutionResult` + constants; `execute_sql` validates and returns `rejected`, executing nothing | AC1–AC4, and the fail-closed invariant from §7 |
| **T2** | Connection, read-only transaction, always-rollback | AC5, AC6, AC7, AC8 |
| **T3** | Execute and return rows; columns on empty results | AC16, AC17, AC18, native types |
| **T4** | Row cap and truncation, including the boundary | AC11, AC12, AC13, AC15 |
| **T5** | `SET LOCAL statement_timeout` and the timeout category | AC9, AC10 |
| **T6** | SQLSTATE-driven categorisation, hint and message passthrough | AC19, AC20, AC21, AC22 |
| **T7** | `gate_violation` (if D-1 is yes) | §3 |
| **T8** | Hostile input, determinism, no internal retries | AC23, AC24, AC25 |
| **T9** | Gate-independence tests through `execute_sql` | Extends `test_validator_gates.py` |
| **T10** | Register in `TOOLS` (if D-2 is yes) | Registry has three entries |

T1 first and alone, because it establishes that no version of this file executes
SQL without validating it.
