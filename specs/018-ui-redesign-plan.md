# 018 — Iteration 15 plan: UI Modernization & Workbench Polish

## 1. Approach

Three independent slices, built in dependency order where one exists:

- **Telemetry** extends an existing contract (`/ask`'s payload) by reading a
  new best-effort attribute off the concrete provider classes — same shape as
  the existing `.model` property, so there is no real design question there.
  The one real question is how a value survives a cache hit, where today's
  code deliberately never builds a provider at all (§2, D-1 below).
- **Schema viewer** adds one small new route that wraps an existing,
  already-gated function and a small dict-builder next to the existing
  `encode_rows` (§5's "one encoder" contract). It inherits auth for free
  because the gate is deny-by-default (`api/http/auth.py`'s `OPEN_PATHS` is
  an allow-list, not a deny-list).
- **Visual overhaul** is CSS/markup only, additive to the existing token
  system, and depends on nothing above — it can be built and checked by eye
  independently of the other two.

## 2. Telemetry: provider identity through a cache hit (D-1)

`api/main.py::_answer_or_replay` builds a provider **only on a miss** — on a
hit `provider` is `None`, deliberately, so a cached answer survives an
outage or a missing key and `provider_ms` is honestly zero
(`api/main.py:946-949`). The approved spec's AC10 requires a hit to still
report the provider/model that produced the *original* answer, not an empty
value — so that information has to be carried by the cache entry itself,
since it cannot be reconstructed by building a provider on a hit without
undoing the exact property that makes a hit cheap and outage-proof.

`api/http/cache.py::get_or_compute` stores whatever `compute()` returns —
it's generic over `Any` and already keyed/locked correctly; nothing about
the cache module itself needs to change.

**D-1 — How does provider/model ride along with a cached entry?**

*My lean:* change what the `compute()` closure in `_answer_or_replay`
returns from the bare `AgentResult` to a small `(AgentResult, provider_name,
model)` tuple (or a tiny frozen dataclass, matching the existing `_Answered`
style at `api/main.py:894-908`), captured once at compute time from whatever
provider was actually built. Both the hit and miss paths in
`_answer_or_replay` then unwrap the same shape, so the payload-building code
in `ask()` never has to know whether it's looking at a fresh or a coalesced
or a replayed value. This is a few lines inside `api/main.py`, touches
`cache.py` not at all, and keeps `cache.py`'s existing "the value is opaque"
design intact rather than teaching the cache module about providers.

Provider *name* itself: add a `NAME` class attribute (e.g. `NAME = "groq"`,
`NAME = "cerebras"`) to `GroqProvider`/`CerebrasProvider`, read the same
best-effort `getattr` way `.model` already is. No factory change — `factory.
get_provider()`'s return type and every existing caller (eval runner,
orchestrator, tests) stay untouched, which the alternative (having the
factory return `(provider, name)`) would not achieve without touching every
call site.

## 3. Schema viewer: `GET /schema`

`get_schema()` (`api/db/introspection.py:249`) returns a `Schema` of
`Table`s of `Column`s, already reached through Gate 2. The route:

- calls `get_schema()`, nothing else — no direct `execute_sql()` or engine
  use, so the existing "reaches the database only through X" structural-test
  pattern extends cleanly;
- catches `SchemaIntrospectionError` and returns **503**, mirroring how
  `/health` treats an unreachable database rather than inventing a new
  failure shape (AC7);
- serializes explicitly, not via `dataclasses.asdict(schema)` — the approved
  spec's AC4 scopes the response to `name`, `kind`, and each column's `name`
  and `type`. `Schema` already carries `foreign_keys` too, and dumping the
  whole dataclass would silently widen the contract the spec deliberately
  narrowed (§7 Q-C: no ERD this iteration). The builder function lives next
  to `encode_rows` in `api/http/serialization.py`, per the "JSON encoder is
  one function in one place" contract in §5 of the spec.

## 4. Files and decomposition

| # | Task | Files | Depends on | Mutation check |
|---|---|---|---|---|
| T1 | `NAME` attribute on `GroqProvider`/`CerebrasProvider`; wire `provider`/`model` into `/ask`'s **miss** path | `api/llm/groq_provider.py`, `api/llm/cerebras_provider.py`, `api/main.py` | — | Remove the `getattr` read, confirm the payload test asserting a specific non-empty value (not just key presence) goes red |
| T2 | D-1: carry provider name + model through `_answer_or_replay`'s cache path so a **hit** reports the original answer's provider/model | `api/main.py` | T1 | Force a hit to report an empty/placeholder value, confirm the AC10 test (asserts the *original* value survives a hit) goes red |
| T3 | `schema_to_dict`-style builder next to `encode_rows` | `api/http/serialization.py` | — | Drop a field (e.g. a column's `type`), confirm a test asserting the full shape goes red |
| T4 | `GET /schema` route: calls `get_schema()`, catches `SchemaIntrospectionError` → 503, uses T3's builder | `api/main.py` | T3 | Swap `get_schema()` for a call that bypasses Gate 2 (e.g. a raw `execute_sql` stand-in), confirm the "reaches the database only through `get_schema()`" structural test goes red |
| T5 | Structural/completeness test for `/schema` — mirrors the existing pattern that walks `api/` for unmapped failure categories and the `OPEN_PATHS` auth walk; confirm the new route is swept in automatically rather than needing a manual add | `tests/test_schema_endpoint.py` (new) | T4 | Add the route to an exemption list by hand, confirm the completeness test catches an *unswept* route rather than passing vacuously |
| T6 | Visual overhaul: extend `:root` tokens with a type scale; two-region responsive workbench layout, one breakpoint | `api/web/styles.css`, `api/web/index.html` | — | None (CSS/layout; verified by eye per §6 of the spec, same as `009-frontend.md`'s own precedent) |
| T7 | Schema viewer rendering: fetch `/schema`, render a collapsible table→columns list; legible message on failure (AC7) | `api/web/app.js`, `api/web/index.html`, `api/web/styles.css` | T4, T6 | `node --check app.js` (existing guard) plus a rendering test that calls the render function and asserts the *output*, not that the function is merely defined — the exact trap `HANDOFF.md` §6 records twice already |
| T8 | Telemetry pills: render `provider`/`model`/`total_ms`/`provider_ms` near the SQL panel, for both a fresh and a cached answer | `api/web/app.js`, `api/web/styles.css` | T2, T6 | Same shape as T7's: assert the call and its rendered effect, including the cache-hit case, not just that the fields are read somewhere in the file |

## 5. Confirmed non-effects (checked, not assumed)

- **`/history` and `/history/data` are unaffected.** `_record_history`
  (`api/main.py:999`) copies named fields off `payload` one at a time rather
  than persisting the whole dict, so `provider`/`model` do not flow into the
  history store or its page just because they exist on `/ask`'s response.
  This is confirmed by reading the function, not assumed — extending history
  to show them is a natural follow-up but is out of the spec's named scope
  and is not silently added here.
- **`errors.py`'s category-completeness test is not touched.** `/schema`'s
  only failure mode is `SchemaIntrospectionError` → a plain 503, handled
  locally in the route rather than routed through `failure_for`'s
  category table — it never introduces a new `AgentResult` category, so
  the existing exhaustiveness walk over agent/db categories has nothing new
  to account for. T5 verifies this against the real test rather than
  assuming it.

## 6. Decisions

One surfaced while writing this plan, beyond D-1 above:

- **D-2 — Does `provider`/`model` need its own structural "doesn't widen the
  `LLMProvider` protocol" test, or does the existing best-effort-attribute
  pattern already cover it?** `last_usage`/`last_rate_limit` are read via
  bare `getattr` at existing call sites with no dedicated test asserting the
  Protocol stayed one method wide beyond the one structural test that already
  parses `api/llm/base.py`'s AST.

  *My lean: no new test* — `NAME` is exactly the same shape of addition as
  `.model`, and the existing Protocol-width test already covers the class
  regardless of how many best-effort attributes sit beside it. Flagging this
  rather than silently deciding it because it's a plan-level judgment call
  the spec didn't anticipate.

## 7. Risks

- **T2 is the only task with real design risk.** Getting the cache-carried
  tuple wrong (e.g. unwrapping it inconsistently between the hit and miss
  branch) would silently misreport which provider answered a cached
  question — exactly the shape of defect the mutation check for T2 exists to
  catch, so that check is not optional.
- **CSS-only T6 has no automated verification**, per `009-frontend.md`'s own
  standing precedent that there is no honest automated assertion of
  "demoable to a non-technical person." Checked by hand at two widths before
  this task is called done.

---

## Task table (compact, for the implementation go-ahead)

| Task | What | Depends on |
|---|---|---|
| T1 | Provider `NAME` attribute + wire into `/ask` miss path | — |
| T2 | Carry provider/model through a cache hit (D-1) | T1 |
| T3 | Schema JSON builder in `serialization.py` | — |
| T4 | `GET /schema` route | T3 |
| T5 | `/schema` structural/completeness tests | T4 |
| T6 | CSS token/type-scale + responsive workbench layout | — |
| T7 | Schema viewer rendering in `app.js` | T4, T6 |
| T8 | Telemetry pills in `app.js` (incl. cache-hit case) | T2, T6 |

Pausing here for the go-ahead to implement, per the batched-rulings
precedent (spec §7 Q-E) — the next pause point, not before.
