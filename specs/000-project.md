# 000 — QueryPilot: Project Charter

Status: **active** · Created: 2026-08-21

This is the root spec. Every feature spec (`001-…`, `002-…`, …) inherits the
constraints declared here. Where a feature spec and this document disagree, this
document wins unless the feature spec explicitly states it is overriding it, and why.

---

## 1. Intent

QueryPilot answers business questions about a real relational database, asked in
plain English, and returns three things together:

1. **The answer** — the actual rows or aggregate the question asked for.
2. **The SQL that produced it** — so the answer is auditable rather than trusted.
3. **A chart** — chosen automatically from the shape of the result set.

The intended user understands the *business* but not the *schema*. They can ask
"which genres sold the most in 2013?" but cannot write the four-table join that
answers it.

### What makes this an agent, not a prompt

The distinguishing property is a **bounded self-correction loop**. A model is
given tools, calls them, reads the results — *including errors* — and decides what
to do next:

```
question → get_schema → draft SQL → validate_sql → execute_sql → final_answer
                ↑                                       │
                └──────── read the error, revise ───────┘
                              (max N attempts)
```

When `execute_sql` returns `column "artist_name" does not exist`, that error text
goes back into the model context and it tries again. A chatbot hands the broken
SQL to the user; an agent reads the failure and fixes it. Everything else in this
project exists to make that loop safe, measurable, and honest.

### Why the project is worth building

Text-to-SQL is one of the few LLM applications with a **machine-checkable ground
truth**. A generated query either returns the expected result set or it does not.
Accuracy is therefore a number rather than a vibe — and a number that moves over
time is the entire argument this project makes.

---

## 2. Success criteria

The project succeeds when all of the following are simultaneously true:

| # | Criterion | How it is proven |
|---|---|---|
| S1 | The agent self-corrects | A question that fails on attempt 1 succeeds on attempt 2, with both attempts persisted and inspectable |
| S2 | Accuracy is measured, not claimed | `run_evals.py` scores a held-out question set and prints an execution-accuracy figure |
| S3 | Accuracy improved for stated reasons | `EVALS.md` records baseline → current with a diagnosis attached to each jump |
| S4 | Unsafe SQL cannot execute | An adversarial suite (DDL, DML, stacked statements, comment injection) is blocked 100%, at both the AST gate and the database role |
| S5 | It is demoable | A non-technical person can ask a question in the UI and watch the agent steps stream in |
| S6 | It runs from cold | `docker compose up` on a clean machine yields a working system |

**S4 has no acceptable failure rate.** A single successful write originating from
a generated query invalidates the central claim of the project. Accuracy
regressions are bugs; safety regressions are stop-the-line events.

---

## 3. Scope

### In scope

- **Read-only analytical querying** over one PostgreSQL target database.
- **Sample dataset: Chinook** — a music-store schema (artists, albums, tracks,
  invoices, invoice lines, customers, employees, genres, playlists). Chosen
  because it is multi-table, has real foreign keys, and has enough referential
  depth that interesting questions require non-trivial joins. AutoMart remains a
  possible second dataset later; it is not part of Iteration 0.
- **A hand-written agent loop** over an explicit tool set: `get_schema()`,
  `sample_rows(table)`, `validate_sql(sql)`, `execute_sql(sql)`, `final_answer(...)`.
- **A non-bypassable safety layer** (see §4).
- **An eval suite** — 30–50 questions across easy/medium/hard tiers, scored by
  execution accuracy, runnable locally and in CI.
- **A metadata store** — query history, agent steps, eval runs, feedback, latency
  and token cost.
- **A streaming frontend** — Next.js chat UI, SSE-streamed agent steps, SQL
  viewer, results table, auto-selected Recharts visualisation.
- **Deployment** — Vercel (frontend), Render or Fly.io (API), Supabase (Postgres).

### Non-goals

Listed so that neither I nor a coding agent quietly expands the project. These are
not "later" — they are **out**, unless a future spec deliberately reverses one.

| Non-goal | Why |
|---|---|
| **Writing to the target database** | The system is analytical. No `INSERT` / `UPDATE` / `DELETE` / DDL is ever generated, validated, or executed — not behind a flag, not in an admin mode, not for tests |
| **Multi-dialect support** | PostgreSQL only. No MySQL / Snowflake / BigQuery abstraction layer |
| **Multi-tenancy, auth, user accounts** | Single-user demo. No login, no per-user data isolation, no RBAC |
| **A semantic layer or metrics store** | The agent reads the physical schema. No dbt, no cube, no curated metric definitions |
| **Fine-tuning or training a model** | Prompting, grounding, and retry logic only |
| **Agent frameworks** | No LangChain, LlamaIndex, CrewAI, AutoGen, or equivalent — see §5 |
| **Conversational memory across turns** | Each question is independent. Follow-ups such as "and for 2014?" are out until a spec explicitly adds them |
| **LLM-written prose summaries of the result set** | The answer is the data. Narrating results is a separate, later decision |
| **User-customisable charts** | Chart type is auto-selected from result shape. No chart editor |
| **Cost and latency optimisation ahead of accuracy** | Caching and cost work is Iteration 7, after accuracy is measured and improved |
| **A general classifier for unanswerable questions** | The agent should decline rather than invent columns, but detecting unanswerability in general is out |

---

## 4. The safety layer (binding on all future work)

The safety layer is the one part of this system that is not permitted to have a
bypass. It is **defence in depth**: three independent gates, each of which must
hold on its own if the other two fail.

**Gate 1 — Database level.** The agent connects with a PostgreSQL role
(`querypilot_ro`) holding `CONNECT`, `USAGE`, and `SELECT` grants and nothing
else. A flawless jailbreak of the model still cannot write, because the privilege
to write does not exist on that connection. Created in Iteration 0.

**Gate 2 — AST level.** Every candidate query is parsed by `sqlglot` before it
goes near the database. Anything that is not exactly one `SELECT` statement is
rejected with a reason. **Parsing, never regex** — keyword blacklists are defeated
by comments, casing, string splitting, and encoding, and shipping one would be a
false sense of security worse than no gate at all.

**Gate 3 — Resource level.** An enforced `LIMIT`, a Postgres `statement_timeout`,
and a maximum returned-row cap, so no generated query can hang the API or pull an
entire table into memory.

`statement_timeout` is pinned to the `querypilot_ro` role (`ALTER ROLE ... SET`,
default `10s`, configurable via `QUERYPILOT_STATEMENT_TIMEOUT`) so that every
session the role opens inherits it, including connections the pool reopens later.

**This gate depends on Gate 2 to be enforceable.** `ALTER ROLE ... SET` installs a
per-session default, not a hard ceiling — the role can raise it with
`SET statement_timeout = 0`. It holds only because Gate 2 rejects anything that is
not a single `SELECT`, leaving no route to issue a `SET`. Consequence: **if Gate 2
is ever widened to permit further statement types, the timeout stops being an
enforced limit** and the resource gate must move into the session or the pool.
Any spec proposing to loosen Gate 2 must address this explicitly.

### Standing rules

- **No code path may execute SQL without passing through the validator.** Not
  tests, not scripts, not `run_evals.py`, not a temporary debug helper.

  **One recorded exemption:** `tests/test_validator_gates.py` sends forbidden
  statements straight to PostgreSQL, because proving Gate 1 refuses them
  *independently* is impossible any other way — routing them through the
  validator would only prove the validator agrees with itself, and the whole
  claim being tested is that the gates do not depend on each other. It is
  bounded: the connection is `querypilot_ro`, every statement runs in a
  transaction that is **always rolled back** (PostgreSQL makes DDL
  transactional, so even a Gate 1 failure leaves nothing behind), a row count
  is asserted unchanged, and the payloads are a hardcoded corpus rather than
  model output. Executing *generated* SQL there would be a different decision
  needing its own entry here.
- The read-only DSN and any privileged DSN are never interchangeable. Seeding and
  migration credentials live outside the runtime configuration of the API.
- Every rejection carries a *reason*. Silent failures are unacceptable: the agent
  needs the reason in order to retry, and the operator needs it to debug.
- Safety-relevant behaviour ships with an adversarial test in the same task.

---

## 5. Architectural commitments

Deliberate choices, recorded here so that "wouldn't it be easier to just…" is
answered by the spec rather than by fatigue.

**The agent loop is hand-written Python.** No LangChain, no LlamaIndex, no agent
framework. The loop — build the prompt, parse the tool call, dispatch it, append
the observation, decide whether to continue — is the learning value of the project
and the part that gets questioned in interviews. A framework hides exactly the
part worth understanding.

**The LLM sits behind a swappable interface.** Provider code (Groq, Gemini, or
anything else) is reachable only through one narrow abstraction. Nothing in the
orchestrator, tools, or safety layer imports a vendor SDK directly. Changing
provider must be a configuration change, not a refactor.

**Tools before the agent that uses them.** Each tool is built and unit-tested
standalone (Iteration 1) before any LLM is wired up (Iteration 2). The safety
layer exists before the thing that generates unsafe input.

**`api/agent/tools.py` is a registry, not an implementation.** It holds the
agent's complete tool surface — one thin registered wrapper per tool — and each
wrapper delegates to the implementation in its own domain module
(`get_schema()` → `api/db/introspection.py`, `validate_sql()` →
`api/safety/validator.py`). The point is that by Iteration 4 a reader can open
one file and see every capability the agent has, without that file having
decayed into a grab bag of unrelated implementation. This applies to all tools,
not just the first.

**Evals before optimisation.** The eval suite lands at Iteration 3, ahead of any
accuracy work. Tuning prompts without a scoreboard is guessing, and a guessed
improvement cannot honestly be written down.

**Target DB and metadata store are separate concerns.** The target database is
read-only to the agent. History, eval runs, and feedback are written to the
metadata store — never back into the database being analysed.

---

## 6. Iteration map

Each iteration is one full pass through SPECIFY → PLAN → DECOMPOSE → IMPLEMENT →
VERIFY. One iteration at a time; one task at a time within an iteration.

| # | Iteration | Done when |
|---|---|---|
| 0 | Foundation | `docker compose up` gives a running API that queries the sample DB over a read-only connection |
| 1 | Tools before agent | `validate_sql()` rejects DROP, DELETE, UPDATE, stacked statements, and comment-based injection |
| 2 | Single-shot SQL | Simple single-table questions work end-to-end with one LLM call |
| 3 | Evals | A baseline accuracy number exists in `EVALS.md`. It will be mediocre — that is the point |
| 4 | The agent loop | A query failing on attempt 1 succeeds on attempt 2, and accuracy moves measurably |
| 5 | Accuracy work | A documented accuracy climb with the reasoning behind each jump |
| 6 | Frontend | Demoable to a non-technical person |
| 7 | Hardening | History, feedback, latency and cost logging, rate limiting, caching |
| 8 | Ship | Deployed, evals running in CI, README with honest numbers, demo video |

---

## 7. Working method

- **The spec is the source of truth, not the chat history.** Requirements are not
  invented mid-implementation. If a coding agent needs a decision that no spec
  contains, it stops and asks rather than assuming.
- **A feature is not understood until its acceptance criteria are written.** If a
  testable criterion cannot be stated, the feature is not ready to delegate.
- **A task is done when its test passes**, not when the agent reports success. The
  claim "I implemented X" is checked against the code, not accepted.
- **Every accuracy number is logged in `EVALS.md`, including the bad ones.** A
  regression that gets quietly deleted destroys the value of the whole record.

---

## 8. Carried debt

Work that a completed iteration knowingly deferred. Distinct from §9's open
questions: nothing here is undecided, it is decided and unbuilt. An item leaves
this table only when it ships or when a spec records why it never will.

| # | Item | Opened | Target |
|---|---|---|---|
| ~~B-1~~ | ~~Rate-limit telemetry on `GroqProvider`~~ | Iteration 5 T8 | **discharged 2026-09-04** |
| ~~B-2~~ | ~~AC13's glossary-off control arm~~ | Iteration 5 T7 | **discharged 2026-09-08** |
| ~~B-3~~ | ~~T8's held-out run on a clean quota~~ | Iteration 5 T8 | **discharged 2026-09-04** |
| **B-4** | Alternative LLM provider, with re-baselining | Iteration 5 close | deferred, own milestone |
| **B-6** | Exercise 429 → ledger reconciliation against the live API | B-5 | open — accepted debt |
| **B-7** | Which `expert` questions the glossary actually rescues | B-2 | open |
| ~~B-5~~ | ~~Guard all three limits, and count the day not the invocation~~ | B-1 | **verified live 2026-09-08** |

### B-1 — Rate-limit telemetry on `GroqProvider`

**The pre-flight budget check reads a local heuristic and never asks the
provider what is actually left.** `project_run_cost` counts the worst case with
`tiktoken`, which is the right instrument for a decision that must be
reproducible offline, but it is blind to the one number that decides whether a
run can start: how much of today's quota the account has already spent. Iteration
5 met this twice — T7's `ddl` arm and T8's held-out run each lost one question to
a rate limit that nothing saw coming, and the second cost the iteration its
`EVALS.md` entry.

Groq returns the answer on every response: `x-ratelimit-limit-tokens`,
`x-ratelimit-remaining-tokens` and `x-ratelimit-reset-tokens`, plus `retry-after`
on a 429. Exposing them follows D-1's settled precedent exactly — a best-effort
attribute on the concrete provider, read with `getattr`, alongside `last_usage`.
**`complete(system, user) -> str` does not change**, which is the whole reason
that precedent exists.

Two things it would buy: a pre-flight check that refuses on *measured* remaining
quota rather than on a projection, and a reported reset time, so the question
*when does the quota clear* stops being answered from memory. Iteration 5 could
only answer it as "probably 00:00 UTC, unverified", which is precisely the shape
of claim this project spends its effort eliminating.

> **MEASURED AND DISCHARGED 2026-09-04. There are three limits, not one, and
> the one that actually stopped Iteration 5 is invisible to every header.**
>
> | limit | capacity | refill | window | reported in |
> |---|---|---|---|---|
> | tokens per minute | 8,000 | 133.3 / second | 60.0s | headers |
> | requests per day | 1,000 | 1 per 86.4s | 86,400s | headers |
> | **tokens per day** | **200,000** | — | — | **only the 429 body** |
>
> **This entry's first version got the conclusion wrong, and the error is
> kept here rather than tidied away.** Having measured the two header limits,
> it concluded that the project's 200,000-a-day figure was not something
> the provider reports and, on that evidence, not what it enforces; and that
> Iteration 5's closing arithmetic had been measuring a quantity that did not
> bind us. That is absence of a header read as absence of a limit — the exact
> reasoning this project's measure-don't-assume rule exists to prevent, made
> in the middle of a task about not assuming.
>
> The refusal that settled it arrived with the per-minute bucket reading a
> full **8,000/8,000** and a body reading:
>
> ```
> on tokens per day (TPD): Limit 200000, Used 199301, Requested 1279
> ```
>
> **So Iteration 5's closing arithmetic was right.** Its two refused
> multi-pass runs ended at roughly 192,000 and would have needed 237,000
> against a real 200,000 ceiling. The decisions taken on that basis — stop,
> do not migrate providers, do not stretch across days — were taken on a
> correct reading, and this task briefly argued otherwise.
>
> **What the TPM finding is still worth.** 8,000 a minute is real and does
> bind a sustained run: an eval call costs about 1,214 tokens, so the bucket
> covers roughly six back-to-back calls, and an unpaced pass empties it in
> ten seconds. It is a second constraint, not a replacement for the first.
>
> **The two header limits' windows are derived, never assumed:**
>
> ```
> refill = (limit - remaining) / seconds_until_reset
> window = limit / refill
> ```
>
> which returns 60.0 and 86,400 to four significant figures on every sample.
> A constant would have been quicker and would stop being true the day a tier
> changes; this keeps working and says so in the terminal on every run.
>
> **Pacing helps the per-minute limit and cannot help the daily one.** A
> daily allowance does not refill on any timescale a run can wait for, and
> Groq answers an exhausted one with `retry-after` values of 85 and 251
> seconds. An early version of the pacer slept through those and turned a
> 30-question run into an eighteen-minute crawl that had produced nothing;
> `MAX_RETRY_WAIT_SECONDS` now caps the wait and lets the refusal through,
> because failing in a minute beats failing in two hours when AC18 will
> refuse to record either.
>
> **Shipped:** `api/llm/rate_limits.py` (snapshot, derivation, duration
> parsing), `last_rate_limit` on `GroqProvider` following `last_usage`'s
> best-effort pattern, `api/llm/pacing.py`, and a pre-flight line in the
> runner. `complete(system, user) -> str` is unchanged, and the deployed API
> does not pace — sleeping inside a user's request would trade a rare refusal
> for a guaranteed delay, so only the benchmark wraps its provider.
>
> **A regression this uncovered.** `tests/test_llm_live.py` skipped
> rate-limited runs by checking `category == "provider_error"` and
> substring-matching the message. T5 split `rate_limited` into its own
> category, which killed the guard silently — for two iterations a
> rate-limited live run failed red as *"prompt injection produced executable
> DDL"*, the exact security-shaped-message-for-a-billing-condition its own
> docstring warned about. Nothing noticed because the tier had never been
> under enough pressure to rate-limit the suite. Now checked by constant,
> with the substring kept only as a fallback.
>
> **Originally pulled forward 2026-09-04 to be the next task worked, ahead of
> Iteration
> 6.** Iteration 5 closed with two multi-pass held-out runs refused for rate
> limits at 120,000 and 165,000 of a 200,000-token day — a fifth to a third of
> the budget still unspent, with the failures appearing inside sustained runs
> and worsening as runs lengthened. That is a burst ceiling, not daily
> exhaustion, and **every guard the project owns polices a daily quantity**, so
> none of them can see it. Until the headers are read, the project cannot say
> which limit binds it, and no infrastructure decision that depends on the
> answer should be taken. B-4 is the immediate case in point.
>
> **Originally filed against Iteration 7, and the discrepancy is deliberate.**
> The
> instruction was to file this in the Iteration 6 backlog, but §7's map assigns
> Iteration 6 to *Frontend* and Iteration 7 to *Hardening — history, feedback,
> latency and cost logging, rate limiting, caching*, which names this item almost
> literally. Recorded against 7 so the charter stays self-consistent; move it if
> the intent was to pull it forward.

### B-5 — AC8's guards are denominated in a quantity nothing enforces

Opened by B-1's measurements, and narrower than it first appeared. The daily
token ceiling the guards are denominated in **is real and is enforced**, so
they are not guarding a phantom; the gap is that they are the *only* thing
guarded. Three limits apply and AC8 knows about one:

| limit | guarded today |
|---|---|
| 200,000 tokens per day | yes — `--token-budget`, `--max-projection` |
| 8,000 tokens per minute | no — mitigated by pacing, not guarded |
| 1,000 requests per day | **no, not at all** |

Two further problems with the daily-token guard as written. It has no way to
know what the *account* has already spent today, only what this invocation
will spend, so `--token-budget 30000` on a run starting at 199,000 used is
satisfied and then refused on its first call. And the request limit is
unguarded despite being the cheaper one to exhaust: a three-pass dev run is
90 requests, and nothing anywhere counts them.

The pre-flight question worth asking is therefore not *does this run fit in
its own budget* but *does it fit in what the account has left, across all
three limits, at the rate it intends to spend it*. Deliberately **not**
folded into B-1: it touches both budget guards, their tests, the CLI surface
and two specs, and B-1 was filed as telemetry.

> **VERIFIED LIVE 2026-09-08, and the run found three defects.**
>
> A 30-question dev run on a fresh quota: **100.0%, 30/30, zero rate
> limits**, 35,083 tokens over 30 calls, with the pacer taking **20 waits
> totalling 135 seconds**. A run of that size had been rate-limited on every
> previous attempt, so pacing is doing what it was built for.
>
> **The ledger's token figure is Groq's own.** It is the sum of the per-call
> `usage` the provider reports, so there is nothing to reconcile: 35,083
> billed, 35,083 recorded.
>
> **The request figure was wrong, and the run is what showed it.** The
> ledger recorded 30 against Groq's own count of 32. The two missing calls
> were the runner's *own* pre-flight probes -- real spend, charged to the
> day, and structurally invisible to a ledger that summed `EvalReport`s,
> because a probe produces no report. Counting moved to `PacedProvider`,
> which sees every call by construction. Two further defects fell out of the
> same reading: the probe ran **twice** per invocation, once in the
> projection block and once in the quota block, spending a request to learn
> something it already knew; and the wall-clock estimate read *at least 11
> minutes* from the worst-case projection when the real run took four, since
> a worst case is the right basis for a refusal threshold and the wrong one
> for a duration someone plans around.
>
> **The guard was then tripped on purpose, which is the part that matters.**
> With 35,083 already spent, a `--daily-token-limit 100000` is a ceiling the
> run's own 85,320-token projection passes comfortably -- and the day's
> 120,403 does not. Before B-5 that run would have started, burned the
> remaining allowance and produced a rate-limited result AC18 refuses to
> record. It now refuses before the first question, names all three figures,
> spends only the single probe, and leaves the ledger untouched.
>
> **One path remains unexercised live and is now tracked as B-6** rather
> than left inside a discharged entry, where it would stop being visible the
> moment this row was struck through.
>
> **Only one limit needed a ledger, and that asymmetry is the design.**
> Where the provider reports what is left, asking it beats bookkeeping:
>
> | limit | how the guard knows | outcome |
> |---|---|---|
> | 8,000 tokens per minute | live, from headers | advisory note, never a refusal |
> | 1,000 requests per day | live, from headers | refuses before the first question |
> | 200,000 tokens per day | `evals/ledger.py`, corrected by any 429 | refuses before the first question |
>
> The minute bucket is deliberately **not** a refusal. Pacing owns that
> limit, and blocking on it would refuse every run larger than 8,000 tokens,
> which is every run worth making. It reports the projected wall clock
> instead. A mutation that turned the note into a refusal was caught by five
> tests, two of them end to end.
>
> **The ledger is a floor, not the truth.** It counts what this project
> spent through the eval runner; the deployed API, another checkout or a
> colleague sharing the key are invisible to it. Under-counting fails the
> safe way round -- the guard approves a run the provider then refuses,
> which is today's behaviour and no worse. A 429 is authoritative and
> overwrites the estimate with the provider's own `Used` figure, so a
> refusal is not only a failure but a free correction.
>
> Keyed by UTC date, because that is when the limit resets and the machine's
> local date is not necessarily it. Iteration 5 nearly mis-planned a day on
> exactly that gap: local time was already the 4th while UTC was the 3rd.
>
> **A test wrote real state, and that is why the fix is structural.** The
> first full run after B-5 landed put 6,800 tokens and 40 requests of fake
> spend into the real ledger, from two tests elsewhere that drive `main()`
> end to end and had no reason to know a ledger existed. Gitignored, so it
> would never have appeared in review, and read at the next real run's
> pre-flight, where it would have moved the daily guard by the size of a
> small benchmark. This is T5's `EVALS_PATH` trap in a second place, and
> per-test discipline is what failed there too, so isolation is now an
> autouse fixture applied to every test whether it asks or not.

### B-6 — 429 to ledger reconciliation, never seen against the live API

`ledger.reconcile()` replaces the local estimate with the provider's own
`Used` figure, parsed from a 429 body. It is the mechanism that turns the
ledger from a floor into the truth, and the only one that can correct for
spend this project cannot see -- the deployed API, another checkout, a
colleague sharing the key.

**Covered by fakes against a real captured 429 payload**, including the
exact body measured on 2026-09-04 (`on tokens per day (TPD): Limit 200000,
Used 199301`). What has never happened is the whole path running end to end
against Groq: refusal, parse, overwrite, and the next run's pre-flight
reading the corrected figure.

**Accepted as debt on 2026-09-08, deliberately.** Reaching it requires the
account to be genuinely at its daily ceiling, so exercising it on purpose
means burning roughly 165,000 tokens to reach a state worth reaching --
spending most of a day's allowance to watch an error handler work. The
cheap way to close it is opportunistic: the next time a run is refused for
TPD in the ordinary course of work, check that the ledger was corrected and
that the following run's pre-flight reports `provider-reconciled`.

Filed here rather than left inside B-5's entry because a discharged row is
read as finished, and an unverified edge case buried in one stops being
visible the moment the row is struck through.

### B-2 — AC13's glossary-off control

Specified in [`008-prompt-tuning.md`](008-prompt-tuning.md) at AC13. **Iteration
5 closed without it**, which is a knowing exception rather than an oversight:
the 178-token glossary ships on every call and nothing measures whether it pays
for itself. B-3 was discharged at the same close — `EVALS.md` now carries a
held-out 100.0%.

> **MEASURED AND DISCHARGED 2026-09-08. The glossary pays for itself, and the
> effect is confined entirely to the tier it was built for.**
>
> Three passes, `--split dev`, `--rendering ddl`, `openai/gpt-oss-120b`, one
> variable between the arms. Prompt `0d280c367c5e`, schema `f289a58e7ef7`,
> split `ec65d5ba81d6`, dataset v3.
>
> | arm | pass | overall | easy | medium | hard | **expert** | failures | billed |
> |---|---|---|---|---|---|---|---|---|
> | glossary on | 1 | 96.7% | 8/8 | 9/10 | 6/6 | **6/6** | 1 `no_sql_returned` (`medium`) | 40,502 |
> | glossary off | 1 | 93.3% | 8/8 | 10/10 | 6/6 | **4/6** | 2 `wrong_result` (`expert`) | 34,950 |
> | glossary off | 2 | 90.0% | 8/8 | 10/10 | 6/6 | **3/6** | 3 `wrong_result` (`expert`) | 34,901 |
>
> **The headline numbers prove nothing and are not the evidence.** A spread of
> 96.7 / 93.3 / 90.0 sits inside the noise this project has already measured —
> eight passes of one Iteration 5 configuration produced 0 to 2 wrong answers
> each. Read the tiers instead.
>
> **Every non-`expert` question is correct in both control passes — 24/24,
> twice — and all five control failures are `wrong_result` in `expert`.** That
> is the mechanism AC15 and AC16 predicted, observed: an `expert` question is
> hard because of *interpretation*, so removing the definitions produces a
> confident wrong answer rather than a broken one. Pooled, glossary-off
> `expert` is **7/12**; glossary-on `expert` is 6/6 here, and 6/6 again in T7's
> dev run and B-5's 30/30 dev run — though **both of those vary a second
> variable**, T7's being `compact` and B-5's rendering never having been
> written down. The single-variable comparison is the `ddl` pair above.
>
> **The cost, counted rather than recalled: 179 tokens a call** — 1,104 against
> 925 for the assembled system prompt, `o200k_base`. That is **not** a
> disagreement with the 178 recorded in `glossary.py` and quoted above: the
> block still counts 178 standalone, re-measured today, and the two figures are
> different quantities — 178 is the block on its own, 179 is what it adds to
> the prompt it is concatenated into, and the extra token is the join. The
> in-situ figure is the one a cost decision wants. Over a 30-question pass that
> is 5,370 tokens. The arms' billed gap was 5,552, and
> **that number is not attributed to the glossary**: the treatment run's call
> count was not recorded and its `no_sql_returned` may have spent extra calls.
>
> **Two limits on this conclusion, stated rather than buried.** The tier holds
> six questions, and two passes over the same six are not twelve independent
> trials, so 7/12 must not be read as an n of 12. And **neither control pass
> recorded which questions failed**, so *the same two questions failing every
> time plus one flake* and *the glossary lifting the tier broadly* both fit this
> data. Pass 2 was run without `--verbose`, which costs nothing and would have
> separated them. That specific question is now **B-7** rather than a caveat
> inside a discharged row.
>
> **Nothing was appended to `EVALS.md`, deliberately.** These are dev-split
> tuning runs, and both earlier arms went unrecorded; recording only the third
> would have made the file's account of this experiment less honest, not more.
> The measurement lives here and in `HANDOFF.md` §8.
>
> **A projection error is what had made this look unaffordable**, and it is
> kept here because it is the same shape of mistake as B-1's. The pre-flight
> worst case had been quoted once, as 102,240, as though it described the
> experiment. It describes *one arm*: the projection scales with the prompt, so
> the 179-token-lighter control arm projects **86,130**, and against a ledger of
> 110,763 that is 196,893 — inside the 200,000 ceiling. Only the treatment arm
> was ever refused. The control pass therefore ran with **the daily guard
> untouched at its measured 200,000**; `--max-projection 90000` raised only that
> run's own pre-flight ceiling, which is what lets a `--token-budget 50000`
> breaker exist alongside it. **A projection that varies with the configuration
> must be recomputed per arm.**
>
> **The ledger confirmed itself again.** The day moved 110,763 → 145,778, a
> delta of 35,015 against 34,901 billed: the 114-token difference and the 31st
> request are the single pre-flight probe, which is exactly what B-5 moved the
> counting to `PacedProvider` to capture.

### B-7 — Which `expert` questions the glossary actually rescues

Opened by B-2's discharge, and deliberately narrower than the row it descends
from. B-2 answered AC13: the glossary is worth its 179 tokens, and the whole of
its effect sits in the `expert` tier. What B-2 cannot say is **which questions**.

Five `wrong_result` failures across two glossary-off passes, and **no run
recorded their ids**. Two readings fit that equally well:

1. Two or three `expert` questions are genuinely glossary-dependent and fail
   every time, with the pass-to-pass difference being ordinary noise.
2. The glossary lifts the tier broadly, and a different subset fails each pass.

The distinction matters beyond bookkeeping. Under (1) the glossary is doing a
narrow, nameable job that a shorter block might do as well, and the tier's
`expert` label is carried by a couple of questions. Under (2) it is doing what
AC16 describes — making a class of question answerable — and shortening it is a
regression waiting to happen. **`008` AC16's test is that a competent analyst
produces one answer given the glossary and cannot without it**; naming the
questions is what would let that be checked case by case rather than inferred
from a count.

**The cheap fix is a flag, not an experiment.** A glossary-off dev pass with
`--verbose` prints every failing case with its SQL, at no token cost over a
plain pass — the omission on B-2's pass 2 is the only reason this is open.
Roughly 35,000 tokens on a day with room, and the answer is the terminal
output; nothing new needs building.

**Do not run it against a ledger that cannot hold it.** The worst-case
projection for a glossary-off dev pass is 86,130, which is the figure the daily
guard checks — not the ~35,000 such a run actually bills.

### B-4 — Alternative LLM provider, deferred as its own milestone

Raised at Iteration 5's close, when the free tier's rate limiting made a
5x-larger daily quota elsewhere look attractive. **Deferred, for two reasons
that are worth keeping written down**, because they will look weaker later than
they do now:

1. **It probably solves the wrong problem.** The evidence points at a burst
   ceiling, not a daily one (B-1). A larger daily bucket does not widen a
   per-minute pipe, and the migration would reproduce the same failure
   somewhere new.
2. **It retires three iterations of comparability.** Every `EVALS.md` entry
   names its model because a number is only comparable to another taken the
   same way, and Iteration 4 already records what a model switch costs. A swap
   retires the Iteration 3 baseline, Iteration 4's figures, T7's A/B and
   Iteration 5's held-out number in one move.

**The swap itself is cheap; the re-baselining is not.** §6's swappable-provider
commitment means a new provider is one module in `api/llm/` plus a branch in
the factory, and the Iteration 4 decision to use a text action protocol rather
than vendor tool schemas means nothing above `api/llm/` moves. What is not
cheap: re-running the Iteration 3 baseline and the T7 rendering A/B to
re-establish comparability, and re-measuring the prompt-size pins under a
different tokenizer.

Two specifics for whoever picks this up. `test_ac2_no_vendor_sdk_outside_the_llm_package`
scans only for `groq` imports, so **adding a provider without widening it
silently narrows the swappability guarantee to one vendor**. And the prompt-size
pins are `o200k_base` figures: T1 measured `o200k_base` against `cl100k_base`
as a control and they agree within 6 tokens on a 944-token prompt, so a
different real tokenizer moves the pins by single digits rather than
invalidating the method.

---

## 9. Open questions

Tracked here rather than assumed. Each is resolved by the spec of the iteration
that first depends on it.

- **Q1** — Which LLM provider is the default at Iteration 2: Groq (Llama 3.3 70B)
  or Gemini? The interface is swappable either way, but one of them is the default
  used in CI. *Needed by Iteration 2.*
- **Q2** — Does the metadata store live in the same Postgres instance as the
  target database (separate database, or separate schema), or in a second
  container? *Needed by Iteration 4.*
- **Q3** — What is the maximum retry count `N` in the agent loop? The source spec
  suggests roughly 3. *Needed by Iteration 4.*
- **Q4** — What counts as a "correct" answer in the eval scorer: exact result-set
  match, order-insensitive set match, or numeric tolerance for aggregates? This
  determines what the headline accuracy number actually means. *Needed by
  Iteration 3.*
### Resolved

- **Q5** — *Is `statement_timeout` pinned to the role, set per-session by the API,
  or both?* **Resolved 2026-08-21: pinned to the role.** Implemented in
  `db/init/02_readonly_role.sh` as `ALTER ROLE querypilot_ro SET statement_timeout`,
  default `10s`. Verified at Iteration 0: `SELECT pg_sleep(15)` is cancelled after
  ~10s while the superuser session is unaffected. See the Gate 3 caveat in §4 —
  this is a session default, not a ceiling, and it leans on Gate 2.
