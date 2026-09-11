# 012 — Iteration 9: The board

Status: **DRAFT — presented 2026-09-11**, §7 open · Created: 2026-09-11

Charter §6's iteration map ends at **8**. This iteration is not on it, and that
is the first thing the charter has to record rather than quietly outgrow: the
map was written as a route to a shipped system, the system shipped at Iteration
8, and what remains is a board of six items nobody planned an iteration around.

Three of them are in scope, chosen because none is blocked on a decision outside
this repository: **B-14** (does the schema cache still earn its weight?),
**B-13** (an intermittent gold-query failure), and **B-6** (429-to-ledger
reconciliation). The other three stay where they are — B-4 is its own milestone
by the charter's own ruling, and B-11 and B-12 are deferred on grounds that have
not changed.

**Every item here is a question the project filed instead of answering.** That
is the theme, and it is also the risk: a maintenance iteration is the easiest
place to do work because it is available rather than because it is warranted.
Two of the three measurements below argue for *less* code than exists today, and
one argues that a debt is smaller than its entry claims.

---

## 1. What this iteration is for

Iteration 8 closed by adding two items to the board as fast as it discharged
two, and it said so at the time. B-14 and B-13 were both opened by tests that
noticed something and declined to conclude — which is the behaviour the project
wants, and which leaves a balance to settle.

The three in scope are unalike in shape:

- **B-14 is a decision about existing code.** The schema cache was justified by
  a measurement, B-10 moved that measurement by a factor of five, and the
  module's own test failed to say so. Nothing is broken; the question is whether
  718 lines are still buying anything.
- **B-13 is a decision about evidence.** Six hypotheses were eliminated and the
  seventh is unprovable on this machine. What has never been available is a
  single un-truncated occurrence, and §2.4 shows why: the message has been there
  all along, in a file nobody was reading.
- **B-6 is a decision about what a guard can honestly claim.** It was filed as
  one gap — an untested error path — and §2.5 and §2.6 find two more, both
  closable without ever provoking a refusal.

**This iteration changes no prompt, no dataset and no scorer.** If a number in
`EVALS.md` moves, something is wrong. That was Iteration 8's rule and it is
inherited unchanged, because two of the three tasks here touch code that sits
directly under the prompt.

---

## 2. Measurements

Taken 2026-09-11 against the running stack, in the `api` container unless said
otherwise. No estimates. `get_schema()` figures are medians of 15 calls after a
warm-up; request figures are read from `/ask`'s own `total_ms` and `provider_ms`.

### 2.1 The schema cache now saves 13ms of a request that takes 1,431 (B-14)

The arithmetic this module was built on, re-measured today:

| | round trips | median | when the cache was built |
|---|---|---|---|
| `get_schema()` | **9** | **19.98ms** | 52 / 99ms |
| the catalog probe | 3 | 5.51ms | 3 / 5.3ms |
| `cached_schema()`, warm | 3 | 5.32ms | — |

The margin is **3.8:1**, against the **18:1** that justified building it.

But the module in isolation is what Iteration 7 T6 measured, and it is the wrong
frame — it is the frame that made this look settled. The question B-14 actually
asks is what a *request* costs, so the whole agent path was measured with a stub
provider, which spends no tokens and is deterministic:

| `answer()` | round trips | median |
|---|---|---|
| with the schema cache, as shipped | **6** | **8.35ms** |
| with `cached_schema` replaced by `get_schema` | 12 | 21.69ms |
| **the cache's saving** | **6** | **13.34ms** |

And what that 13ms is a fraction of, measured through HTTP on this stack today:

| | `total_ms` | `provider_ms` |
|---|---|---|
| cold miss (first request after boot) | 5,901 | 3,003 |
| warm miss | 3,088 | 1,378 |
| warm miss | 1,431 | 1,251 |
| answer-cache hit | 8, then **6** | 0 |

**On the path a user waits on, the schema cache saves between 0.4% and 0.9%.**

There is one path where it is not marginal, and it is the opposite of the one it
was built for. `/ask` reads the schema **twice** on a miss — once in
`api/agent/fingerprints.py` to build the answer-cache key, and once inside
`answer()` to build the prompt — and **once** on a hit, for the key alone. So on
an answer-cache hit the probe's 5.32ms is essentially the entire 6ms request.

**The cache did not remove the second read; it made each read cheap.**
`api/main.py::_deployed_fingerprints`'s own docstring says T6 *"collapses both
reads to one lookup"*, and that is not what happens: both reads still occur and
each pays the 3-trip probe, so a miss is **9 round trips** — 3, 3, and 3 for the
generated `SELECT`. Uncached and unchanged it would be 21. This matters because
reading the schema once is a saving available **independently of Q-A**, worth 3
round trips with the cache and 9 without, and it is the kind of duplication a
per-module round-trip test cannot see.

Substituting the two measured schema figures into that 6ms puts an uncached hit
at about **21ms — 3.5× slower, and still 21ms**. That number is arithmetic over
measurements rather than a measurement, which is why **AC4 requires it to be
measured before it is relied on**; it is here to size the decision, not to
settle it.

The probe hashes **91 signature rows** against Gate 3's 1,000-row cap, so the
truncation blindness the module documents is real and is not reached here.

### 2.2 What the cache costs to keep

| | |
|---|---|
| `api/db/schema_cache.py` | 200 lines |
| `tests/test_schema_cache.py` | 518 lines, **22 tests** |
| an autouse isolator in `conftest.py` | 1 of 6 |
| a structural test elsewhere | `test_cache.py::test_the_schema_cache_is_shared_with_the_eval_runner_deliberately` |

That isolator is the one `HANDOFF.md` §6 singles out: **the only one that can
hold something *false* rather than merely stale**, because a test that
monkeypatches `get_schema` leaves a hand-built `Schema` behind for the next test
to build a prompt from.

The correctness argument this module carries — a fingerprint that must mean
*unverifiable* rather than *unchanged*, a lock deliberately not held across the
introspection, a rejection of TTLs as not-invalidation — is the most intricate
in the codebase. It is currently defending 13.34ms.

### 2.3 Concurrency does not rescue it

Six round trips saved per request could still matter if requests overlapped, so
that was measured rather than assumed, in the container, against the shared pool:

| threads | reads | cached | uncached | ratio |
|---|---|---|---|---|
| 1 | 6 | 33.5ms | 119.1ms | 3.6× |
| 4 | 24 | 120.7ms | 346.7ms | 2.9× |
| 8 | 48 | 332.6ms | 972.7ms | 2.9× |
| 16 | 96 | 696.5ms | 1,933.4ms | 2.8× |

The ratio is flat and the per-read saving stays at **9–14ms** throughout. The
pool (size 5) never saturates: `Current Checked out connections: 0` at rest
after the 16-thread burst.

**And the ceiling above it is the provider, not the database.** `010` §2.5
measured about **seven questions per minute** before the 8,000-token minute
bucket drives the provider into its slow mode. Seven requests a minute is 63
catalog round trips a minute uncached. There is no load at which this matters.

### 2.4 B-13 has not recurred, and its evidence was never truncated where it counts

**CI: nine green runs, no reproduction.** Thirteen runs exist; four are red and
all four are accounted for — two deliberate mutations of the empty-run guard and
two real defects CI found on its first days. None is the gold-query pair.

**Locally: 20 full runs today, no reproduction.** Live provider tests excluded
exactly as CI excludes them, so this cost no tokens. Counted from the 20 junit
reports rather than from the terminal, which is the point of §2.4:

```
20 reports · 24,340 tests · 0 failures · 0 errors · 0 skipped
1,217 passed per run · 54–68s per run
```

**Zero skipped matters as much as zero failed**: it says the database was
reachable for all 24,340, so these are twenty real observations and not the
green-and-empty run `011` §2.1 was written about.

The entry says *"what to do when it next happens: read the assertion message"*,
and records that both observed failures were seen through `-q` and a tailed
capture. That is a solved problem and nobody noticed it was solved. Forcing a
failure with the same message shape:

```
terminal, -q:   FAILED tests/...::test_pretend_gold_failure - AssertionError: refer...
```

```xml
junit XML:  <failure message="AssertionError: reference queries must run:
  easy-003: connection_error: Could not connect to the database.
  ...
```

**The junit XML carries the complete message, un-truncated, in both the
`message` attribute and the element body, regardless of `-q`.** CI already
passes `--junitxml` and already uploads the report on failure, so the single
occurrence B-13 is waiting for would be fully diagnosable *if it happened in
CI*. It is the **local** run that throws the evidence away, and the local run is
where both observations happened.

One asymmetry in the pair worth recording: `test_every_gold_query_executes`
interpolates id, category and error; `test_every_gold_query_returns_at_least_one_row`
interpolates **ids only**. They fail together so the category is always
available from the first — but only because they fail together.

For the record, the environmental theory's constant: `CONNECT_TIMEOUT_SECONDS
= 5` in `api/db/engine.py`.

### 2.5 There are two spend records, and neither can see the other (B-6)

| | today | all time | knows about |
|---|---|---|---|
| `.querypilot/spend.json` | **0** — last entry is **2026-09-09** | 91,396 on that day | eval runs only |
| SQLite `ask.tokens` | **3,448** | **23,131** | API requests only |

The 3,448 is this session's three measurement questions. A pre-flight run right
now would read **0 tokens spent today** and project against a 200,000 ceiling
that is really 196,552.

`HANDOFF.md` calls the ledger *"a floor"* and names the API as one of the things
it cannot see. This is that sentence with a number on it.

**The API cannot be made to write the ledger without changing what ships.**
Measured rather than assumed: the container has no `.querypilot` at all, and
`evals/` is not in the image — the build context is `./api` with `COPY . ./api/`,
so `evals.ledger` is not importable there. Closing this from the API's side
means a new bind mount *and* shipping the benchmark harness into the runtime
image.

**And the gap is smaller than the error already in the guard.** The pre-flight
projection assumes three provider calls per question where the loop uses one, so
it runs about **3× over reality** and has already refused runs that would have
fit (`HANDOFF.md` §4). A 3,448-token under-report sits inside a ~60,000-token
over-estimate.

### 2.6 A mid-run 429 names the provider's own figure, and it is thrown away

`ledger.reconcile()` has exactly one caller: `evals/run_evals.py:1325`, in the
**pre-flight**, fed by the rate-limit probe. Nothing else in the repository
calls it.

A refusal that arrives *during* a run takes a different route and is not
reconciled:

| where | what it has | what it does |
|---|---|---|
| pre-flight probe | `limit_from_message(limits_line)` | **reconciles** |
| mid-run, per question | `RateLimitError` → `AgentResult.error` → `CaseResult.error`, the full body | counts it, refuses to record the run (AC18), **discards the figure** |

The body is preserved verbatim the whole way — `error=str(exc)` at
`orchestrator.py:429` and `:450`, straight into `CaseResult.error`. So the
provider's authoritative `Used` figure is sitting in a field the runner already
reads to count rate-limited cases, and `limit_from_message` is already imported
in that module.

**This half of B-6 needs no live 429 to build or to test.** The captured real
payload — `on tokens per day (TPD): Limit 200000, Used 199301` — is already a
fixture in `tests/test_rate_limit_telemetry.py`.

**The other half still costs a day.** Provoking a genuine TPD refusal means
reaching the ceiling: roughly **196,552 tokens** from where the account stands
today, to watch an error handler run.

---

## 3. Acceptance criteria

### The map, and the board

- **AC1** — Charter §6 gains a row for this iteration rather than having its map
  silently outgrown, and §8's board shows each of B-6, B-13 and B-14 either
  struck through or carrying a new, dated reason. An item that survives the
  iteration says why in its own entry.

### B-14 — the schema cache, decided on a request rather than a module

- **AC2** — B-14 leaves the board. The cache is retired or kept, and the entry
  records **the number the decision was made on** — §2.1's 13.34ms of a
  1,431ms request, not §2.1's 3.8:1 ratio, because the ratio is the frame that
  made this look settled.
- **AC3** — Whichever way it goes, the round-trip cost of a request is asserted
  **end to end**, not per module. `HANDOFF.md` §6 already records that nineteen
  green tests of `cached_schema()` did not notice the request path had stopped
  calling it; the same shape must not be able to hide the reverse.
- **AC4** — If it is retired, the latency change is **measured and stated**, not
  predicted — including the answer-cache hit, which §2.1 measures at 6ms and
  which is the one path that gets meaningfully slower.
- **AC5** — If it is retired, nothing user-visible changes except latency: the
  rendered schema stays byte-identical and the recorded prompt fingerprints
  `0d280c367c5e`, `91036a089282` and `f971d8787f0c` still reproduce. **This is a
  gate, as it was at Iteration 8 T5** — if they do not, the task stops rather
  than re-baselining.

### B-13 — evidence, not a retry

- **AC6** — A local full-suite run preserves the failing assertion message
  without needing a re-run, since the re-run is what destroys the evidence.
- **AC7** — **No retry is added** that could let a genuine gold-query defect
  pass. If Q-E decides otherwise, the retry is scoped to one category and a test
  proves a real broken reference query still fails.
- **AC8** — B-13 leaves the board or carries a **stated observation budget**
  with a date and a count, so that "still open" cannot mean "still unexamined"
  for another iteration.

### B-6 — what the guard can honestly claim

- **AC9** — A TPD refusal arriving **mid-run** reconciles the ledger, proven
  against the captured real payload.
- **AC10** — The ledger's blind spots are **named with their measured size**
  where a reader will meet them — §2.5's numbers, not the word "floor".
- **AC11** — No test and no code path in this iteration spends a token to prove
  any of the above.

### Unchanged

- **AC12** — No prompt, dataset or scorer change. `EVALS.md` does not move, and
  it stays append-only.

---

## 4. Non-goals

- **B-4, B-11, B-12.** Out by the rulings that put them there, none of which has
  changed. B-4 in particular is a milestone, and folding a provider swap into a
  maintenance iteration is how a re-baselining gets done in passing.
- **Provoking a real 429.** §2.6 prices it at ~196,552 tokens — a day's entire
  allowance to watch an error handler. B-6's live half stays opportunistic.
- **Authentication.** Still absent, still its own iteration, named here for the
  same reason `011` §4 named it.
- **Any accuracy work.** AC12.
- **A TTL on the schema probe.** The module's own docstring rejects it as
  trusting rather than invalidating, and that argument is untouched by §2.1.

---

## 5. Contracts this iteration must not break

- Everything reaching the database goes through `execute_sql()`. The catalog
  probe and the three introspection queries are inside the gate today, and any
  rearrangement keeps them there — §4 is absolute with one recorded exemption,
  and B-10 was discharged at Iteration 8 T5, last night, precisely to make that
  true. A task that deletes the cache is a task editing the module B-10 just
  rewrote.
- `complete(system, user) -> str` stays one method wide; `groq` stays imported
  by exactly one module.
- `EVALS.md` stays append-only and its fingerprints keep reproducing (AC5).
- The deployed API still does not pace, and `/ask` still answers synchronously.
- The answer cache stays unreachable from `evals/`.
- Nothing in `db/init/` changes — it needs `down -v`, which discards the
  question log with the database.
- Feedback stays collected and **not consumed** (`011` AC14). Six tests exist to
  make adding an aggregate fail, and a maintenance iteration touching the store
  is exactly where one would get added by accident.

---

## 6. Risks

- **The biggest risk is doing this work at all.** All three items are available
  rather than urgent, and two of the three measurements argue for deleting code.
  A maintenance iteration that finds work to justify itself is worse than a short
  one that closes items with a recorded "no".
- **Retiring the cache is a deletion, and deletions are where absence
  assertions rot.** `HANDOFF.md` §6 has five recorded instances of a test that
  asserts something is missing and passes for the wrong reason. The structural
  test naming `api.db.schema_cache` must be *updated*, never deleted to make a
  suite green.
- **AC3 is the criterion most likely to be met by a test that proves nothing.**
  Counting round trips inside the module is what already exists and is what
  missed the adoption gap once. The count has to be taken through the endpoint.
- **B-13 may simply not recur**, and this iteration cannot make it. The
  deliverable is that the next occurrence is legible, which is unfalsifiable
  until one happens — so AC8 asks for a stated budget rather than a fix.
- **B-6's cheap half may look like the whole thing.** Closing mid-run
  reconciliation does not close B-6; the live path is still unexercised, and the
  entry has to keep saying so or the strike-through will read as finished.

---

## 7. Open questions

- **Q-A — Does the schema cache stay?** §2.1 measures its saving at 13.34ms and
  6 round trips on a request whose measured range is 1,431–5,901ms, and §2.3
  shows no load at which that changes. §2.2 prices keeping it at 718 lines, 22
  tests, and the one isolator that can hold a false value.

  *My lean: retire it.* The module exists because introspection was 52 round
  trips and 99ms; B-10 made it 9 and 20ms, and a justification that moved by 5×
  has not survived — it has been outlived. Keeping intricate machinery because
  it is already written and still green is the shape of debt this board exists
  to prevent. The honest counter-argument is that it is *working, tested code
  that costs nothing at runtime*, and that retiring it undoes Iteration 7 T6
  deliberately rather than by neglect. I think that is coherent rather than
  contradictory — T6 cached a 99ms operation, and the operation is now 20ms —
  but it is your call, and it is the one decision here that deletes something
  that works.

- **Q-B — Does a request stop reading the schema twice?** This is **independent
  of Q-A**, which is why it is its own question. §2.1 found `/ask` reading the
  schema once for the answer-cache key and again for the prompt — 6 of a miss's
  9 round trips with the cache, 18 of 21 without. The duplication was never
  chosen: it arrived when T4 added the cache key, and `_deployed_fingerprints`'s
  docstring records the belief that T6 *"collapses both reads to one lookup"*,
  which is not what the count shows.

  *My lean: read it once, whichever way Q-A goes.* It saves 3 round trips with
  the cache and 9 without, so it also makes Q-A's deletion cheaper than the
  13.34ms headline suggests. Two things give me pause. It is a change to the
  request path in an iteration whose other tasks are deletions and
  documentation, and the schema would have to reach `answer()` as an argument —
  which either widens a signature or threads state through a module boundary
  that `_deployed_fingerprints`'s docstring shows has already been defended
  once. If that turns out to be more than a small change, I would rather file it
  than force it.

- **Q-C — Does the API's spend ever reach the ledger?** §2.5 measures the gap at
  3,448 tokens today and 23,131 all time, and finds the container unable to
  reach the ledger file or import the module that writes it.

  *My lean: no — document it precisely and sharpen what `describe()` says.*
  Two measurements push this way. Closing it from the API's side needs a bind
  mount and the benchmark harness shipped into the runtime image, which
  contradicts `010` D-1's separation of product and benchmark records for a
  partial fix — it would still miss another checkout, another machine, and the
  colleague sharing the key. And the gap is **inside the error already there**:
  a ~3,400-token under-report against a projection that over-estimates by
  roughly 60,000. The thing that actually closes it is the 429 reconciliation,
  which is Q-D. The alternative worth weighing is having the *eval runner* read
  the API's record at pre-flight over `GET /history/data` — one-way, no change
  to what ships — which I lean against only because it makes the pre-flight
  depend on a running service for a correction smaller than its own noise.

- **Q-D — Is the live half of B-6 worth forcing?** §2.6 prices it at ~196,552
  tokens from today's position.

  *My lean: no, and close the cheap half properly instead.* Reconciling from a
  mid-run refusal is buildable and testable against the captured payload at zero
  token cost, and it is strictly more useful than the pre-flight path it
  complements — a run refused at question 17 is the likely way this project
  meets a TPD limit in ordinary work. B-6 then stays open for its live leg, with
  its entry rewritten to say which half is closed. Spending a day's allowance to
  watch an error handler is the trade the entry already declined, and nothing in
  §2 argues for reversing it.

- **Q-E — What closes B-13?** §2.4 finds the diagnostic already complete in CI
  and discarded locally, nine green CI runs, twenty green local runs today, and
  no reproduction.

  *My lean: make the local run keep its evidence, add no retry, and give B-13 a
  stated observation budget.* Concretely: a documented local invocation that
  writes the junit report, and the second test's message brought up to the
  first's so the pair is not dependent on failing together. Not a retry — the
  entry's own reasoning that *a retry is a place a real failure can hide* is
  still right, and twenty-nine clean observations since it was filed is evidence
  against urgency, not for a workaround. The budget is the part I would ask you
  to set: a count of CI runs or a date after which B-13 is closed as
  environmental rather than carried indefinitely.
