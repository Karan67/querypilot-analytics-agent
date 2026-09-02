# 003 — Query Execution (`execute_sql()`)

> **STATUS: IMPLEMENTED 2026-08-21.** All 26 acceptance criteria covered.
> Implementation: `api/db/execution.py`, registered in `api/agent/tools.py`.
> Plan and task log: [`003-execute-sql-plan.md`](003-execute-sql-plan.md).
> Plan: [`003-execute-sql-plan.md`](003-execute-sql-plan.md).
>
> Everything asserted about PostgreSQL behaviour in §2 and §5 was **measured
> against the running container while drafting**, not recalled. The measurements
> are shown inline so the plan does not have to re-derive them.
>
> §7 asks you for five decisions. **Q-B is the one that will bite later** if it
> goes the wrong way.

Iteration: 1 (Tools before agent) · Inherits: [`000-project.md`](000-project.md)
Depends on: [`002-sql-validation.md`](002-sql-validation.md) — Gate 2 exists, so
this may now be built.

---

## 1. Intent

`execute_sql()` is the only place in QueryPilot where generated SQL touches the
database. Everything before it is preparation; everything after it is
presentation.

That makes it the highest-consequence tool in the project. `get_schema()` reads
the catalog. `validate_sql()` reads a string. This one hands model output to a
live server — so it is the last place a mistake stays theoretical.

Its second job is almost as important: **turning failure into something the
agent can act on.** A query that fails must come back as a *specific* diagnosis —
`column "artist_name" does not exist` — because that message is the input to the
retry that fixes it. Iteration 4's self-correction loop is built on the quality
of what this function returns when things go wrong.

---

## 2. Position in the pipeline

```
model output
     │
     ▼
┌─────────────────────────────────────────────────────────┐
│ execute_sql()                                            │
│                                                          │
│  1. validate_sql()          Gate 2 — no bypass, no flag  │
│  2. BEGIN … READ ONLY       Gate 1b — blocks even a      │
│                                      superuser           │
│  3. SET LOCAL statement_timeout      Gate 3 — scoped     │
│  4. stream rows, cap at MAX_ROWS     Gate 3 — memory     │
│  5. ROLLBACK  (always, both paths)                       │
└─────────────────────────────────────────────────────────┘
     │
     ▼
ExecutionResult  — success rows, or a categorised failure
```

### The read-only transaction is a genuinely independent gate

Measured as the **superuser** `querypilot`, inside `SET TRANSACTION READ ONLY`:

| Statement | Result |
|---|---|
| `CREATE TABLE probe_evil (id int)` | `cannot execute CREATE TABLE in a read-only transaction` |
| `DELETE FROM track WHERE track_id = -1` | `cannot execute DELETE in a read-only transaction` |
| `WITH g AS (DELETE … RETURNING 1) SELECT …` | `cannot execute SELECT in a read-only transaction` |
| `SELECT track_id INTO probe_evil2 FROM track` | `cannot execute SELECT INTO in a read-only transaction` |

This matters because it does **not** depend on role privileges. If
`querypilot_ro` were ever misconfigured — a stray `GRANT`, a restore that
recreated the role, someone pointing the API at a privileged DSN — the read-only
transaction still refuses. It is a fourth layer, not a restatement of Gate 1.

### `SET LOCAL` is transaction-scoped, which matters for pooling

Measured on the read-only role:

```
inside transaction : 300ms        SET LOCAL statement_timeout = '300ms'
pg_sleep(3)        : killed after 0.32s
after rollback     : 10s          role default restored
```

The value does not leak into the pooled connection. A per-call timeout is
therefore safe, where `SET` (without `LOCAL`) would poison every later query on
that connection.

**Note this is our own `SET`, issued by the tool, never by generated SQL** —
Gate 2 rejects any `SET` in model output (AC14 of `002`), and that rejection is
what keeps this timeout enforceable.

---

## 3. Acceptance criteria

### Gate 2 interception

- **AC1** — `execute_sql()` calls `validate_sql()` itself. A rejected query never
  reaches the database, and the caller is not trusted to have validated first.
- **AC2** — **There is no bypass.** No parameter, keyword argument, environment
  variable, or module-level toggle skips validation. Not for tests, not for
  `run_evals.py`, not for debugging. A `skip_validation=True` argument would
  defeat the entire safety layer and must never exist.
- **AC3** — Validation runs *before a connection is acquired*. A rejected query
  costs no connection and touches no pool.
- **AC4** — A rejection returns a structured failure carrying the validator's
  reason **verbatim**. It is not re-worded, summarised, or truncated — the agent
  retries against that text, and `002` spent its whole design budget making it
  actionable.

### Gate 1 enforcement — the read-only transaction

- **AC5** — Every execution runs inside an explicit transaction marked
  `READ ONLY`. Autocommit is off.
- **AC6** — The transaction is **always rolled back** — on success as well as on
  failure. Nothing this tool does is ever committed. A `SELECT` needs no commit,
  and never committing removes the possibility of one appearing by accident.
- **AC7** — The read-only property is verified against the database, not assumed:
  a test asserts a write attempt inside the tool's own transaction is refused
  with `read-only transaction`, distinct from Gate 1's `permission denied`.
- **AC8** — The setting does not leak. After a call, a fresh checkout of the same
  pooled connection is unaffected.

### Execution guardrails

- **AC9** — `SET LOCAL statement_timeout` is applied inside the transaction on
  every call, so the limit is explicit at the call site rather than inherited
  from role configuration that a future migration might not reproduce.
- **AC10** — A query exceeding the timeout returns a failure categorised as a
  timeout, not a generic database error. The agent's correct response is to
  narrow the query, which differs from its response to a bad column name.
- **AC11** — At most `MAX_ROWS` rows are returned. **Resolved Q-A: 1000.**
- **AC12** — When more rows existed, the result says so via a `truncated` flag.
  Silently returning a capped result set would let the agent draw a conclusion
  from partial data and state it as fact — worse than an error.
- **AC13** — Truncation is detected by fetching `MAX_ROWS + 1` and returning
  `MAX_ROWS`, not by comparing against a separate `COUNT(*)`, which would double
  the work and could disagree with the result under concurrency.
- **AC14** — Rows are streamed with a server-side cursor. Measured on a
  deliberate cross join, `fetchmany(5)` took **0.13s** through a client-side
  cursor versus **0.02s** server-side, because the client-side path materialises
  the whole result before the first row is available. The gap grows with result
  size, and that is the memory exhaustion the cap is supposed to prevent.
- **AC15** — **No `LIMIT` is injected into the SQL.** The capping happens on the
  fetch, so the SQL displayed to the user is byte-identical to the SQL that ran.
  This follows Q-A of `002`: validation and execution do not rewrite queries.

### Structured output

- **AC16** — Returns a frozen result object. Expected failures — rejection,
  timeout, bad column, unreachable database — are *return values*, never
  exceptions.
- **AC17** — A success carries: column names, rows, the row count returned, and
  the truncation flag.
- **AC18** — Column names are present even when zero rows come back. An empty
  result is a **success**, not an error, and the agent needs the columns to
  explain that the answer is "none".
- **AC19** — A failure carries a machine-readable category, at minimum
  distinguishing: `rejected` (Gate 2), `timeout`, `database_error` (the query ran
  and Postgres refused it), and `connection_error`. The retry loop responds
  differently to each, and matching on message text would be exactly the
  brittleness `002` refused.
- **AC20** — The PostgreSQL **SQLSTATE** is preserved on database errors.
  Measured: `42703` for `column "artist_name" does not exist`, `42P01` for
  `relation "tracks" does not exist`. These are stable, machine-readable, and do
  not change with server locale or version the way message text does.
- **AC21** — The PostgreSQL **HINT** is preserved when present, because it is
  frequently the answer. Measured on `SELECT count(*) FROM track GROUP BY nope`:

  > *Perhaps you meant to reference the column `track.name`.*

  **But it is not always present** — `SELECT artist_name FROM album` produced no
  hint at all. So the hint is passed through when offered and never depended on.
- **AC22** — No message ever contains the DSN, credentials, host, port, or a
  Python traceback. The agent's context is model input, and from Iteration 6 it
  is also streamed to a browser.
- **AC23** — Never raises, on any input. The same rule as `validate_sql`: a crash
  here is a denial of service on the API.
- **AC24** — **No internal retries.** The tool executes once and reports. Retry
  policy belongs to the agent loop in Iteration 4, and burying a hidden retry
  here would corrupt both the latency numbers and the eval accounting.
- **AC25** — Deterministic for a given database state.

- **AC26 — A read-only violation is reported as a gate failure, not a query
  error.** PostgreSQL raises SQLSTATE `25006` (`read_only_sql_transaction`) when
  a write is attempted inside the tool's transaction. By that point Gate 2 has
  already certified the statement as a single read-only `SELECT`, so **`25006`
  can only mean Gate 2 let a write through** — a defect in the validator, not a
  mistake by the model.

  It therefore gets its own category, `gate_violation`, and is the one condition
  in this module logged at `error` level. Folding it into `database_error` would
  hand it to the retry loop, which would ask the model to rewrite a query that
  should never have passed validation — turning a stop-the-line safety defect
  ([`000-project.md` §2](000-project.md)) into what looks like an accuracy
  problem, potentially for iterations.

  No known input reaches this state. That is the point: it is a canary, and a
  test asserts the category exists and is unreachable.

---

## 4. Non-goals

| Excluded | Why |
|---|---|
| **Bind parameters / parameterised queries** | Not applicable, and worth stating so nobody "fixes" it. The usual injection defence binds *user input* into a fixed query skeleton — but here the model generates the entire statement, so there is no user-supplied value to bind. That is precisely why the AST gate exists instead |
| Retry logic | Iteration 4. See AC24 |
| Caching repeated queries | Iteration 7 |
| Persisting history, latency, token cost | Iteration 7 |
| Pagination or cursors across calls | The cap is a safety limit, not a paging API. If the agent needs fewer rows it should say so in SQL |
| Injecting `LIMIT` | AC15 |
| Formatting results, prose summaries, chart selection | Iterations 5 and 6 |
| `EXPLAIN` / cost estimation before running | Gate 3's timeout already bounds cost, and `002` AC13 rejects `EXPLAIN` outright |
| Schema-aware pre-checks ("does this column exist?") | The database answers that authoritatively, with a better message than we could synthesise. Accuracy work, Iteration 5 |
| Write support, in any form | Categorically out |

---

## 5. Contracts

```python
# api/db/execution.py   — implementation  (per 000-project.md §5)
# api/agent/tools.py    — thin registered wrapper

@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    columns: tuple[str, ...] = ()
    rows: tuple[tuple, ...] = ()  # native Python types: Decimal, datetime, ...
    row_count: int = 0
    truncated: bool = False        # more rows existed than MAX_ROWS (1000)
    category: str = ""      # "" on success; see AC19 otherwise
    error: str = ""         # "" on success
    sqlstate: str = ""      # PostgreSQL SQLSTATE, when the server supplied one
    hint: str = ""          # PostgreSQL HINT, when the server supplied one


def execute_sql(sql: str) -> ExecutionResult:
    """Validate, then run one read-only query. Never raises (AC23)."""
```

**Failure categories** (AC19):

| Category | Meaning | The agent's correct next move |
|---|---|---|
| `rejected` | Gate 2 refused it | Rewrite the query; it was never run |
| `timeout` | Exceeded `statement_timeout` | Narrow it — add filters, reduce joins |
| `database_error` | Postgres ran and refused it | Fix the reference; `sqlstate` and `hint` say how |
| `gate_violation` | SQLSTATE `25006` — Gate 2 passed a write | **Stop.** Not a model error. The validator is broken |
| `connection_error` | Could not reach the database | Not the model's fault; stop retrying |

That last distinction earns its place: without it the loop would burn its retry
budget rewriting a perfectly good query while the database is down.

---

## 6. Verification

- `tests/test_execution.py` — needs a live database, like `001`.
- **Mutation checks on the load-bearing gates**, as in `002`:

  | Mutation | Expected |
  |---|---|
  | Remove the `validate_sql()` call | A test proving a `DROP` reaches the database must go red |
  | Drop `READ ONLY` from the transaction | The AC7 test must go red |
  | Replace `rollback()` with `commit()` | A test must go red |
  | Remove the row cap | AC11/AC12 must go red |
  | Remove `SET LOCAL statement_timeout` | AC10 must go red |

  Twice now on `002` a green suite hid a removable defence. This is the module
  where that would matter most.
- **Gate independence**, extending `tests/test_validator_gates.py`: a write
  attempted through `execute_sql()` must be refused by *both* the read-only
  transaction and the role, and the test should show they are different refusals.
- **The timeout test must be fast.** Use a short `SET LOCAL` value against
  `pg_sleep`, not the 10s production default, or the suite pays 10 seconds per
  run.

---

## 7. Decisions — RESOLVED 2026-08-21

- **Q-A — `MAX_ROWS` = 1000.** Balances analytical completeness against memory
  safety.
- **Q-B — Native Python types.** `Decimal` and `datetime` are returned as-is.
  Numeric fidelity is preserved here and serialisation to JSON primitives happens
  at the API / SSE presentation boundary, never inside the execution tool. A tool
  that quietly degraded `Decimal` to `float` underneath an analytics product
  would be the wrong default.
- **Q-C — Fixed module constant**, scoped per transaction via `SET LOCAL`.
- **Q-D — No `duration_ms`.** `ExecutionResult` stays deterministic per AC25;
  telemetry is deferred to Iteration 7 with the rest of the observability work.
- **Q-E — No SQL echo.** The caller already holds the query string, and a second
  copy invites divergence.

<details>
<summary>Original framing of all five, kept for the record</summary>

### Open questions

- **Q-A — What is `MAX_ROWS`?** It bounds memory and response size, and it is the
  number a truncation notice reports. *My lean: 1000.* Large enough that
  realistic analytics answers are never truncated, small enough to be a
  meaningful ceiling.

- **Q-B — Native Python types, or JSON-ready primitives?** Postgres returns
  `Decimal` for `NUMERIC`, `datetime` for `TIMESTAMP`. Neither is JSON
  serialisable, and from Iteration 6 these rows are streamed to a browser over
  SSE. Either this tool normalises them, or every consumer does.
  *My lean: keep native types here* and convert once at the API boundary —
  `Decimal` → `float` loses precision, and a tool that quietly degrades numeric
  results is a bad thing to have underneath an analytics product. **But decide it
  now**: retrofitting a type contract after the eval suite and the frontend both
  depend on it is expensive.

- **Q-C — Fixed timeout, or per-call override?** A fixed constant is simpler and
  harder to misuse; an override lets the eval suite run faster and lets a future
  "this is a big one" path exist. *My lean: one module constant now*, revisit if
  Iteration 3 finds evals too slow.

- **Q-D — Include `duration_ms` in the result?** Iteration 7 needs latency
  logging, and capturing it here is nearly free. Against: it is observability
  scope arriving five iterations early, and it makes the result non-deterministic
  in a way AC25 has to carve out. *My lean: leave it out*, add it with the rest
  of the observability work.

- **Q-E — Should the result echo the SQL that ran?** It makes the object
  self-contained for logging and for the UI. Against: the caller already has it,
  and echoing invites divergence between the two copies.
  *My lean: no.*

---

</details>

---

## 8. Relationship to the rest of Iteration 1

`sample_rows()` is the one remaining tool with no spec. It is lower-stakes — it
reads a fixed number of rows from a named table rather than executing generated
SQL — but it has one property worth deciding deliberately: it takes a **table
name from the model**, which is the only identifier in this project that flows
from model output into SQL. That deserves its own spec rather than being folded
in here.
