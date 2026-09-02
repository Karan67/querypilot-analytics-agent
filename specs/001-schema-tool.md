# 001 — Schema Tool (`get_schema()`)

> **STATUS: IMPLEMENTED 2026-08-21.** All 17 acceptance criteria covered by
> 40 passing tests, verified on the host and inside the container.
> Implementation: `api/db/introspection.py`, registered in `api/agent/tools.py`.
> Plan and task log: [`001-schema-tool-plan.md`](001-schema-tool-plan.md).
>
> One known coverage gap, recorded rather than hidden: Chinook has no composite
> **foreign** key, so `ForeignKey.columns` is exercised by composite primary
> keys only. See the plan's task log.

Iteration: 1 (Tools before agent) · Inherits: [`000-project.md`](000-project.md)

---

## 1. Intent

`get_schema()` is the agent's map of the target database. Before it can write a
single correct query it has to know what relations exist, what columns they
hold, what types those columns are, and — most importantly — **how the tables
join**.

Foreign keys are the highest-value part of this output. A model that knows
`album.artist_id → artist.artist_id` writes the join correctly; a model left to
guess produces `album.artist_name`, which is the single most common failure mode
in Text-to-SQL. Most of the accuracy this project will claim traces back to how
good this grounding is.

This is the **first tool built and the only one with no injection surface**,
because it accepts nothing from the user or the model. It is a pure read of the
Postgres catalog over the read-only connection.

Consumers, in order of arrival: the Iteration 2 single-shot prompt, the
Iteration 4 agent loop (as a callable tool), the validator's
hallucinated-column checks, and the eval suite. **Three of those four need
structure, not prose** — which is why §5 returns an object and never a string.

---

## 2. Scope

Structural metadata for the `public` schema of the target database:

- base tables **and views** — both are `SELECT`-able, so both are legitimate
  query targets for the agent
- columns — name, type, nullability, ordinal position
- primary keys, including composite ones
- foreign keys, including composite and self-referencing ones

Nothing else. §4 says what is deliberately excluded.

---

## 3. Acceptance criteria

Testable statements, verified against the loaded Chinook dataset. Every figure
was read out of the running database on 2026-08-21, not recalled.

**Relations**

- **AC1** — Returns exactly 12 relations — the 11 Chinook base tables plus one
  view — sorted alphabetically regardless of kind: `album, artist, customer,
  employee, genre, invoice, invoice_line, invoice_totals, media_type, playlist,
  playlist_track, track`.
- **AC2** — Each relation carries its kind. Exactly 11 have `kind="table"`;
  `invoice_totals` has `kind="view"`. A consumer must be able to tell them apart
  without a second call.

**Columns**

- **AC3** — Columns come back in ordinal position order, not alphabetically.
  `track` returns exactly 9, beginning `track_id, name, album_id` and ending
  `bytes, unit_price`.
- **AC4** — Nullability is accurate **for base tables**: `track.name` is
  `nullable=False`, `track.album_id` is `nullable=True`.
  *Observed during T2:* Postgres does not propagate `NOT NULL` through a view —
  every column of `invoice_totals` reports nullable, including `invoice_id`,
  which derives from a `NOT NULL` primary key. So nullability is only
  meaningful on `kind="table"`. The tool reports what the catalog says; the
  Iteration 2 prompt renderer should not present view nullability as a
  constraint the model can rely on.
- **AC5** — Types preserve length and precision: `track.name` renders as
  `VARCHAR(200)` and `track.unit_price` as `NUMERIC(10, 2)` — not the bare
  `character varying` / `numeric` that `information_schema.data_type` returns.
  *Observed during T2:* computed view columns carry no declared precision —
  `invoice_totals.total` is bare `numeric` and `line_count` is `bigint`. That is
  correct and expected, not a defect; the tool must not synthesise a precision
  the catalog does not have.

**Keys and relationships**

- **AC6** — Primary keys are flagged on the column. `track.track_id` is
  `primary_key=True`.
- **AC7** — Composite primary keys flag **every** participating column:
  `playlist_track` marks both `playlist_id` and `track_id`. A tool that reports
  only the first column of a composite key is wrong.
- **AC8** — Returns 11 foreign keys across the schema. `album` has exactly one:
  `(artist_id) → artist(artist_id)`.
- **AC9** — `invoice_line` returns both of its foreign keys, not one.
- **AC10** — The self-referencing key on `employee` (`reports_to →
  employee.employee_id`) is returned. It must neither be dropped as a self-join
  nor cause unbounded recursion.

**Views specifically**

- **AC11** — A view with no primary key and no foreign keys is handled without
  raising. `invoice_totals` returns its columns, an empty `foreign_keys`, and no
  column with `primary_key=True`. Code that assumes every relation has a PK
  fails here.
- **AC12** — The `querypilot_ro` role can actually `SELECT` from every relation
  `get_schema()` reports. A relation the agent can see but cannot read is worse
  than one it cannot see at all — it produces a query that passes validation and
  then fails at execution. See the ordering constraint in the plan.

**Behaviour**

- **AC13** — Deterministic: two calls in one process return equal values.
  Ordering is stable across calls. Eval reproducibility depends on this.
- **AC14** — Takes **no parameters**. The schema name comes from configuration,
  never from a call argument, so no user or model input reaches an identifier.
  This is the property that makes the tool injection-free; a future signature
  like `get_schema(schema_name)` would forfeit it.
- **AC15** — System catalogs never appear. No `pg_catalog`, no
  `information_schema`, no relation owned by an extension.
- **AC16** — Executes over the read-only connection from `api/db/engine.py` and
  reads catalog metadata only. It selects no rows from any user relation.
- **AC17** — On an unreachable or failing database it raises
  `SchemaIntrospectionError`. It must **never** return an empty schema as a
  success — a silently empty map would make the model hallucinate an entire
  database, and the failure would surface as bad SQL rather than as an outage.

---

## 4. Non-goals

| Excluded | Why, and where it belongs instead |
|---|---|
| **Materialized views** | Explicitly out for Iteration 1. They are not in `information_schema.views` at all — Postgres exposes them via `pg_matviews` / `pg_class.relkind = 'm'`, so they need a separate code path rather than falling out of the view work for free. Chinook has none. **Observed in T1, recorded but not acted on:** matviews behave identically to tables and views under both `GRANT ... ON ALL TABLES` and `ALTER DEFAULT PRIVILEGES ... ON TABLES` — so the *privilege* side would need no work if they were ever added; only the *introspection* side would. Revisit only if a dataset needs it |
| Prompt-string formatting | **Resolved Q-A:** `get_schema()` returns a `Schema` object and never a string. `api/agent/prompts.py` owns rendering, from Iteration 2 |
| Caching or invalidation | **Resolved Q-C:** no cache. A catalog round-trip is negligible against LLM latency. Revisit at Iteration 7 with measurements, not before |
| Sample values from columns | That is `sample_rows()`, a separate tool in this same iteration |
| Row counts / table cardinality | Not needed for correct SQL. Revisit in Iteration 5 only if failed evals point at table selection |
| Column comments and descriptions | Iteration 5 enrichment. Chinook has none anyway |
| Schemas other than `public` | Single-schema by design; multi-schema is not a project goal |
| Indexes, unique constraints, check constraints, triggers, sequences | No demonstrated effect on SQL correctness. Adding them costs prompt tokens for no measured accuracy gain |
| Any DDL, or any write | Categorically out — [`000-project.md` §3](000-project.md) |

---

## 5. Contracts

Frozen dataclasses so the result cannot be mutated by a caller and so equality
(AC13) is free.

```python
# api/db/introspection.py — implementation and types
# api/agent/tools.py      — thin registered wrapper that delegates here

@dataclass(frozen=True)
class Column:
    name: str
    type: str          # SQLAlchemy rendering: "VARCHAR(160)", "INTEGER", "NUMERIC(10, 2)"
    nullable: bool
    primary_key: bool

@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[str, ...]           # local column(s) — tuple to carry composite keys
    referred_table: str
    referred_columns: tuple[str, ...]

@dataclass(frozen=True)
class Table:
    name: str
    kind: str                          # "table" | "view"  (AC2)
    columns: tuple[Column, ...]        # ordinal order (AC3)
    foreign_keys: tuple[ForeignKey, ...]   # always empty for views (AC11)

@dataclass(frozen=True)
class Schema:
    tables: tuple[Table, ...]          # alphabetical, kinds interleaved (AC1)


class SchemaIntrospectionError(RuntimeError):
    """Raised when the catalog cannot be read. Never swallowed into an empty Schema."""


def get_schema() -> Schema:
    """Return the structural map of the target database. Takes no arguments (AC14)."""
```

**Resolved Q-E — type rendering.** SQLAlchemy's (`VARCHAR(160)`), not
`information_schema`'s (`character varying`, which drops the length). It loses
no information and matches the DDL conventions the model saw in training.

**Resolved Q-D — module split.** Implementation in `api/db/introspection.py`;
`api/agent/tools.py` holds only the thin registered wrapper. This is now a
project-wide convention for every tool — see [`000-project.md` §5](000-project.md).

---

## 6. Verification

- `tests/test_schema_tool.py`, run against the **live Chinook container** rather
  than a mocked `Inspector`. Mocking introspection would assert that my mock
  matches my code and prove nothing about Postgres.
- Accepted cost: Iteration 1 tests require a running database, which makes the
  suite slower and makes CI (Iteration 8) need a Postgres service. This is the
  right trade for a tool whose entire job is reading a real catalog.
- AC17 gets its own test using a deliberately broken DSN.

---

## 7. Out of scope for this spec

`sample_rows()`, `validate_sql()`, and `execute_sql()` are the rest of Iteration
1 and get their own specs. Per [`000-project.md` §4](000-project.md),
`execute_sql()` is not built until `validate_sql()` exists — the safety layer
lands before the thing that needs it, never the reverse.
