# 001 — Schema Tool: Technical Plan

> **STATUS: AWAITING YOUR APPROVAL.** No application code exists against this.
> Spec: [`001-schema-tool.md`](001-schema-tool.md).
> §2 and §7 are the parts most worth your scrutiny — §2 is a problem the spec
> did not anticipate, and §7 asks you for two decisions I should not make alone.

---

## 1. Approach

Use SQLAlchemy's `Inspector` (`sqlalchemy.inspect(engine)`) rather than
hand-written `pg_catalog` queries. It already provides `get_table_names()`,
`get_view_names()`, `get_columns()`, `get_pk_constraint()`, and
`get_foreign_keys()`, and it renders types the way AC5 wants for free.
Hand-rolling catalog SQL would be more code, more to get wrong, and would gain
nothing the spec asks for.

The work is mostly **mapping** — Inspector's loose dicts into the frozen
dataclasses of the contract — plus getting three edge cases right (views,
composite keys, self-references).

**One correction to your Q-B phrasing.** You specified
`table_type IN ('BASE TABLE','VIEW')`, which is `information_schema` vocabulary.
On the Inspector path there is no `table_type` column to filter — views come
from a *separate call*, `get_view_names()`, because `get_table_names()` returns
base tables only. Same outcome, different mechanism. If you would rather see the
literal `table_type IN (...)` predicate in the code, that means dropping
Inspector for raw catalog SQL, which I would not recommend — say so and I will
re-plan.

---

## 2. A problem the spec did not anticipate: init ordering

Adding a view to the seed collides with how the read-only grant works.

`GRANT SELECT ON ALL TABLES IN SCHEMA public` applies **only to relations that
exist at the moment it runs**. It is not a standing rule. Today `db/init/` runs:

```
01_load_chinook.sh     → creates 11 tables
02_readonly_role.sh    → GRANT SELECT ON ALL TABLES
```

If the view were added in an `03_*.sql`, it would be created *after* the grant
and `querypilot_ro` would have **no privilege on it**. The failure mode is
nasty: `get_schema()` would happily report `invoice_totals` to the model, the
model would write a perfectly good query against it, the query would pass
validation, and only then would it die at execution with *permission denied*.
An agent cannot self-correct its way out of that — the SQL is not wrong.

This is what **AC12** exists to catch, and it is why the spec added that
criterion.

**Fix — renumber so the view is created before the grant:**

```
01_load_chinook.sh     (unchanged)
02_create_views.sql    (NEW)      → view exists before any grant
03_readonly_role.sh    (renamed from 02_readonly_role.sh)
```

This relies on `ALL TABLES` covering views. Postgres documents `ALL TABLES` as
including views and foreign tables, so I expect it to hold — but **the plan
should not rest on my expectation.** First implementation task verifies it
directly, and if it turns out not to hold, the fallback is an explicit
`GRANT SELECT ON invoice_totals` in `03`.

**Considered and rejected:** `ALTER DEFAULT PRIVILEGES` to make grants apply to
future relations automatically. It would remove the ordering constraint, but it
grants on things that do not exist yet — which is precisely the property you do
not want on a security boundary. Explicit ordering is auditable; a standing
future-grant is not. Ordering also makes the dependency visible in `ls db/init/`.

**Cost to you:** changing `db/init/` means `docker compose down -v` again, since
Postgres skips init scripts on a non-empty volume. Same lesson as Iteration 0.

---

## 3. The view

Proposed, in `db/init/02_create_views.sql`:

```
invoice_totals  =  invoice ⋈ invoice_line, aggregated to one row per invoice,
                   exposing (invoice_id, customer_id, invoice_date, line_count, total)
```

Chosen deliberately over a trivial `SELECT * FROM artist`-style view, because it
exercises the properties that actually distinguish a view from a table:

- **no primary key** and **no foreign keys** in the catalog → drives AC11, the
  case where code that assumes every relation has a PK crashes
- **computed columns** (`sum(...)`, `count(...)`) → the type of `total` is
  derived, not declared, which is a realistic thing for the type renderer to meet
- it is genuinely useful to the agent later — "average invoice value by country"
  becomes a much shorter query

Note `invoice` already has a `total` column of its own. That is a feature, not a
collision: it gives Iteration 5 a real disambiguation case to study.

---

## 4. Files

| File | Status | Contents |
|---|---|---|
| `api/db/introspection.py` | **new** | `Column`, `ForeignKey`, `Table`, `Schema`, `SchemaIntrospectionError`, `get_schema()`. All real logic lives here |
| `api/agent/tools.py` | exists, empty | Thin registered wrapper delegating to the above. Per [`000-project.md` §5](000-project.md) this file is the registry |
| `db/init/02_create_views.sql` | **new** | The view, positioned before the grant |
| `db/init/03_readonly_role.sh` | **renamed** from `02_readonly_role.sh` | Unchanged content |
| `tests/conftest.py` | **new** | Engine fixture; skip-with-reason when no database is reachable |
| `tests/test_schema_tool.py` | **new** | One test per acceptance criterion |
| `api/requirements-dev.txt` | **new** | `pytest`. Kept out of the runtime image |
| `README.md` | edit | Update the `db/init/` table for the renumbering |

Not touched: `api/main.py`, `api/db/engine.py`, `api/safety/`, `docker-compose.yml`
(unless you pick the container-test option in §7).

---

## 5. Data structure mapping

Inspector output → contract, with the traps:

| Contract field | Source | Trap |
|---|---|---|
| `Table.name` | `get_table_names()` + `get_view_names()` | Two calls, then merge and sort together — AC1 wants one alphabetical list with kinds interleaved, not tables-then-views |
| `Table.kind` | which call it came from | Assigned at merge time; there is no field on the dict to read it from |
| `Column.type` | `str(col["type"])` | SQLAlchemy renders `VARCHAR(200)`, `NUMERIC(10, 2)` — satisfies AC5 directly |
| `Column.primary_key` | `get_pk_constraint()["constrained_columns"]` | Must be membership-tested against the **whole list**, not `[0]`. Testing only the first column is exactly the AC7 bug |
| `Column.nullable` | `col["nullable"]` | Direct |
| `Table.foreign_keys` | `get_foreign_keys()` | Returns `constrained_columns` / `referred_columns` as lists; convert to tuples for hashability and AC13 equality |
| views | both PK and FK calls | Return empty/`None` shapes rather than raising. `get_pk_constraint()` on a view yields `constrained_columns: []` — code must not index into it |

Self-referencing FKs (AC10) need no special handling on this path — they are
ordinary rows where `referred_table == table.name`. The criterion exists to stop
someone "helpfully" filtering them out later, not because the mapping is hard.

**Ordering:** sort relations by name; keep columns in Inspector's order, which is
already ordinal. Sorting is explicit, not incidental — AC13 and eval
reproducibility both depend on it.

---

## 6. Error handling

Wrap the introspection body and re-raise as `SchemaIntrospectionError`:

- `SQLAlchemyError` → connection refused, auth failure, timeout
- `RuntimeError` from `get_engine()` → missing `QUERYPILOT_DATABASE_URL`

The rule from AC17 that constrains the implementation: **an empty result is
never a success.** If zero relations come back, raise rather than return an empty
`Schema` — a database with genuinely no tables is indistinguishable from a
broken introspection, and the failure mode of guessing wrong is the model
inventing an entire schema.

Chaining preserved (`raise ... from exc`) so the original Postgres error stays
in the traceback. From Iteration 4 the agent sees the message text, so it has to
stay specific — "could not connect to server" is actionable, "schema error" is
not.

---

## 7. Decisions — RESOLVED 2026-08-21

**D-1 — RESOLVED: host.** With one binding constraint: the test DSN is read from
**`TEST_DATABASE_URL`**, defaulting to localhost. Nothing hardcoded in test
setup. **Any test embedding `localhost:5432` directly is to be rejected in
review.** Iteration 8 CI then only has to set the environment variable.

**D-2 — RESOLVED: minimal.** `TOOLS` dict plus the wrapper, nothing more.
Rationale to preserve: tool schemas are *provider-shaped* — Groq, Anthropic and
Gemini each describe tools differently — so serialisation belongs behind the LLM
interface, not in `tools.py`. It cannot be designed before `007-agent-loop.md`
picks the provider contract. Putting it in `tools.py` now would bake one
provider's shape into the shared tool surface.

<details>
<summary>Original framing of both decisions, kept for the record</summary>

**D-1 — Where do tests run?**

| | Host pytest | Container pytest |
|---|---|---|
| How | `pip install -r requirements-dev.txt` on Windows, connect to `localhost:5432` | Dockerfile `dev` stage + a profiled `test` service, connect to `db:5432` |
| Pro | Fast loop, no rebuild, no compose changes | Identical env to CI; no host Python setup; matches Iteration 8 |
| Con | Needs host Python + `psycopg` toolchain; DSN differs from prod; classic "works on my machine" | ~15 lines of compose/Dockerfile; slower iteration |

*My lean: host for now.* Iteration 1 is a tight write-test loop, your host Python
already works, and Iteration 8 has to build a CI Postgres service regardless — so
containerising tests now buys parity you cannot use for seven more iterations.
Worth overriding if you would rather never run a "works on my machine" suite.

**D-2 — How much registry in `tools.py` now?**

Full tool-registry machinery (JSON schemas for the model, name→callable dispatch,
argument validation) is Iteration 4's problem — it cannot be designed properly
until `007-agent-loop.md` decides how the model expresses a tool call.

*My lean: minimal now.* A `TOOLS: dict[str, Callable]` plus the `get_schema`
wrapper. Enough to establish the convention from [`000-project.md` §5](000-project.md)
and to make the file readable as the tool surface, without inventing a calling
convention we would rewrite in Iteration 4. Tell me if you want the full registry
designed up front instead.

</details>

---

## 7b. Task log

| Task | Status | Outcome |
|---|---|---|
| **T1** | done | `ALL TABLES` covers views **and** matviews; grant confirmed to be a snapshot (all post-grant relations denied); `ALTER DEFAULT PRIVILEGES ... ON TABLES` covers future views and matviews. No explicit per-view grant needed in T2 |
| **T2** | done | `02_create_views.sql` added, role script renamed to `03_`, `ALTER DEFAULT PRIVILEGES` added, volume recreated. AC12 verified: `querypilot_ro` reads `invoice_totals`; role holds SELECT only (INSERT/UPDATE/DELETE/TRUNCATE all false) |
| **T9** | done early | README init table referenced a renamed file and a stale `/health` count; fixed rather than left wrong until the end |
| **T3** | done | `api/db/introspection.py` types: `Column`, `ForeignKey`, `Table`, `Schema`, `SchemaIntrospectionError`, `KIND_TABLE`/`KIND_VIEW`. Verified frozen, structurally equal, hashable, no DB contacted |
| **T4** | done | `get_schema()` over base tables. 12 tests pass. Harness added: `tests/conftest.py`, `tests/test_schema_tool.py`, `api/requirements-dev.txt`, `pytest.ini`, `.venv` |
| T5 | **scope changed** | **Now test-only.** `_build_relation()` written in T4 already maps composite PKs and foreign keys — splitting it across two tasks would have meant writing the function twice. Confirmed working against the live catalog: `playlist_track` flags both PK columns, `employee.reports_to → employee.employee_id` is mapped, 11 FKs total. T5 is writing the AC7–AC10 assertions, not implementing them |
| **T5** | done | AC7–AC10 assertions added, including the full 11-key expected map. **Mutation-verified:** flagging only the first column of a composite key fails `test_ac7` and nothing else — no other test in the suite catches that bug |
| **T6** | done | Views merged via `get_view_names()`, `kind` assigned at merge time, sorted after merge so kinds interleave. 27 tests pass. **Mutation-verified:** grouping tables-then-views fails AC1 and AC13 |
| **T7** | done | AC17 across all three failure modes: unreachable host, missing DSN, and empty catalog. Cause chaining asserted — the underlying Postgres text survives into the message the agent will read |
| **T8** | done | `api/agent/tools.py`: `get_schema` wrapper + `TOOLS` registry, nothing else. A structural test asserts the module imports no SQLAlchemy, so the §5 split cannot erode silently |

**All 17 acceptance criteria are covered by the suite — 40 tests passing**, on
the host and inside the container.

**Second unplanned file:** `tests/test_tools.py`. Keeping registry tests out of
`test_schema_tool.py` means the next three tools extend one obvious file rather
than growing the schema tool's.

**AC12 was upgraded during T8.** It had been asserted only by
`db/init/03_readonly_role.sh`, which runs on a fresh volume and never again. It
now also has a test that runs every time, using `has_table_privilege` with the
relation name bound as a parameter — testing the privilege directly, and staying
clear of the §4 rule that no code path executes generated SQL without the
validator, tests included.

**Known coverage gap (T5):** all 11 Chinook foreign keys are single-column. The
tuple-valued `ForeignKey.columns` handles composite foreign keys by design, but
no fixture exercises that path — composite *primary* keys are covered by
`playlist_track`, composite *foreign* keys are not covered at all. Recorded
rather than papered over; adding a synthetic composite FK to the seed would be a
spec change, not an implementation detail.

**Unplanned file:** `pytest.ini` was not in §4. Needed so tests import the app as
`api.*` the way the container does — without it pytest puts `tests/` on
`sys.path` instead of the repo root and every import fails.

---

## 8. Proposed decomposition

One task, one testable behaviour, in dependency order. I stop after each for your
review.

| # | Task | Verified by |
|---|---|---|
| **T1** | Confirm `GRANT SELECT ON ALL TABLES` covers views (§2 risk). Throwaway database, dropped after | A `querypilot_ro` `SELECT` against a view created before the grant either works or does not. Decides whether T2 needs an explicit grant |
| **T2** | Add `02_create_views.sql`, rename role script to `03_`, recreate volume | Init log shows the view created; `querypilot_ro` can `SELECT` from `invoice_totals` — **AC12** |
| **T3** | `api/db/introspection.py`: dataclasses + `SchemaIntrospectionError` | Imports clean; no DB needed |
| **T4** | `get_schema()` — tables only, no views yet | AC3, AC4, AC5, AC6, AC13, AC14, AC15, AC16 |
| **T5** | Composite PK + FK mapping | AC7, AC8, AC9, AC10 |
| **T6** | Merge views in, assign `kind` | AC1, AC2, AC11 |
| **T7** | Error path | AC17, via a deliberately broken DSN |
| **T8** | `tools.py` wrapper + minimal registry | Wrapper returns the same object the implementation does |
| **T9** | README update for renumbered init scripts | Documentation matches `ls db/init/` |

T1 comes first because its outcome changes T2. T4 before T6 so that a failure in
view handling cannot be mistaken for a failure in the base mapping.

---

## 9. Risks

| Risk | Handling |
|---|---|
| `ALL TABLES` might not cover views | T1 checks it before anything depends on it |
| A relation is visible to `get_schema()` but unreadable by the role | AC12 is a standing test, not a one-off check — it will catch this for future relations too |
| Volume recreation needed for every `db/init/` change | Known from Iteration 0; documented in the README troubleshooting section |
| Test suite now requires a live database | Accepted in spec §6. `conftest.py` skips with a clear reason rather than erroring cryptically |
| `str(col["type"])` rendering could differ across SQLAlchemy versions | Pinned to `>=2.0,<2.1`; AC5 asserts exact strings, so a version bump that changes rendering fails loudly rather than silently degrading the prompt |
