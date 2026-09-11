# 010 — Iteration 7: Hardening

Status: **delivered 2026-09-10** · approved 2026-09-09, §7 resolved · Created: 2026-09-09

> **AC6 (feedback) was struck and moved to Iteration 8** at T1, and the
> charter is amended to say so. Every other criterion is met. Two findings
> made along the way are filed rather than fixed: **B-9** (AC14's live
> injection test asserts that the model refuses, which is
> nondeterministic) and **B-10** (`get_schema()` has reached the database
> around Gate 2 since Iteration 1).

> **Resolved questions.** Q-A persisted state lives in **SQLite in a volume**,
> so the API gains no Postgres write credential at all · Q-B **guard the
> upstream quota, single-flight inbound**, no inbound rate limiter · Q-C
> **disclose the slow mode, do not pace** · Q-D cache key is **exact question
> text + schema fingerprint + prompt fingerprint**, in memory, verified by token
> spend · Q-E **feedback defers to Iteration 8**, recorded as a charter
> amendment rather than carried silently.

Inherits every constraint in [`000-project.md`](000-project.md). Where this
document and the charter disagree, the charter wins.

> **Done when** (charter §6): *history, feedback, latency and cost logging, rate
> limiting, caching.*

---

## 1. Intent

Iteration 6 made the agent reachable. This one makes it **survivable and
observable**: the system should record what it did, degrade honestly when the
provider throttles it, and stop paying for the same answer twice.

The charter's Iteration 7 line reads like five separate features. The
measurements below say it is really **two problems wearing five names**:

1. **Nothing is remembered.** No history, no feedback, no cost log — and the API
   holds no credential that could write one.
2. **The upstream quota is the binding constraint**, and it degrades silently.

---

## 2. What the measurements say

Taken 2026-09-09 against the live stack and the deployed model.

### 2.1 The API cannot persist anything, by construction

Nothing in this project writes a row. There is no metadata store, no history
table, no feedback record. More pointedly, **the API container is given exactly
one credential and it is read-only**:

```yaml
QUERYPILOT_DATABASE_URL: postgresql+psycopg://${QUERYPILOT_RO_USER}:...
```

`POSTGRES_USER` — the superuser — is passed to the `db` service and **never to
`api`**. So "add a history table" is not a schema change; it is a change to the
credential the API holds, which charter §4 constrains absolutely: *"Read-only,
always. The API only ever holds the `querypilot_ro` credential."*

**This is the central design question of the iteration** and it is Q-A, not an
implementation detail to be settled in code.

### 2.2 The model is 94% of the wall clock; the database is noise

Twelve dev questions through `answer()`, with the provider wrapped to time
`complete()` separately:

| component | measured |
|---|---|
| provider (`complete`) | **94.2%** of total, up to 98.4% |
| `get_schema()` | 143ms median |
| `execute_sql()` | 5–6ms median |

**Optimising the database would be optimising 1% of the request.** Any latency
work that is not about the model, the network, or avoiding the call entirely is
misdirected effort.

### 2.3 Latency is bimodal, and the slow mode is invisible

The same twelve questions, in order, with token counts flat between 1,047 and
1,256:

```
easy-001    1169ms      medium-001   3984ms
easy-002     786ms      medium-002   7091ms
easy-003     800ms      medium-003   9032ms
easy-008     742ms      medium-004  10393ms
```

Latency climbs **fourteen-fold** while the work stays the same size. It is not
difficulty. Twelve calls of ~1,100 tokens is ~13,500 tokens inside a minute,
against the **8,000-tokens-per-minute bucket B-1 measured**.

**Confirmed rather than inferred.** Three identical calls spaced 35 seconds
apart, letting the bucket refill:

| call | latency | minute-bucket remaining |
|---|---|---|
| 1 | 1,157ms | 3,257 |
| 2 | 1,031ms | 6,792 |
| 3 | 959ms | 6,758 |

Latency returns to baseline. **The provider does not refuse when the bucket
runs low — it slows down.** There is no 429, no header a user could be shown, and
nothing in the product currently notices.

So the honest statement of this system's latency is **two numbers, not one**:

- **~750–1,250ms** for a user asking one question at a time
- **up to ~10,400ms** under sustained load, degrading silently

**This corrects `009-frontend.md` §2.6**, which reported 1.20–2.51s at n=3 and
was quoted into resolved Q-D. That sample was taken on a cold bucket and is the
fast mode only. Q-D's conclusion — synchronous, no streaming — still holds for a
single user, but it was decided on a range that did not include the slow mode.

### 2.4 The schema is re-introspected on every question, and never changes

There is **no caching anywhere in the project** — no `lru_cache`, no memo, no
stored schema. `get_schema()` runs per question at **143ms**, and two successive
calls return byte-identical objects.

Cacheable, then — but worth being precise about the prize: 143ms of a ~1,000ms
request is **~14% of the fast mode and ~1.4% of the slow mode**, and it saves
**zero tokens**. It does not touch the constraint in §2.3.

### 2.5 A question costs about 1,100 tokens and one call

Median 1,078 tokens, range 1,047–1,256, **one provider call every time** across
all twelve. The three-call budget is a ceiling the loop does not reach on
questions it can answer.

At 200,000 tokens a day that is roughly **180 questions**, and the per-minute
bucket allows about **seven in any sixty seconds** before §2.3's slow mode
begins.

---

## 3. Acceptance criteria

### Observability

- **AC1** — Every answered question records **latency, token cost, provider call
  count, category and outcome**. Cost is the provider's billed figure where
  available, and says which instrument produced it (D-1's precedent).

  > **The number already exists and the API throws it away.** `AgentResult`
  > carries `usage` — the provider's own billed figure — and `POST /ask` does not
  > put it in the payload or anywhere else. Found while reconciling the spend
  > ledger on 2026-09-09, where a 20,370-token gap had to be counted by hand from
  > three instrumented probes because nothing recorded it. Preserving a figure
  > that is already measured is the cheapest half of this criterion.
- **AC2** — The record distinguishes **provider time from total time**, because
  §2.2 says everything else is noise and a log that hides that invites work on
  the wrong 6%.
- **AC3** — **A slow response is attributable.** When a request lands in §2.3's
  slow mode, the record carries the rate-limit headers that explain it. A
  latency log that cannot tell throttling from a hard question is a graph nobody
  can act on.

### Memory

- **AC4** — Question history is persisted and readable, including the SQL, the
  outcome and the trace.
- **AC5** — **The target database stays read-only.** Whatever Q-A decides,
  `querypilot_ro` is not widened and no generated SQL is ever executed against a
  writable connection.
- ~~**AC6** — Feedback is recorded against a specific answer, not a question
  string, so it survives the agent producing different SQL next time.~~

  > **DEFERRED to Iteration 8 (resolved Q-E), and amended into the charter
  > rather than carried quietly.** It is the only one of the five features with
  > **nothing in §2 behind it**: latency, cost, the persistence gap and the
  > schema's cacheability were all measured, and nothing was measured about what
  > feedback would be for, because there are no users to ask. `008` is the
  > precedent — AC13 rode along unmeasured, sat unmet at close, and took two
  > backlog items and three days to discharge honestly.
  >
  > The requirement itself is kept above, struck rather than deleted, because
  > the *shape* it names is the right one: feedback attaches to an **answer id**,
  > never to a question string, since the agent may answer the same question
  > differently next time.

### Not paying twice

- **AC7** — An identical question does not spend tokens twice. The cache key,
  and what invalidates it, are stated in the spec rather than discovered in the
  code (Q-D).
- **AC8** — **A cached answer is visibly cached.** Presenting stale data as fresh
  is the analytics equivalent of the accuracy claim AC13 banned from the UI.
- ~~**AC9** — The schema is introspected once and reused (§2.4), and the reuse is
  invalidated on a schema change rather than trusted forever.~~

  > **RETIRED 2026-09-11, at Iteration 9 T4 (B-14).** The schema is no longer
  > reused: `api/db/schema_cache.py` is gone, and every request introspects.
  > The text is struck rather than rewritten, for the reason charter §6's
  > deployment amendment gives — *a commitment quietly edited to match what was
  > built is the failure `EVALS.md`'s append-only rule exists to prevent.*
  >
  > **The criterion did not rot; the thing underneath it moved.** This AC was
  > written when §2.4 measured `get_schema()` at **143ms** and the cache's own
  > docstring later recorded **99ms** against a 3-round-trip probe — an 18:1
  > margin. Iteration 8 T5 discharged B-10 by replacing SQLAlchemy's `Inspector`
  > with three catalog queries through `execute_sql()`, taking introspection to
  > **9 round trips and 19.98ms**. `012-board.md` §2.1 then measured what the
  > cache was still buying a whole request: **13.34ms of one measured between
  > 1,431ms and 5,901ms**, or 0.4%–0.9%, with no load at which that changes —
  > the provider's 8,000-token minute bucket caps throughput at about seven
  > questions a minute.
  >
  > Each of 143ms, 99ms and 19.98ms was correct when taken. Three measurements
  > of one operation, and the third invalidated a design the first two justified.
  >
  > **What was given up, stated rather than glossed.** An answer-cache hit was
  > 6ms and is now 9 catalog round trips; the cold/warm distinction is gone,
  > because there is no cache to be warm. What was bought is 718 lines — a
  > 200-line module and 518 lines of tests — plus the one autouse isolator in
  > `tests/conftest.py` that could hold something *false* rather than merely
  > stale.
  >
  > **AC9's other half held and is why this was safe.** The reuse had to be
  > invalidated on a schema change; removing the reuse satisfies that trivially,
  > and the byte-identical rendering gate proved nothing else moved — the
  > `compact` and `ddl` renderings and the fingerprints `0d280c367c5e` and
  > `91036a089282` all reproduce unchanged. `012-board.md` AC5 made that a stop
  > condition, not a goal.

### Degrading honestly

- **AC10** — When the minute bucket is low, the user is **told the answer may be
  slow** rather than left watching a spinner for ten seconds.
- **AC11** — Under-quota behaviour is never a silent penalty: §2.3's slow mode
  currently has no user-visible explanation anywhere in the system.

---

## 4. Non-goals

- **Optimising the database.** It is 1% of the request (§2.2).
- **Multi-user accounts, auth, tenancy** — charter §3, unchanged.
- **A metrics backend, dashboards, or tracing infrastructure.** A log that can be
  read and queried is the bar; Grafana is not.
- **Caching that survives a restart**, unless Q-D says otherwise.
- **Making the model faster.** We do not control it. We control whether we call
  it (§2.5).
- **Streaming**, still. Q-D's reasoning holds for the fast mode, and the slow
  mode is better fixed than better animated.

---

## 5. Contracts

- **`complete(system, user) -> str` is unchanged.** Rate-limit telemetry stays a
  best-effort attribute on the concrete provider (`last_rate_limit`), per B-1.
- **`answer()` does not learn about storage.** It returns an `AgentResult`; the
  caller decides what to record, exactly as the HTTP boundary decides what to
  serialise.
- **Nothing bypasses `execute_sql()`**, including any new store that lives in
  PostgreSQL.
- **The deployed API still does not pace** (AC5 of `009`). Pacing is a benchmark
  behaviour; a user's request must not sleep.

---

## 6. Verification

- Mutation testing at every task, per the standing rule.
- **A latency assertion must not be a timing test.** Wall-clock assertions are
  flaky by construction; assert that the *fields are recorded and attributed*,
  not that a number is below a threshold.
- The cache is verified by **token spend, not by speed**: a cache hit that still
  calls the provider is the failure mode, and it is invisible to a stopwatch.
- AC5 gets a structural test: no writable DSN reachable from the request path.

---

## 7. Open questions

**Answer these before the plan is drafted.** Each carries my lean.

- **Q-A — Where does persisted state live, and what credential writes it?**
  §2.1 makes this the iteration's central question. Candidates: (1) a **second
  database or schema** with its own writable role, leaving `querypilot_ro`
  untouched on the target; (2) **SQLite** in a mounted volume, so no Postgres
  credential changes at all; (3) **append-only JSONL** files.

  *My lean: (2), SQLite.* It keeps charter §4 literally true — the API gains no
  Postgres credential of any kind — needs no compose changes beyond a volume,
  and history is queryable. (1) is the "right" answer for a real deployment and
  is what Iteration 8 would want; it also means the API process holds a writable
  handle, which is precisely the property §4 was written to prevent. Worth your
  decision rather than mine.
  > **Resolved 2026-09-09: (2), SQLite in a volume.** *"The Postgres analytics
  > warehouse remains strictly read-only, and the API gets a local, lightweight
  > operational datastore for history and caching. It completely bypasses the
  > Charter §4 conflict."* So §4 stays literally true rather than gaining a
  > second recorded exemption — the API holds one Postgres credential and it is
  > read-only, exactly as written.

- **Q-B — Does "rate limiting" mean protecting us from users, or managing our
  upstream quota?** The charter's phrasing suggests the former; §2.3 says the
  real constraint is the latter, and it is already binding at one user asking
  seven questions a minute.

  *My lean: both, but upstream first.* An inbound limiter that lets a single
  user exhaust the shared quota protects nothing. The measurement says the queue
  that matters is ours.
  > **Resolved 2026-09-09: guard upstream, single-flight inbound, and build no
  > inbound rate limiter.** *"An inbound rate limiter is useless if the upstream
  > chokes at 7 requests per minute."* Two pieces: the most recent
  > `last_rate_limit` snapshot is kept and exposed so the UI can warn *before* a
  > slow answer (AC10), and two identical in-flight questions share one provider
  > call rather than racing. The second is inbound work, but it protects the
  > upstream constraint rather than policing the user.

- **Q-C — Is the slow mode fixed, or only disclosed?** We could pace the API
  (rejected once, as AC5 of `009`), queue requests, or simply tell the user the
  system is busy and let the request run slowly.

  *My lean: disclose, do not pace.* Sleeping inside a user's request is what
  B-1 refused; a request that takes 10s because the provider is slow is honest,
  while one that takes 10s because we chose to wait is not. But AC10 means the
  UI must say so.
  > **Resolved 2026-09-09: disclose, do not pace.** *"If the upstream provider
  > silently throttles us to 10 seconds under load, we pass that reality to the
  > user interface. Artificially pacing or throwing errors for answerable
  > questions violates the product's core intent."* This keeps `009` AC5 intact:
  > the deployed API still never sleeps inside a request.

- **Q-D — What does the cache key on, and what invalidates it?** Options: exact
  question text; normalised text; question plus schema fingerprint. And: does it
  survive a restart, and does it ever expire?

  *My lean: exact text plus schema fingerprint, in-memory, no expiry within a
  process.* Chinook is static, so staleness is theoretical here — but a cache
  keyed without the schema fingerprint is one that serves yesterday's answer
  after a migration, and the fingerprint machinery already exists.
  > **Resolved 2026-09-09: exact question text + schema fingerprint + prompt
  > fingerprint**, in memory, no expiry within a process, storing the whole
  > `AgentResult`.
  >
  > Exact text rather than normalised: normalisation is a correctness bet, and a
  > wrong hit returns a confidently wrong answer with someone else's SQL
  > attached. Under-hitting is harmless; mis-hitting is not.
  >
  > **The prompt fingerprint is in the key for a benchmark reason.** *"Including
  > the schema and prompt fingerprints ensures we never poison our benchmark
  > measurements with stale cache hits from previous architectural
  > iterations."* A cache surviving a prompt change would serve pre-change
  > answers into a post-change measurement, which is the one failure this
  > project's whole record exists to prevent.
  >
  > **Verified by token spend, never by a stopwatch** — a "hit" that still calls
  > the provider is invisible to timing and obvious to a token counter.

- **Q-E — Is feedback part of this iteration or the next?** AC6 is the smallest
  of the five features and the only one with no measurement behind it — nothing
  in §2 says anything about what feedback would be for.

  *My lean: defer it to Iteration 8*, and say so in the charter rather than
  carrying it silently. `008` shows what happens when an unmeasured criterion
  rides along: AC13 sat unmet for four days and needed two backlog items to
  discharge.
  > **Resolved 2026-09-09: deferred to Iteration 8**, with a charter amendment
  > rather than a silent drop. *"Building a feedback table for a non-existent
  > user base based on zero measurements is how we end up with dead code."*
  > AC6 above is struck, and the shape it names — feedback attaches to an answer
  > id, not a question string — is preserved for whoever builds it.

---

## 8. What this unblocks

Iteration 8 ships. It cannot honestly ship a system whose latency is *"between
0.7 and 10.4 seconds depending on something the user cannot see"*, and it cannot
put evals in CI without a cost record to show what a CI run spends.
