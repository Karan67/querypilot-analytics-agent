# 009 — Iteration 6: Frontend

Status: **approved 2026-09-09**, §7 resolved · Created: 2026-09-09

> **Resolved questions.** Q-A server-rendered HTML and vanilla JS, no npm ·
> Q-B chart is a user toggle, default off · Q-C amend the charter in this
> iteration's PR, quoting the measurements · Q-D synchronous · Q-E return the
> trace, render it collapsed. Each is recorded in §7 beside the question it
> answers, with the reasoning given.

Inherits every constraint in [`000-project.md`](000-project.md). Where this
document and the charter disagree, the charter wins.

> **Done when** (charter §6): *demoable to a non-technical person.*

---

## 1. Intent

The charter's §1 promises three things together: **the answer**, **the SQL that
produced it**, and **a chart chosen from the shape of the result set**. Five
iterations have built the first two and measured them hard. Neither has ever
been visible to anyone who does not run `pytest` or the eval runner.

This iteration makes the agent reachable and legible: a question typed in a
browser, an answer with its SQL, and a chart where a chart means anything.

**The intended user understands the business but not the schema** — the charter's
words. That person cannot read `EVALS.md`, cannot start a Python process, and
will not be impressed by 100.0% held-out accuracy. They will judge the product
on whether it answers their question and whether they believe it.

### The thing this iteration is really for

**Auditability, not decoration.** The SQL is on screen because an answer nobody
can check is worth less than no answer. Iteration 4 built a loop that reads its
own errors; Iteration 5 measured which prompt makes it right. Neither is
believable to a non-technical viewer without the query in front of them, and
the moment it is on screen the product stops being a chatbot that emits numbers.

---

## 2. What the measurements say

Taken 2026-09-09 against the live database and the deployed model. Numbers here
are measured, never estimated — §2's standing rule.

### 2.1 There is no backend to build a frontend against

`api/main.py` exposes **one endpoint, `/health`**, and is still at Iteration 0
scope. Its own module docstring says *"the tools arrive in Iteration 1, the agent
loop in Iteration 4"* — **neither ever landed in the API.** `answer()` lives in
`api/agent/orchestrator.py` and is reachable from the eval runner and the test
suite and from nowhere else.

**So Iteration 6 is two pieces of work, not one**, and the smaller one is the
UI. Naming this now matters because *"Frontend"* on the charter's map reads like
a single afternoon of HTML.

### 2.2 The JSON boundary is specified but unbuilt

`ExecutionResult`'s docstring states the contract already:

> `rows` holds **native Python types** — `Decimal` for NUMERIC, `datetime` for
> TIMESTAMP. Converting to JSON primitives happens at the API boundary, not
> here: `Decimal` to `float` silently loses precision, and an analytics product
> should not degrade its numbers on the way out of the database.

That boundary is where this iteration puts it, and the precision rule is
inherited rather than re-decided. It interacts with `006`'s six-decimal
comparison policy, which the user has already refused to loosen once.

### 2.3 **56% of the corpus is a single number, and that reshapes the chart promise**

Every one of the 50 gold queries, executed through `execute_sql()`:

| shape | count | share |
|---|---|---|
| **scalar** (1 row, 1 column) | **28** | **56.0%** |
| category + one measure (2 col, multi-row) | 10 | 20.0% |
| table, 2 columns | 6 | 12.0% |
| single-column list | 3 | 6.0% |
| table, 3 columns | 2 | 4.0% |
| table, 4 columns | 1 | 2.0% |

29 of 50 results are exactly one row. **The charter's "a chart, chosen
automatically from the shape of the result set" applies to at most a fifth of
the questions this product has ever been measured on**, and is meaningless for
the majority, where the honest rendering of the answer is the number itself,
large.

### 2.4 Shape alone cannot decide what is chartable

Of the 10 mechanically chartable results — multi-row, two columns, exactly one
numeric — **at least one is a false positive**. `easy-010` is *"List the id and
name of every playlist"*, whose numeric column is a **playlist id**. A bar chart
of 18 identifiers is nonsense that passes every shape test.

So the real figure is **9 or fewer of 50, under 18%**, and a rule phrased purely
over shape will confidently draw the wrong chart. Distinguishing a *measure*
from an *identifier* is not a shape question.

### 2.5 There are no time series in the corpus at all

**Zero of 50 results contain a date or timestamp column.** Not one. A charting
library chosen for its time-axis handling would be chosen on no evidence, and a
line chart has no question in this benchmark to draw.

This is a fact about Chinook and the corpus rather than about analytics, and it
is the same shape of finding as `008` §2.4's *Chinook cannot express metric
ambiguity*: the textbook design does not fit the data in front of us.

### 2.6 Results are small, and latency is dominated by one model call

Largest result in the corpus: **55 rows**. Measured end to end on three
questions, through `answer()` against `openai/gpt-oss-120b`:

| question | latency | provider calls | tokens | JSON payload |
|---|---|---|---|---|
| `easy-001`, scalar | 2.51s | 1 | 1,070 | 211 B |
| `medium-003`, chartable | 1.20s | 1 | 1,276 | 536 B |
| `hard-007`, chartable | 2.28s | 1 | 1,326 | 718 B |

**n=3, and stated as such.** The range is 1.20–2.51s and a single call every
time, consistent with Iteration 5's finding that the loop uses one call rather
than its budget of three. Payloads are under a kilobyte.

Two consequences. **Pagination, virtualisation and result streaming are
unjustified** at 55 rows and 718 bytes. And **the user waits on the model, not
on us** — so the interface's job during that 1–3 seconds is to show that
something is happening, not to be fast.

---

## 3. Acceptance criteria

### The HTTP surface

- **AC1** — A **`POST /ask`** endpoint accepts a natural-language question and
  returns the agent's result. It calls `answer()` and adds nothing to the
  reasoning: the agent is not reimplemented behind HTTP.
- **AC2** — The response carries **the answer, the SQL, and the failure detail
  when it fails**, so the UI never has to guess why something did not work.
- **AC3** — **`Decimal` and `datetime` cross the boundary without precision
  loss** (§2.2). Asserted with a value that actually discriminates — `0.1 + 0.2`
  quantises identically under both constructions and proves nothing, per the
  trap `HANDOFF.md` §6 records.
- **AC4** — **Nothing bypasses the safety layer.** The endpoint reaches the
  database only through `execute_sql()`, and a structural test asserts it.
  Charter §4 is not relaxed by adding a transport.
- **AC5** — **The deployed API does not pace.** `PacedProvider` wraps the
  benchmark only, per B-1: sleeping inside a user's request trades a rare
  refusal for a guaranteed delay.
- **AC6** — A provider failure, a rate limit and a validator rejection each
  produce a **distinct, non-alarming** response the UI can render. A billing
  condition must not surface as a security-shaped message — the exact defect
  B-1 found in the test suite.

### The interface

- **AC7** — A question can be asked and answered **in a browser, with no
  terminal**, against `docker compose up`.
- **AC8** — **The SQL is visible by default**, not behind a toggle. §1's
  auditability argument is the whole point, and a hidden query is a chatbot.
- **AC9** — **A scalar answer renders as the number**, not as a one-bar chart
  (§2.3). This is the majority case and it gets the majority of the design
  attention.
- **AC10** — **A chart appears only where one carries meaning**, and the rule
  that decides is stated in the spec rather than discovered in the code. See
  Q-B: shape alone is insufficient (§2.4).
- **AC11** — **The waiting state is honest**: the user is told the model is
  working, and 1–3 seconds (§2.6) is designed for rather than hidden.
- **AC12** — A **failed** question shows what failed and the SQL that failed,
  if any. The demo audience learns more from a legible failure than from a
  spinner that stops.

### Honesty

- **AC13** — **No accuracy claim appears in the UI.** Not "100% accurate", not a
  confidence score. The held-out number is one pass at 100.0% and honestly reads
  *between 90% and 100%*; a product surface is exactly where that nuance dies.
- **AC14** — **The demo path is the real path.** No fixtures, no canned answers,
  no pre-baked responses behind a flag. If the model is down the demo fails, and
  that is correct.

---

## 4. Non-goals

- **Authentication, accounts, multi-tenancy.** Nothing here is deployed to a
  hostile network in this iteration; Iteration 8 owns shipping.
- **Pagination, virtualisation, streaming results.** Unjustified at 55 rows and
  718 bytes (§2.6).
- **Time-series or trend visualisation.** Zero corpus results have a temporal
  column (§2.5). Building it would be building for an imagined dataset.
- **Conversation history and follow-up questions.** Charter §6 assigns history
  to Iteration 7. One question, one answer.
- **A component library or design system.** The bar is *demoable to a
  non-technical person*, not *pretty to a designer*.
- **Changing the agent, the prompt, or the dataset.** This iteration adds a
  surface. Any accuracy movement here would be an accident and would invalidate
  five iterations of comparability.
- **Charting the 56%.** Explicitly refused rather than forgotten (§2.3).

---

## 5. Contracts

- **`answer()` is unchanged.** The endpoint adapts it; it does not grow HTTP
  concerns. Same reasoning as the LLM provider interface — the loop does not
  learn about its callers.
- **`complete(system, user) -> str` is unchanged.** Charter §5, non-negotiable.
- **No vendor SDK outside `api/llm/`.** Enforced structurally, and widening it
  for a web framework is not on the table.
- **`tools.py` stays a registry.** If the API needs logic, it goes in a domain
  module (charter, and the user's explicit widening of that instruction).
- **Every database read goes through `execute_sql()`**, including anything the
  UI needs for its own purposes.
- **The JSON encoder is one function in one place.** Two encoders disagreeing
  about `Decimal` is precisely the drift `HANDOFF.md` §6 warns about.

---

## 6. Verification

- Structural tests for AC4 and AC5, asserted **against the parsed AST**, never
  by grepping source — that mistake has been made twice in this repository.
- A precision test for AC3 whose value discriminates, per AC3's own note.
- Endpoint tests for each failure category in AC6, asserting the *category*
  rather than substring-matching a message. B-1 records what substring matching
  cost when a category was renamed.
- **Mutation testing at every task**, per the standing rule. The recurring bug
  shape is *a default elsewhere silently standing in for the code under test*,
  and a JSON boundary with a fallback encoder is an obvious place for it.
- The UI is verified by a human opening it. There is no honest automated
  assertion of *demoable to a non-technical person*, and pretending otherwise
  would be theatre.

---

## 7. Open questions

**Answer these before the plan is drafted.** Each carries my lean, which is a
starting position and not a decision.

- **Q-A — What is the frontend built with?** The options are (1) server-rendered
  HTML from FastAPI with a little vanilla JS, (2) a single static page with a
  CDN-loaded framework, (3) a real build pipeline with npm and a bundler.

  *My lean: (1).* The charter's bar is *demoable*, and options 2 and 3 add a
  toolchain, a build step and a second language ecosystem to a repository whose
  deliberate character is one language and no frameworks. Option 1 also keeps
  `docker compose up` as the entire setup instruction, which is what makes the
  demo reproducible by someone who is not you.

  > **Resolved 2026-09-09: option 1, server-rendered HTML and vanilla JS.** The
  > user's reasoning, and not that of someone unfamiliar with the alternative:
  > *"my background in full-stack web development typically leans toward
  > component-based architectures like React.js, [but] introducing an npm build
  > pipeline just to render a scalar value and a SQL string violates our
  > simplicity mandate. Keep it dependency-free so `docker compose up` remains
  > the absolute only setup instruction."*
  >
  > **Achievable literally.** Every render happens client-side after a `fetch`,
  > so no templating engine is needed and **no new runtime dependency is added
  > at all** — `HTMLResponse` and Starlette's `StaticFiles` already ship with
  > FastAPI.
  >
  > It also strands the root `frontend/` directory, which holds only a
  > `.gitkeep` and which `README.md` describes as a *"Next.js chat UI"* that was
  > never built and now never will be. See the plan's D-4.

- **Q-B — What rule decides whether to draw a chart?** §2.4 shows shape alone
  is insufficient — a playlist id passes every shape test. Candidates: (1)
  shape plus a column-name heuristic (`count`, `total`, `avg`, `sum`), (2) ask
  the model to declare the shape as part of its answer, (3) let the user toggle
  a chart on any multi-row result and default to off.

  *My lean: (3), with (1) as a refinement.* Option 2 widens the agent's
  contract and puts a presentation concern inside the reasoning loop, which
  five iterations of architecture have kept out. Option 3 cannot be wrong,
  because the human decides — and it means the 56% scalar case never has to be
  argued about. But it makes the charter's *"chosen automatically"* a partial
  promise, so this is genuinely your call rather than mine.

  > **Resolved 2026-09-09: option 3, a user toggle defaulting to off.** *"Let
  > the human decide if the multi-row data warrants a chart. Shape heuristics
  > will inevitably generate false positives (like charting playlist IDs)."*
  > The `easy-010` false positive in §2.4 is therefore not an edge case the
  > design tolerates, it is the reason the design exists.

- **Q-C — Does the charter's chart promise get revised?** §2.3 and §2.5 say the
  automatic-chart language was written before anyone measured what the answers
  look like: under 18% chartable, zero time series. Either the charter is
  amended to say what this iteration will actually deliver, or the iteration
  carries a knowingly-unmet promise the way `008` carried AC13.

  *My lean: amend the charter, in the same PR, with the measurement quoted.*
  This project's convention is that the record changes when a measurement
  contradicts it, and that the old text stays visible. An aspiration silently
  outliving its evidence is the thing `EVALS.md`'s discipline exists to stop.

  > **Resolved 2026-09-09: amend the charter in this iteration's PR, quoting
  > the 56% scalar and 0% time-series measurements directly.** *"An aspiration
  > must not silently outlive the empirical evidence that disproves it."* The
  > amendment is the plan's T1 rather than a closing tidy-up, so the charter and
  > the spec agree before any code is written.

- **Q-D — Is `/ask` synchronous?** At 1.20–2.51s (§2.6) a plain blocking request
  is honest and simple. Streaming tokens or a job-and-poll design would both
  look more sophisticated.

  *My lean: synchronous.* Three seconds does not need a job queue, and the loop
  produces its SQL in one shot rather than incrementally, so there is nothing
  meaningful to stream. Revisit if Iteration 7's work lengthens the path.

  > **Resolved 2026-09-09: synchronous.** *"At a 1–3 second P99 latency,
  > introducing WebSockets, streaming, or job-polling is architectural theater.
  > Block and wait."* Noted for the record that §2.6's range is n=3 and is a
  > measured range rather than a true P99; the decision does not depend on the
  > distinction, but a later claim about tail latency would.

- **Q-E — Does the API surface the agent's trace?** `AgentResult` carries
  `steps`, `attempts_used` and `usage`. A retry visible on screen is the single
  most compelling demonstration that this is an agent and not a prompt — it is
  literally the charter §1 diagram — but it is also noise for the intended
  non-technical user, and `usage` exposes token costs.

  *My lean: return the trace, render it collapsed.* The demo argument is strong
  and it costs nothing to include in the payload at 718 bytes. But defaulting it
  to hidden contradicts AC8's reasoning about the SQL, so if you want it visible
  by default, say so.

  > **Resolved 2026-09-09: return it, render collapsed.** *"The retry loop is
  > the literal proof that this is an agent rather than a wrapper around a
  > single prompt. Surface it for auditability, but collapse it by default to
  > keep the business user's focus on the answer and the SQL."* So the SQL and
  > the trace are deliberately treated differently: the SQL is the audit of the
  > *answer* and is always visible (AC8); the trace is the audit of the
  > *process* and is one click away.

---

## 8. What this unblocks

Iteration 7's hardening — history, feedback, latency and cost logging — all
assume a request path that a human actually uses. There is no useful latency
log for a benchmark that runs 30 questions in a loop, and no feedback to collect
from a test suite. **Iteration 6 is what makes Iteration 7's subject exist.**

It also converts five iterations of measured-but-invisible work into something
that can be shown, which is the charter's own bar and the one thing the project
has never been able to do.
