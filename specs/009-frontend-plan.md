# 009 — Iteration 6 plan: Frontend

Status: **approved 2026-09-09**, §7 resolved · Created: 2026-09-09

> **Resolved decisions.** D-1 `Decimal` crosses as a **JSON string**, parsed to
> float on the client only for SVG bar widths · D-2 **`200` with `ok: false`**
> for a failed question, `503` reserved for infrastructure · D-3 **hand-rolled
> SVG**, no CDN · D-4 **delete `frontend/`** and correct the README, including
> the 40 → 50 corpus count. Reasoning is recorded in §7.

Implements [`009-frontend.md`](009-frontend.md), whose §7 is resolved. This
document says *how*, and surfaces the decisions the design itself raised.

---

## 1. Approach

**Two pieces, and the UI is the smaller one** (spec §2.1). The API is still at
Iteration 0 scope, so this iteration first exposes `answer()` over HTTP and
only then puts a page in front of it.

The order is deliberate. A working `POST /ask` is independently verifiable with
`curl` and fully testable; a page is not. Building the page first would mean
debugging two unproven layers at once, and the page cannot be meaningfully
tested anyway — spec §6 says so plainly rather than pretending otherwise.

Three properties constrain everything below:

1. **The agent does not learn about HTTP.** `answer()` is unchanged; the
   endpoint adapts it. Same reasoning that keeps the LLM provider one method
   wide.
2. **No new dependency, runtime or dev.** Q-A's *"dependency-free"* is
   literally achievable: the page is static, every render is client-side after
   a `fetch`, and `HTMLResponse` plus Starlette's `StaticFiles` already ship
   with FastAPI.
3. **The safety layer is untouched.** Adding a transport does not relax charter
   §4, and a structural test says so.

---

## 2. The endpoint

### 2.1 Contract

```
POST /ask     {"question": "How many tracks are in the library?"}

200  {
       "ok": true,
       "question": "...",
       "sql": "SELECT count(*) FROM track",
       "columns": ["count"],
       "rows": [["3503"]],
       "shape": "scalar",
       "attempts_used": 1,
       "trace": [{"action": "execute_sql", "ok": true}, ...],
       "category": "",
       "error": ""
     }
```

`shape` is computed server-side from `columns` and `rows` — `scalar`,
`list`, `table`, or `chartable` — because the *same* classifier that produced
spec §2.3's measurements should decide what the UI renders. Two
implementations of "is this a scalar" that could disagree is exactly the drift
`HANDOFF.md` §6 warns about.

**`shape: "chartable"` does not mean "draw a chart."** Q-B settled that the
human decides; `chartable` only means the toggle is offered. `easy-010`'s
playlist ids will be classified `chartable` and that is correct behaviour —
the toggle exists precisely because the classifier cannot tell an id from a
measure.

### 2.2 Failure categories

Eleven exist today, across three modules:

| module | categories |
|---|---|
| `db/execution.py` | `rejected`, `timeout`, `database_error`, `connection_error`, `gate_violation` |
| `agent/orchestrator.py` | `budget_exhausted`, `repeated_sql`, `unknown_action` |
| `agent/single_shot.py` | `provider_error`, `rate_limited`, `no_sql_returned` |

Every one is mapped explicitly, and **a completeness test asserts that no
category is unmapped** — the same discipline `RETRY_POLICY` already carries,
and for the same reason: the first category that test ever missed was one
added in the file it was testing. The test must enumerate all three modules;
a completeness check that exempts a module is not a completeness check.

Mapping (D-2 decides the status codes):

- **A question the agent could not answer is not an HTTP error.** The request
  succeeded; the answer is negative. `200` with `ok: false` and a category.
- **`rate_limited`** is the exception worth care. It is a billing condition,
  and B-1 records the cost of letting one surface as a security-shaped message.
  It gets its own category, its own user-facing sentence, and never the word
  "rejected".
- **`connection_error` and `provider_error`** are genuine infrastructure
  failures — the service, not the question. `503`.

### 2.3 The JSON boundary

`ExecutionResult`'s docstring already specifies this and defers it to "the API
boundary" (spec §2.2). One encoder, one module, per spec §5.

- **`datetime` → ISO 8601 string.** Uncontroversial, and untestable against the
  corpus, which has zero temporal columns (spec §2.5) — so its test uses a
  synthetic query rather than a gold one.
- **`Decimal` → decided by D-1.** This is the substantive one and it is not a
  detail: `float(Decimal("2328.60"))` is the precision loss the docstring
  refuses, and `006`'s six-decimal comparison policy was explicitly left
  unloosened once already.

---

## 3. The page

One HTML file, one CSS file, one JS file. An input, a button, and a result
region that renders by `shape`:

- **`scalar` → the number, large.** The majority case (56%) gets the majority
  of the design attention, per AC9.
- **`list` / `table` → a plain table.** At most 55 rows (spec §2.6), so no
  virtualisation.
- **`chartable` → a table, plus a "Show chart" toggle, off by default.**

**The chart is hand-rolled**, subject to D-3. Every chartable result is a
category and one measure, ≤55 rows, and there are no time series anywhere in
the corpus — so a horizontal bar per row, widths as percentages of the maximum,
is the entire requirement. Pulling in a charting library over a CDN would break
Q-A's dependency-free property and add a network dependency to a demo whose
whole selling point is `docker compose up`.

The SQL is always visible (AC8). The trace is in a collapsed `<details>`
(Q-E) — `<details>` needs no JavaScript, which is a small but real reason to
prefer it.

---

## 4. Files

| file | change | why |
|---|---|---|
| `api/main.py` | edit | `POST /ask`, `GET /` — thin, delegating |
| `api/http/serialization.py` | **new** | the JSON boundary (§2.3) |
| `api/http/shapes.py` | **new** | result-shape classifier, shared with the harness |
| `api/http/errors.py` | **new** | category → response mapping + completeness |
| `api/web/index.html` | **new** | the page |
| `api/web/app.js`, `api/web/styles.css` | **new** | render and style |
| `specs/000-project.md` | edit | T1, the charter amendment (Q-C) |
| `README.md` | edit | quickstart, layout, and two stale claims (D-4) |
| `tests/test_ask_endpoint.py` | **new** | contract, categories, safety |
| `tests/test_serialization.py` | **new** | precision and encoding |
| `tests/test_result_shapes.py` | **new** | classifier, incl. the `easy-010` case |

**Static files live under `api/`, not in the root `frontend/`** — see D-4. The
Docker build context is `./api` with `COPY . ./api/`, so anything outside
`api/` is invisible to the image. That is a measured constraint, not a
preference.

`api/http/` is a new package rather than three modules in `api/`, following
`tools.py`'s registry rule: `main.py` stays a thin surface and the logic sits
in domain modules.

---

## 5. Test plan, with the mutations

Mutation testing is mandatory. For each defence, the mutation that must turn a
test red:

| defence | mutation | expected |
|---|---|---|
| Decimal precision (AC3) | encode via `float()` | fails on a value that discriminates — **not** `0.1 + 0.2`, which quantises identically both ways |
| No safety bypass (AC4) | endpoint calls `engine.execute` directly | AST structural test fails |
| No pacing in the API (AC5) | wrap the endpoint's provider in `PacedProvider` | structural test fails |
| Category completeness | add a category constant, leave it unmapped | completeness test fails |
| `rate_limited` distinctness (AC6) | map it onto the `rejected` response | test asserting the category by constant fails |
| Shape classifier | classify `easy-010` as `scalar` | classifier test fails |
| Scalar rendering (AC9) | return `chartable` for a 1×1 result | shape test fails |

**The precision mutation is the one most likely to pass vacuously**, so its
value is chosen deliberately: `Decimal("4.0000005")` survives the round trip
under a correct encoder and does not under `float()`. `HANDOFF.md` §6 records
the earlier version of this exact test proving nothing.

Endpoint tests assert **categories by constant**, never by substring — B-1
records two iterations of a rate-limited run failing red as a prompt-injection
alarm because a test substring-matched a message that later changed.

---

## 6. Risks

- **The static-file path differs between host and container.** `COPY . ./api/`
  lands the package at `/app/api/`, so a path resolved relative to the current
  working directory will work in one and not the other. Resolve it from
  `__file__`, and test that the file is actually readable rather than that a
  route returns 200.
- **`shape` becomes a second source of truth.** If the classifier in
  `api/http/shapes.py` and the ad-hoc one used for spec §2.3's measurements
  disagree, the spec's numbers stop describing the product. Mitigation: the
  measurement script is replaced by an import of the real classifier, and a
  test asserts the corpus distribution matches spec §2.3 exactly — 28 scalar,
  10 chartable. **That test also pins the measurement**, so a reseed or a
  dataset change that would invalidate spec §2.3 fails loudly.
- **A demo is judged on the failure path.** AC12 exists because the most likely
  live-demo failure is a rate limit, and "something went wrong" would be the
  worst possible thing on screen at that moment.
- **Scope creep toward a dashboard.** The non-goals list is unusually long for
  this reason. Anything resembling history, saved questions, or multi-chart
  layout belongs to Iteration 7 or later.

---

## 7. Decisions

**These need answers before T2.** Each is a question the plan raised, not one
the spec left open.

- **D-1 — How does `Decimal` cross the JSON boundary?** `float()` is refused by
  `ExecutionResult`'s own docstring. The candidates: (a) a JSON **string**,
  exact, but the client must parse it before charting; (b) a raw JSON **number**
  emitted at full precision, which every browser then parses to float64 anyway,
  so the fidelity is lost at the only place it would be read; (c) both — a
  string for display and a float for the chart.

  *My lean: (a), a string.* The product's claim is auditability, and the
  displayed number should be the database's number, exactly. The chart is the
  approximate view by nature — parsing to float for a bar width loses nothing
  that matters — and (c) doubles the payload and invites the two fields to
  disagree. But this decides how every number in the product is rendered, so it
  is yours.

  > **Resolved 2026-09-09: (a), a JSON string.** *"Auditability is the entire
  > selling point. The UI must render the exact value from the database without
  > float64 precision loss. We can parse it to float on the client side
  > exclusively for calculating SVG bar widths, where precision loss is visually
  > irrelevant."* So the display path is exact and the chart path is explicitly
  > allowed to be approximate — the one place a float is correct is a bar width.

- **D-2 — Which HTTP status does a failed question return?** §2.2 proposes
  `200` with `ok: false` for answer failures and `503` for infrastructure. The
  alternative is `422` for the agent's inability to answer, which is more
  RESTful and makes failures visible in access logs without parsing bodies.

  *My lean: `200` with `ok: false`.* The request was understood and processed;
  the agent's inability to answer is a result, not a protocol error. Iteration
  7 owns logging and can log the category directly.

  > **Resolved 2026-09-09: `200` with `ok: false`.** *"The API successfully
  > received, parsed, and executed the workflow. The agent's inability to answer
  > is a valid domain result, not a server crash or a malformed request. We will
  > reserve 503 for genuine infrastructure failures."*

- **D-3 — Hand-rolled bars, or a charting library?** §3 argues hand-rolled: one
  chart type, ≤55 rows, no time axis, and Q-A's dependency-free property.

  *My lean: hand-rolled SVG.* The alternative is a CDN script tag, which means
  the demo needs network access and the "only setup instruction is
  `docker compose up`" claim quietly becomes false.

  > **Resolved 2026-09-09: hand-rolled SVG.** *"Stick to the dependency-free
  > mandate. For the ≤18% of queries that are chartable, dynamically calculating
  > horizontal SVG bar widths via Vanilla JS is trivial and keeps our
  > `docker compose up` promise strictly true."*

- **D-4 — What happens to the root `frontend/` directory?** It contains only a
  `.gitkeep`. `README.md` describes it as a *"Next.js chat UI (Iteration 6)"* —
  a stack Q-A has now rejected. The static files must live under `api/`
  regardless, because of the build context.

  *My lean: delete `frontend/` and correct the README.* Leaving an empty
  directory whose documented purpose was explicitly rejected is a trap for the
  next reader. The README carries a second stale claim worth fixing in the same
  edit: it says the benchmark scores **40** reference queries, and the corpus
  has been **50** since dataset v3 added the `expert` tier.

  > **Resolved 2026-09-09: delete it and correct the README.** *"Leaving an
  > empty directory with stale Next.js documentation is a trap. Fix the 40-to-50
  > corpus count in the README while you are at it."* Both land in T8.

---

## 8. Proposed decomposition

Pause after **T1**, **T3** and **T6** — the points where something is worth
looking at before more is built on it.

| task | what | verified by |
|---|---|---|
| **T1** | Charter amendment (Q-C): quote 56% scalar, 0% time series, ≤18% chartable; state what "a chart" now promises | Read it. **Pause.** |
| **T2** | `api/http/shapes.py` + the classifier test that pins spec §2.3's distribution | Corpus distribution matches 28/10 exactly |
| **T3** | `api/http/serialization.py`, the JSON boundary, D-1 applied | Precision mutation. **Pause.** |
| **T4** | `api/http/errors.py`, all eleven categories mapped + completeness test | Unmapped-category mutation |
| **T5** | `POST /ask` in `main.py`, with the AC4/AC5 structural tests | `curl` answers a real question; both mutations |
| **T6** | `GET /` and the page: scalar, table, SQL, collapsed trace | A human opens it. **Pause.** |
| **T7** | The chart toggle (D-3) | Toggle on `medium-003`; absent on a scalar |
| **T8** | README and layout corrections (D-4), delete `frontend/` | Fresh-clone read-through |

**T1 first, deliberately.** Q-C's resolution says the charter and the spec
should agree *before* code is written, rather than the record catching up
afterwards.

No task in this decomposition spends provider tokens except T5's manual
verification — roughly 1,300 tokens for one live question. The day's remaining
allowance is not a constraint on this iteration, which is a first for the
project.
