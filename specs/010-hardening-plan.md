# 010 — Iteration 7 plan: Hardening

Status: **delivered 2026-09-10**, T1-T7 complete · approved 2026-09-09, §6 resolved · Created: 2026-09-09

> **Resolved decisions.** D-1 the eval runner does **not** write to this store ·
> D-2 a history-write failure **never** fails the request; degradation surfaces
> in `/health` · D-3 the cache stays **in memory**, not persisted · D-4 the
> SQLite file lives in a **named volume**, mirroring `pgdata`.

Implements [`010-hardening.md`](010-hardening.md), whose §7 is resolved. This
document says *how*, and surfaces the decisions the design itself raised.

---

## 1. Approach

The spec's §1 reduces five charter features to two problems. The plan follows
that shape rather than the charter's list, and builds in the order that makes
each step independently verifiable:

1. **Stop discarding what we already measure.** `AgentResult.usage` exists and
   the API drops it. Nothing else can be built on a cost record that is not
   being kept.
2. **Give the process somewhere to write** — SQLite, per Q-A, so the Postgres
   target keeps exactly one read-only credential.
3. **Stop paying twice** — the cache, verified by tokens.
4. **Tell the truth about the slow mode** — quota telemetry to the UI.

**Feedback is not in this iteration** (resolved Q-E), and the charter is amended
to say so rather than leaving Iteration 7 quietly incomplete.

---

## 2. Where state lives

### 2.1 SQLite, and why the boundary is the point

```
docker compose
├── db      postgres  ← the analytics target. querypilot_ro. READ ONLY, always.
└── api     fastapi   ← holds one Postgres credential, still read-only
             └── /data/querypilot.db   ← SQLite, operational state, writable
```

Two stores with two jobs, and the separation is the reason Q-A chose this. The
analytics warehouse is *the thing being queried by a language model*, and charter
§4 exists because of that. Operational state — what we asked, what it cost, how
long it took — has no such threat model and no reason to share the credential.

**A new compose volume**, mirroring `pgdata`: named, so `docker compose down`
preserves history and only `down -v` discards it.

`sqlite3` is in the standard library, so **no new dependency**, runtime or dev —
the same property Q-A protected in Iteration 6.

### 2.2 The schema

```sql
CREATE TABLE ask (
  id             TEXT PRIMARY KEY,      -- uuid4; what feedback will attach to
  asked_at       TEXT NOT NULL,         -- ISO 8601, UTC
  question       TEXT NOT NULL,
  ok             INTEGER NOT NULL,
  sql            TEXT NOT NULL DEFAULT '',
  category       TEXT NOT NULL DEFAULT '',
  shape          TEXT NOT NULL DEFAULT '',
  row_count      INTEGER,
  attempts_used  INTEGER NOT NULL,
  -- AC1/AC2: the two numbers, kept apart because §2.2 says one of them is 94%
  total_ms       INTEGER NOT NULL,
  provider_ms    INTEGER NOT NULL,
  -- AC1: the provider's own billed figure, plus which instrument produced it
  total_tokens   INTEGER,
  prompt_tokens  INTEGER,
  usage_measured INTEGER NOT NULL,      -- 0 = locally estimated, 1 = billed
  provider_calls INTEGER NOT NULL,
  cache_hit      INTEGER NOT NULL DEFAULT 0,
  -- AC3: what explains a slow answer
  tpm_remaining  INTEGER,
  rpd_remaining  INTEGER,
  -- fingerprints, so a row stays comparable the way an EVALS.md entry does
  model          TEXT NOT NULL DEFAULT '',
  schema_fp      TEXT NOT NULL DEFAULT '',
  prompt_fp      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE step (          -- the trace, one row per attempt
  ask_id   TEXT NOT NULL REFERENCES ask(id),
  attempt  INTEGER NOT NULL,
  action   TEXT NOT NULL,
  ok       INTEGER NOT NULL,
  category TEXT NOT NULL DEFAULT '',
  error    TEXT NOT NULL DEFAULT '',
  sql      TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (ask_id, attempt)
);
```

`id` is a uuid4 and is returned in the `/ask` payload. That is the **answer id**
struck AC6 named — feedback in Iteration 8 attaches here, not to a question
string, because the agent may answer the same question differently next time.

`usage_measured` follows D-1's standing precedent: a locally counted number and
a billed number are different quantities, and the record says which it holds.

### 2.3 Writing must never break answering

**A history write that fails must not fail the request.** The user asked a
question and got an answer; losing the log entry is our problem, not theirs. The
write is wrapped, failures are logged once, and the response is unaffected — see
D-2 for the one thing that judgment does *not* extend to.

---

## 3. The cache

Key: `sha256(question || "\\x00" || schema_fp || "\\x00" || prompt_fp)`, storing
the whole `AgentResult`. In-process dict, no expiry, not persisted (resolved
Q-D).

**Verified by token spend, not by a stopwatch** — the whole point. The test
drives two identical questions through a counting fake provider and asserts the
second one calls it **zero** times. A cache hit that still calls the provider is
invisible to timing and obvious to a counter.

**Single-flight** (resolved Q-B): two identical questions arriving concurrently
share one provider call rather than racing. A `dict[key, Future]`-shaped guard
under a lock; the second caller waits on the first's result.

`cache_hit` is recorded (§2.2) and returned in the payload, because **AC8** says
a cached answer must be visibly cached — presenting stale data as fresh is the
analytics equivalent of the accuracy claim `009` AC13 banned from the UI.

**What the cache does *not* touch:** §2.3's slow mode. A cache helps the second
identical question and nothing else. It is a cost control, not a latency fix,
and any claim otherwise should be checked against §2.5's numbers.

---

## 4. Telemetry and the slow mode

`GET /quota` returns the most recent `last_rate_limit` snapshot: tokens
remaining in the minute bucket, requests remaining in the day, and the derived
reset time. Read from the concrete provider with `getattr`, per B-1's settled
precedent — **`complete(system, user) -> str` does not change.**

The page polls it lightly and, when the minute bucket is low, says so **before**
the user asks rather than after they have waited ten seconds (AC10, AC11).

**The API still does not pace** (resolved Q-C, `009` AC5). A request that takes
ten seconds because the provider is slow is honest; one that takes ten seconds
because we chose to sleep is not.

---

## 5. Files and decomposition

| task | what | verified by |
|---|---|---|
| **T1** | Charter amendment: defer feedback to Iteration 8 (Q-E) | Read it. **Pause.** |
| **T2** | `api/store/` — SQLite schema, migrations, `record_ask()` | Round-trip test; write-failure mutation |
| **T3** | Wire `POST /ask` to record; return `id`, `usage`, `cache_hit` | Usage-survives-the-boundary mutation |
| **T4** | `api/http/cache.py` — key, lookup, single-flight | **Token-count** mutation. **Pause.** |
| **T5** | `GET /quota` + the page's warning | Structural test: no pacing, no protocol change |
| **T6** | Schema caching with fingerprint invalidation (AC9) | Introspection-call-count mutation |
| **T7** | A `history` view or CLI to read what was recorded | A human reads it. **Pause.** |

New: `api/store/__init__.py`, `api/store/schema.sql`, `api/store/history.py`,
`api/http/cache.py`, `tests/test_history_store.py`, `tests/test_cache.py`,
`tests/test_quota_endpoint.py`. Edited: `api/main.py`, `docker-compose.yml`,
`api/web/*`, `specs/000-project.md`.

---

## 6. Decisions

- **D-1 — Does the eval runner write to this store too?** It already has
  `evals/ledger.py` for spend and `EVALS.md` for results. A second writer would
  give per-question history for benchmark runs, and would also mean a benchmark
  and a user share a table.

  *My lean: no.* The ledger exists, `EVALS.md` is the benchmark record, and the
  20,370-token reconciliation on 2026-09-09 was caused by *direct `answer()`
  calls*, which this store would not have caught either. Keeping the benchmark
  and the product's operational log separate preserves the property that
  `EVALS.md` entries are the only accuracy record.
  > **Resolved 2026-09-09: no.** *"Keep the benchmark runs completely isolated
  > from the product operational store. `EVALS.md` and the existing ledger remain
  > the sole source of truth for benchmark accuracy and spend."*

- **D-2 — Is a history-write failure ever allowed to fail the request?** §2.3
  says no. But if the store is unreachable for every request, the system is
  silently amnesiac and looks healthy.

  *My lean: never fail the request; surface it in `/health` instead.* A degraded
  store is exactly what a health check is for, and `/health` already reports
  database readiness. This keeps answering and recording independent.
  > **Resolved 2026-09-09: never fail the request.** *"The primary contract of
  > `/ask` is to return an answer. If SQLite locks or the volume fills up, catch
  > the exception, drop the log, and flag the degradation in `/health`. An
  > observability failure must never cascade into user-facing downtime."*
  >
  > So the store has two failure modes and they are treated differently: a write
  > that fails is dropped silently from the user's point of view and loudly from
  > the operator's, and `/health` is where the second half happens.

- **D-3 — Does the cache survive a process restart?** Q-D said in-memory, and
  SQLite now exists, so persisting it is newly cheap.

  *My lean: keep it in memory, as resolved.* A restart-surviving cache is a
  staleness surface with no measurement behind it, and the fingerprints in the
  key protect correctness rather than lifetime. Revisit when there is a
  deployment where restarts are frequent enough to matter.
  > **Resolved 2026-09-09: in memory.** *"SQLite makes persistence cheap, but a
  > restart-surviving cache introduces a massive staleness vector that we have no
  > telemetry to monitor."* The absence of telemetry is the argument: this
  > iteration is where observability gets built, and a cache older than the
  > process would be the one thing in the system nothing could account for.

- **D-4 — Where does the SQLite file live on the host?** A named volume
  (`querypilot_data`) mirrors `pgdata` and survives `down`. A bind mount would
  make the file directly readable for debugging.

  *My lean: named volume.* It matches the existing pattern, and T7's reader is
  the supported way to look inside. A bind mount also puts a database file in the
  repository working tree, which is how a gitignored file becomes a committed one.
  > **Resolved 2026-09-09: named volume.** *"Mirror the `pgdata` approach. Bind
  > mounts inevitably lead to `.db` files accidentally getting committed to
  > version control."*

---

## 7. Risks

- **A latency test that asserts a duration will flake.** §6 of the spec already
  says so: assert the fields are recorded and attributed, never that a number is
  below a threshold.
- **The cache can poison a measurement.** It is keyed on both fingerprints for
  exactly this reason, but the eval runner must not share the process-level cache
  with the API — D-1 keeps them apart.
- **SQLite concurrency, and the guard is mandatory rather than advisory.**
  One process, but FastAPI serves requests on a thread pool, so writes genuinely
  race. **A short busy timeout and one connection per write are requirements of
  this plan, not suggestions** — the user was explicit that strict timeouts and
  connection limits on the writer are mandatory. The test suite hammers it
  concurrently rather than assuming it is fine, and D-2 means a lock that is
  still contended after the timeout drops the row instead of failing the
  answer.
- **`sqlite3` in the image.** Standard library, so no dependency — but the
  container must be able to *create* `/data`, which is a volume permission
  question and the kind of thing that works on the host and fails in Docker.
