# 018 — Iteration 15: UI Modernization & Workbench Polish

Status: **approved 2026-09-16**, §7 resolved · Created: 2026-09-16

> **Resolved questions.** Q-A hand-written CSS, extend the existing token
> system, no Tailwind/CDN · Q-B yes, a real `GET /schema` endpoint this
> iteration · Q-C flat collapsible table/column list, no ERD · Q-D `provider`
> and `model` travel in the existing `/ask` payload · Q-E full spec → plan →
> mutation-tested-implementation treatment, rulings batched to skip the
> spec-to-plan pause. Each is recorded in §7 beside the question it answers.

Inherits every constraint in [`000-project.md`](000-project.md). Where this
document and the charter disagree, the charter wins.

---

## 1. Intent

The product has answered questions correctly and safely for eight iterations,
but the surface a non-technical person actually sees — `api/web/` — has been
untouched since Iteration 6 ([`009-frontend.md`](009-frontend.md),
2026-09-09). Two things have since made that surface stale relative to what
the backend can now tell it:

- **Iteration 13** ([`016-second-llm-provider.md`](016-second-llm-provider.md))
  added Cerebras as a second `LLMProvider` alongside Groq's default, switched
  by `QUERYPILOT_LLM_PROVIDER`. Nothing in the UI has ever shown which one
  answered a question, even though both providers already expose a `.model`
  property for exactly this purpose.
- **Iteration 14** ([`017-schema-generality.md`](017-schema-generality.md))
  proved the product generalizes to a real second schema. There has never
  been a way for a user to *look at* the schema they are querying, even
  though `get_schema()` has existed since Iteration 1 and already goes
  through Gate 2 like every other read.

This iteration is polish ahead of the still-deferred B-11 live deployment
(charter §8) — it does not touch the agent, the prompt, the dataset, or
accuracy in any way. It converts backend state that already exists
(`get_schema()`, `.model`, `total_ms`/`provider_ms`) into something a viewer
can see, and gives the page laid out at Iteration 6 a typography and layout
pass five iterations later.

---

## 2. What the measurements say

Taken 2026-09-16 by reading the running code, not estimated.

### 2.1 The current frontend, sized

`api/web/index.html` is 170 lines, `api/web/styles.css` is 291 lines and
already has a `:root` custom-property token system plus a
`prefers-color-scheme: dark` override, `api/web/app.js` is 518 lines. No
templating engine, no build step — Q-A of `009-frontend.md` still binds, and
this iteration does not reopen it.

### 2.2 The HTTP surface has no schema route

`api/main.py` exposes exactly seven routes: `/health`, `POST /ask`,
`/quota`, `POST /feedback`, `/history/data`, `/history`, `/`. There is no
`GET /schema`. `get_schema() -> Schema` (`api/db/introspection.py:249`)
takes no arguments, returns tables with their columns and foreign keys, and
already goes through the same catalog-probe path Gate 2 covers — B-10 closed
the question of whether introspection could bypass the gate, and the answer
was no. Wrapping it in an HTTP route is additive, not a new gate to design:
every route but `/health` already requires HTTP Basic (Iteration 10,
`api/http/auth.py`'s `OPEN_PATHS`), and a new route inherits that for free.

### 2.3 `/ask`'s payload carries latency but not identity

`api/main.py:549-609` builds the `/ask` response with `usage`, `total_ms`,
`provider_ms`, `cache_hit` and `id` — but no field says which provider or
model produced the answer, despite two providers having existed since
Iteration 13. `GroqProvider` and `CerebrasProvider` each expose `.model` as a
**best-effort attribute** — not part of the `LLMProvider` protocol, read via
`getattr`, exactly the pattern the charter requires for anything beyond
`complete()` (`api/llm/groq_provider.py:64-70`). Neither exposes a provider
*name*; `api/llm/factory.py::get_provider()` is currently the only code that
knows which branch was taken, and it does not return that information to its
caller.

### 2.4 Shape and chart logic is already server-side and must stay that way

`api/http/shapes.py` classifies every result as `scalar`/`list`/`table`/
`chartable`/`empty` and computes `chart_series` for the chartable case. This
is unchanged by this iteration and the redesign renders by these values —
never re-derives them client-side, per the existing rule this same module's
docstring exists to enforce.

---

## 3. Acceptance criteria

### Visual overhaul

- **AC1** — `styles.css`'s existing `:root` token system (colors, spacing)
  is extended with a real type scale, not replaced by an external framework
  (§7 Q-A).
- **AC2** — The page lays out as a two-region workbench on a wide viewport —
  question/examples/schema on one side, answer/SQL/trace/telemetry on the
  other — and collapses to one column under a defined breakpoint. Verified
  by hand at two widths (§6).
- **AC3** — The existing dark-mode override, AC8's always-visible SQL panel,
  and AC13's ban on any accuracy claim (`009-frontend.md`) all survive
  unchanged.

### Schema viewer

- **AC4** — **`GET /schema`** returns the structural map from `get_schema()`
  as JSON: table name, kind, and each column's name and type, in the same
  ordering `Schema` already guarantees (alphabetical by table, ordinal by
  column). It requires the same HTTP Basic credential as every route but
  `/health`.
- **AC5** — **Nothing bypasses the safety layer.** The route calls
  `get_schema()` and nothing else; it does not open a second path to the
  database. A structural test asserts this, per the charter's standing rule.
- **AC6** — The page renders the response as a **flat collapsible list** —
  tables collapsed by default, each expanding to its columns and types. No
  entity-relationship diagram, no foreign-key graph (§7 Q-C).
- **AC7** — A schema-introspection failure (the same `SchemaIntrospectionError`
  path `get_schema()` already defines) renders as a legible message, not a
  blank panel or a raw stack trace.

### Telemetry status pills

- **AC8** — The **`/ask`** response gains two fields, `provider` and
  `model`, populated the same best-effort way `.model` already is —
  `getattr(provider, ...)`, defaulting to an empty/unknown value rather than
  raising, and **never widening the `LLMProvider` protocol** (§7 Q-D).
- **AC9** — The page renders provider, model and latency (`total_ms`,
  already present) as small status pills next to the SQL panel, per answer —
  not a global "current configuration" banner, since the same request
  payload already carries per-answer `usage` and `total_ms` on this
  precedent.
- **AC10** — A cache hit (`cache_hit: true`) still reports a provider/model —
  the ones that produced the *original* answer, not empty strings — since
  `usage` already distinguishes a hit's zero cost from a miss's real one and
  the pills should not contradict that by looking unset.

---

## 4. Non-goals

- **A new charting library.** The hand-rolled SVG bar chart from Iteration 7
  (`009-frontend.md` D-3) stays exactly as it is.
- **An entity-relationship diagram.** `get_schema()` already carries foreign
  keys, so a graph is possible later, but it is a materially larger design
  surface than a "polish" iteration, and neither Chinook's nor Pagila's live
  table count has been measured for how a graph would even lay out on
  screen. Decided in §7 Q-C, not merely deferred by omission.
- **Any change to the agent, the prompt, the dataset, or accuracy.** This
  iteration adds a surface over existing backend state; any accuracy
  movement would be an accident and would invalidate comparability with
  every prior `EVALS.md` entry, per `009-frontend.md`'s own precedent.
- **Any accuracy claim on the page.** AC13 from `009-frontend.md` — no
  percentage, no confidence score, anywhere — still binds.
- **Authentication changes.** `/schema` inherits the existing Basic Auth
  gate; nothing about who may authenticate changes.
- **Pagination, virtualisation, or streaming.** Still unjustified at the
  result sizes `009-frontend.md` §2.6 measured; nothing in this iteration
  changes that measurement.

---

## 5. Contracts

- **`answer()` is unchanged.** The `/ask` endpoint still adapts it; `provider`
  and `model` are read off the already-built `provider` object the endpoint
  holds, not a new call into the agent.
- **`complete(system, user) -> str` is unchanged.** Charter §5. Reading
  `.model` and a provider name off the concrete object is the same
  best-effort-attribute pattern already used for `last_usage` and
  `last_rate_limit` — it does not touch the protocol.
- **`get_schema()` is unchanged.** `/schema` wraps it; it does not grow a
  parameter or a second code path.
- **Every database read still goes through `execute_sql()`**, including the
  new route — `get_schema()` already satisfies this (§2.2).
- **The JSON encoder stays one function in one place.** No second encoder
  for the schema response.

---

## 6. Verification

- A structural/completeness test for `/schema`, following the existing
  pattern that walks every module under `api/` and every failure category —
  the same shape of test that currently guards `/ask`'s failure mapping in
  `api/http/errors.py`.
- A payload test for `AC8`/`AC10` asserting `provider` and `model` are
  present on both a live answer and a cache hit, with a value that
  discriminates (not an empty-string default that a bug could produce by
  accident).
- Mutation testing for the new backend logic — the `/schema` route and the
  `provider`/`model` attribute read — per the charter's blanket rule: remove
  the defense, confirm a test goes red.
- The visual overhaul is verified by a human opening the page, per
  `009-frontend.md`'s own precedent (*"There is no honest automated
  assertion of demoable to a non-technical person"*) — golden path plus the
  page's existing edge cases (a failed question, an empty result, a cached
  answer), at two viewport widths.

---

## 7. Open questions

**Resolved in the same message that approved this spec's plan** — the user
pre-answered all five to skip the spec-to-plan pause under quota pressure,
per the project's standing precedent for batched rulings. Each is recorded
below with the reasoning that was on the table when it was asked.

- **Q-A — Styling approach.** Hand-written CSS, extending the current token
  system, vs. adopting Tailwind CDN.

  > **Resolved: hand-written CSS.** *"Extend the existing `:root` token
  > system and dark-mode variables in `styles.css`. Do not add Tailwind or
  > external CSS CDNs."* The file already has a working token/dark-mode
  > system; introducing Tailwind alongside it would mean two styling systems
  > coexisting on one page.

- **Q-B — Does the schema viewer get a real backend endpoint this
  iteration?**

  > **Resolved: yes.** *"Implement `GET /schema` wrapping `get_schema()`
  > under existing Basic Auth."* It is the smallest possible addition — it
  > wraps an already-gated, already-tested function and inherits auth for
  > free — and schema visibility was explicitly named in scope.

- **Q-C — Schema viewer shape.** A flat collapsible list vs. an
  entity-relationship diagram with foreign-key lines.

  > **Resolved: flat collapsible sidebar list** — tables → columns + data
  > types. *"No ERD/graph canvas."*

- **Q-D — Where does telemetry live in the contract?** Added to the
  existing `/ask` response body vs. a separate endpoint the page polls.

  > **Resolved: the `/ask` payload.** *"Return `provider` and `model`
  > directly in the `/ask` response payload via best-effort `getattr`."*
  > Consistent with how `usage`/`total_ms`/`provider_ms` already travel with
  > each answer.

- **Q-E — Workflow for this iteration.** Standard spec → user answers →
  plan → user answers → implementation, vs. a batched-ruling shortcut.

  > **Resolved: batched, quota-conservation mode.** *"Pre-approve these
  > decisions. Draft `specs/018-ui-redesign.md` and immediately proceed to
  > `specs/018-ui-redesign-plan.md`."* The pause moves to just before
  > implementation, where a compact `T1`–`TN` task table is presented
  > instead of streamed drafts.

---

## 8. What this unblocks

The schema viewer and telemetry pills are the last pieces of "demoable to a
non-technical person" (charter §6) that Iteration 6 left on the table —
someone watching the product answer a question can now also see what it was
allowed to know and which model answered. Nothing here moves the product
closer to B-11 architecturally; it makes the existing demo path legible
enough to actually demo before that deployment decision is made.
