# 004 — Row Sampling: Implementation and Test Plan

> **STATUS: COMPLETE.** All 18 acceptance criteria covered; 348 tests passing.
> Spec: [`004-sample-rows.md`](004-sample-rows.md).
>
> **§2 goes further than the spec asked for**, and is the part to check: your
> Q-C answer makes it possible to remove string assembly from this module
> entirely rather than merely quoting safely. §7 asks for one decision.

---

## 1. Approach

```
table_name
   ├─ not a str                        -> unknown_relation      (AC17)
   ├─ not in get_schema() relations     -> unknown_relation      (AC1-AC3, AC6)
   ▼
build the query as a sqlglot AST                                 (AC7-AC9, AC14-AC15)
   ▼
execute_sql(generated_sql)                                       (AC10-AC12)
   ▼
ExecutionResult
```

No connection is opened here, no SQL string is assembled, and every guarantee
`003` established is inherited rather than re-implemented.

---

## 2. Beyond quoting: build the whole query as an AST

The spec asked for the identifier to be quoted by a library rather than
formatted. Your Q-C answer — sqlglot, the same engine Gate 2 parses with —
allows something stronger: **construct the entire statement as a syntax tree and
let sqlglot generate the SQL.** There is then no string containing the table name
at any point, because there is no string assembly at all.

```python
exp.select(exp.Star())
   .from_(exp.to_identifier(relation, quoted=True))
   .order_by(*[exp.to_identifier(c, quoted=True) for c in order_columns])
   .limit(SAMPLE_ROW_COUNT)
   .sql(dialect="postgres")
```

Measured against the live schema:

| Relation | Generated |
|---|---|
| `track` (single PK) | `SELECT * FROM "track" ORDER BY "track_id" LIMIT 3` |
| `playlist_track` (composite PK) | `SELECT * FROM "playlist_track" ORDER BY "playlist_id", "track_id" LIMIT 3` |
| `invoice_totals` (view, no PK) | `SELECT * FROM "invoice_totals" ORDER BY "invoice_id", "customer_id", "invoice_date", "line_count", "total" LIMIT 3` |

All three pass Gate 2 and execute: 3 rows each, 9 / 2 / 5 columns.

### The containment proof

Building a query for the hostile name `track"; DROP TABLE track; --` and parsing
the result back gives:

```
SELECT * FROM "track""; DROP TABLE track; --" ORDER BY "x" LIMIT 3

parser sees tables: ['track"; DROP TABLE track; --']     <- one table, that literal name
```

**One table, whose name is exactly the hostile string.** The `DROP TABLE` is
inside an identifier, not a statement — and the proof is a round trip through the
parser rather than an assertion about quote characters. That is the test worth
having (AC9): it demonstrates *containment*, where asserting on doubled quotes
would only demonstrate that the quoting function was called.

### Determinism, measured

Three consecutive runs, distinct results:

| Query | Distinct results over 3 runs |
|---|---|
| `track`, ordered by primary key | **1** |
| `invoice_totals`, ordered by all five columns | **1** |

---

## 3. Files

| File | Status | Contents |
|---|---|---|
| `api/db/sampling.py` | **new** | `SAMPLE_ROW_COUNT`, `CATEGORY_UNKNOWN_RELATION`, `_build_sample_query()`, `sample_rows()` |
| `api/agent/tools.py` | edit | Register `sample_rows`; fourth and final entry in `TOOLS` |
| `tests/test_sampling.py` | **new** | Acceptance tests; needs a live database |
| `tests/test_tools.py` | edit | Registry now complete at four |

Not touched: `api/db/execution.py`, `api/safety/validator.py`,
`api/db/introspection.py`, `api/db/engine.py`.

`api/db/sampling.py` rather than folding into `execution.py`: this module has a
different input problem (an identifier, not a statement) and a different failure
mode (`unknown_relation`). Keeping them apart means `execute_sql` stays readable
as the one place SQL meets the database.

---

## 4. Structure

```python
SAMPLE_ROW_COUNT = 3
CATEGORY_UNKNOWN_RELATION = "unknown_relation"
MAX_LISTED_RELATIONS = 25          # see D-1

def _order_columns(table) -> tuple[str, ...]:
    """Primary key if there is one, otherwise every column (AC14, AC15)."""

def _build_sample_query(relation, order_columns) -> str:
    """AST in, SQL out. No string assembly (AC7-AC9)."""

def sample_rows(table_name: str) -> ExecutionResult:
    """Allowlist, build, delegate to execute_sql (AC1-AC6, AC10)."""
```

Three details worth getting right:

- **The allowlist compares exactly** (AC2). `table_name in {t.name for t in
  schema.tables}` — no `.lower()`, no `.strip()`. PostgreSQL permits `track` and
  `Track` as distinct relations, and folding would let one be reached by asking
  for the other.
- **`get_schema()` can fail.** It raises `SchemaIntrospectionError` when the
  database is unreachable. That must become a `connection_error` result, not an
  exception (AC17) — the tool cannot know the allowlist, so it cannot proceed,
  but it also must not raise.
- **`get_schema()` is called on every invocation**, costing a catalog round trip
  before the sample query. That is the cost of `001`'s no-cache decision (Q-C
  there) and is correct for now; Iteration 7 revisits caching with measurements.

---

## 5. Test plan

`tests/test_sampling.py`, live database.

**The allowlist**
- every relation from `get_schema()` samples successfully, including the view
- `pg_stat_activity`, `pg_class`, `pg_roles`, `information_schema.tables` are all
  refused — each measured readable by the role and accepted by Gate 2, so the
  allowlist is the only thing stopping them
- a companion test asserting Gate 2 *does* accept
  `SELECT * FROM pg_stat_activity LIMIT 3`, pinning that finding. If Gate 2 ever
  starts rejecting it, the allowlist's justification changed and someone should
  notice deliberately
- `Track` (wrong case) is refused while `track` succeeds — AC2
- non-strings, `None`, empty string: refused, not raised
- the rejection names the relation and lists valid ones

**Construction**
- the round-trip containment proof of §2, run against a corpus of hostile names
- generated SQL for all 12 relations passes `validate_sql()`
- composite-PK ordering emits both columns
- the view orders by all five columns

**Pipeline**
- `sample_rows` returns an `ExecutionResult`
- structural test: the module imports neither `get_engine` nor `text` (AC11)
- an empty relation returns success with columns and zero rows (AC18)

**Determinism**
- three consecutive calls return identical rows, for a PK relation and for the
  view

**Mutation checks**

| Mutation | Expected |
|---|---|
| Remove the allowlist check | Sampling `pg_stat_activity` goes red |
| Case-fold the comparison | The `Track` test goes red |
| Replace AST construction with an f-string | The containment round-trip goes red |
| Drop the `ORDER BY` | The determinism tests go red |
| Call the engine directly instead of `execute_sql` | The structural test goes red |

Precedent for insisting on these: mutation testing has now found three defences
on this project that a green suite hid — the tree walk's enumeration in `002`,
the branch scanner's call site in `002`, and the `SET LOCAL` masked by the role
default in `003`.

---

## 6. Risks

| Risk | Handling |
|---|---|
| The allowlist is the only gate for catalog relations | Its own test, plus the companion test pinning *why* it is load-bearing. Mutation-checked |
| `ORDER BY` every column could be slow on a large PK-less view | Bounded by Gate 3's `statement_timeout`, which returns a categorised `timeout` rather than hanging. Chinook's view is small; noted for Iteration 5 if a bigger one appears |
| Sampled content is untrusted input (§1 of the spec) | Out of scope here — the defence is Gate 2, which does not care how persuaded the model is. Iteration 5's prompt design must treat sampled values as data, and the spec says so |
| Two catalog round trips per call | Accepted, from `001`'s no-cache decision. Iteration 7 |

---

## 6b. Task log — ALL COMPLETE

| Task | Status | Outcome |
|---|---|---|
| **T1** | done | Pure AST construction. Mutation replacing it with string concatenation failed **15 tests**, including every containment case |
| **T2** | done | Allowlist, `unknown_relation`, capped listing, `get_schema()` failure path |
| **T3** | done | Wired through `execute_sql`; determinism confirmed across three runs for table, composite-key table, and view |
| **T4** | done | Registered. **The tool surface is complete at four.** |

**D-1 resolved: 25**, with the message stating the omitted count.

### Note on coverage

An audit found AC6 and AC8 without dedicated tests. AC6 (the allowlist runs
*before* any SQL is constructed) is an ordering property — "build then check" is
one edit from "build then execute" — and is now pinned by monkeypatching the
builder to raise. AC8 (unconditional quoting) was asserted incidentally inside
another test; it now has its own.

---

## 7. Decision — RESOLVED

**D-1 — What is `MAX_LISTED_RELATIONS` in the rejection message (your Q-D)?**
Chinook has 12, so any cap above that is invisible today. It matters for a
schema with hundreds of tables, where an unbounded list would dominate the
agent's context and push out the schema it actually needs.
*My lean: 25* — comfortably above Chinook, small enough that the message stays
readable, and the text says how many were omitted so the agent knows to call
`get_schema()` for the rest.

---

## 8. Proposed decomposition

| # | Task | Verified by |
|---|---|---|
| **T1** | `_build_sample_query()` and `_order_columns()`, pure, no database | AC7–AC9, AC14, AC15, plus the containment round trip |
| **T2** | The allowlist and `unknown_relation`, including the `get_schema()` failure path | AC1–AC6, AC17 |
| **T3** | Wire to `execute_sql`; determinism and empty-relation behaviour | AC10–AC13, AC16, AC18 |
| **T4** | Register in `TOOLS` | Registry complete at four |

T1 first and pure: query construction is the part with the security property, and
it is testable with no database at all — so it gets proven before anything can
execute it.
