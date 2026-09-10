# 011 — Iteration 8: Ship

Status: **approved 2026-09-10**, §7 resolved · Created: 2026-09-10

> **Resolved questions.** Q-A deployment and the demo video are **deferred with
> recorded entries**; scope is CI, B-9, B-10 and feedback · Q-B **no live
> provider calls on a merge gate** — merging is deterministic and zero-cost ·
> Q-C **the byte-identical translation layer**, because the comparability of the
> recorded fingerprints is non-negotiable · Q-D **Python 3.12.14 everywhere**,
> the containerised shipping runtime · Q-E **a lightweight collector** on the
> existing answer id, persisted and not consumed.

Charter §6 names this iteration **Ship**: *"Deployed, evals running in CI, README
with honest numbers, demo video, **and feedback**"* — the last item inherited
from Iteration 7 T1, which deferred AC6 openly rather than carrying it unmet.

---

## 1. What this iteration is for

Seven iterations built something that works on one machine. This one is about
whether that survives contact with anyone else: a pipeline that fails when the
code is wrong, a dependency set that resolves the same way twice, and a
statement of what the thing actually does that a stranger can check.

It also carries two safety debts that are cheap to state and were not cheap to
find:

- **B-9** — the live tests gate on what the model *says*, so they are
  nondeterministic. One went red on a full run and green on a re-run. Putting
  those into CI would make the pipeline a coin toss.
- **B-10** — `get_schema()` has reached the database around Gate 2 since
  Iteration 1, through SQLAlchemy's `Inspector`, at 52 statements a call.

**The measurements below moved this spec twice.** A CI pipeline was going to be
a small job that runs pytest; §2.1 shows that would have been worse than no
pipeline. And B-10 looked like a mechanical refactor until §2.4 showed it
changes the prompt.

---

## 2. Measurements

Taken 2026-09-10 against the running stack and the current tree. No estimates.

### 2.1 A CI pipeline without Postgres would be green and empty

The whole suite is gated behind one session-scoped autouse fixture that
**skips** rather than fails when the database is unreachable — a deliberate
choice, so that a developer with the stack down gets one line of explanation
instead of a wall of red. In CI that choice inverts:

```
$ TEST_DATABASE_URL=<unreachable> pytest -q
1109 skipped, 6 warnings in 5.26s
$ echo $?
0
```

**Exit code 0. Eleven hundred and nine tests skipped. A green build that
verified nothing.** This is the single most dangerous thing about adding CI to
this repository, and it is not hypothetical: it is the default behaviour of the
suite as written, today.

The granularity to do better does not exist either. Only **four test files —
89 of 1,109 tests — never reference the database** in any form:

| file | tests |
|---|---|
| `test_eval_dataset.py` | 38 |
| `test_daily_quota_guards.py` | 20 |
| `test_llm_provider.py` | 18 |
| `test_error_mapping.py` | 13 |

So there is no meaningful database-free lane: **8% coverage**. CI needs a real
Postgres with Chinook loaded, and it needs to fail rather than skip when it does
not have one.

### 2.2 CI needs no provider secret, and costs no tokens

```
$ GROQ_API_KEY="" pytest tests/test_llm_live.py -q
3 skipped in 0.02s
```

Exactly **three** tests touch a live provider, and they skip cleanly on a
missing key with a reason that names the variable. Everything else — all 1,106
remaining tests — runs with no credential and spends nothing.

That is a genuinely good position to be in: the pipeline can be public, forkable
and free.

### 2.3 All three live tests assert model behaviour, not safety

B-9 was filed against one test. It is three:

| test | what it asserts | deterministic? |
|---|---|---|
| `test_prompt_injection_does_not_produce_executable_ddl` | the model **refuses** | **no** — went red once, green on re-run |
| `test_a_simple_question_produces_executable_sql` | the model gets the answer right | **no** in principle |
| `test_default_model_returns_clean_content` | the model emits no `<think>` and no fences | **no** — it is a change detector by design |

The injection test's own docstring already says *"persuasion is not the threat,
and the assertion is about what reaches the database"* — and then asserts the
persuasion failed. On the run that went red the model answered with a harmless
`SELECT track_id FROM ...` instead of refusing; the safety property held on
both runs, because `track` still had all 3,503 rows.

**The invariant is checkable and deterministic. The behaviour is not.**

### 2.4 Replacing the `Inspector` changes the prompt

This is the measurement that reshaped B-10. The same six columns of `track`:

| | `get_schema()` (Inspector) | hand-written `pg_catalog` |
|---|---|---|
| `track_id` | `INTEGER` | `integer` |
| `name` | `VARCHAR(200)` | `character varying(200)` |
| `unit_price` | `NUMERIC(10, 2)` | `numeric(10,2)` |

The rendered schema goes into the system prompt, and the prompt is hashed into
the fingerprint every `EVALS.md` entry carries. **A naive replacement therefore
retires the comparability of the entire measurement record** — the same cost
that deferred B-4 to its own milestone.

It is avoidable, and the escape is small. Chinook's whole schema needs **five
base type families** translated, not seventeen spellings:

```
bigint                      -> BIGINT
integer                     -> INTEGER
numeric / numeric(10,2)     -> NUMERIC / NUMERIC(10, 2)     (note the space)
character varying(N)        -> VARCHAR(N)
timestamp without time zone -> TIMESTAMP
```

And the correctness of the translation is **testable to the byte**: render the
schema both ways and require the strings to be identical. That turns a "trust
me" refactor into an acceptance criterion.

The number to be honest about is that five families is *Chinook's* answer. A
real warehouse brings `text`, `boolean`, `date`, `jsonb`, `uuid`, arrays and
domains, and each is a line in a mapping nobody will remember to update. That is
the maintenance question B-10 named, now with a number attached to one side of
it and no number on the other.

### 2.5 The host and the image run different Pythons, and nothing pins it

```
host  (.venv):  Python 3.14.6
image (api):    Python 3.12.14   (FROM python:3.12-slim)
```

`api/requirements-dev.txt` pins SQLAlchemy and FastAPI to the image's ranges
*specifically* because a skew "surfaces as a confusing assertion failure rather
than as the version problem it actually is". The same argument applies to the
interpreter and nothing applies it. Two minor versions apart, the suite passes
on the host and has never been run on 3.12.

`api/requirements.txt` also already says, in a comment written at Iteration 0:
*"Ranges now, a lockfile at Iteration 8."*

### 2.6 Feedback still has nothing to attach to

The store, after two days of real use:

```
answers recorded      : 29
  failed (ok = 0)     : 0
  needed a retry      : 0
  distinct questions  : 17
```

**Zero wrong answers and zero retries.** The signal feedback exists to capture
is *which answers were bad*, and there are no bad answers in the record to mark.
Iteration 7 T1 deferred AC6 because nothing about it was measured; that is still
true, and this is the measurement that says so rather than an assumption.

What *does* exist is the attachment point: every answer carries a uuid returned
in the `/ask` payload and stored in `ask.id`, built at T3 precisely so feedback
would attach to an answer and never to a question string.

### 2.7 None of the shipping apparatus exists

| | |
|---|---|
| `.github/workflows/` | absent |
| a lockfile | absent |
| `.dockerignore` | absent |
| a healthcheck on the `api` service | **absent** — only `db` has one |
| `pyproject.toml` | absent (`pytest.ini` only) |

The missing `api` healthcheck is worth calling out: `docker compose ps` reports
the API as `Up` whether or not it can serve a request, and Iteration 0's whole
lesson was that a container reporting healthy while broken is worse than one
that reports nothing.

### 2.8 The suite runs in 95s

Down from ~144s before Iteration 7 T6, which is the schema cache paying off
inside the test run. 1,109 tests. This is the number a CI budget is drawn
against, and it is small enough that no parallelism or splitting is warranted.

---

## 3. Acceptance criteria

### CI that can fail

- **AC1** — A pipeline runs the full suite on every push and pull request,
  against a real Postgres with Chinook loaded.
- **AC2** — **A run that would skip the suite fails instead.** §2.1 is the
  reason this is a criterion rather than an implementation detail: the pipeline
  must be incapable of reporting success without having executed the tests. A
  floor on tests actually run, asserted by the pipeline itself.
- **AC3** — The pipeline needs **no provider credential** and spends **no
  tokens** (§2.2). The three live tests are excluded by construction, not by a
  secret happening to be absent.
- **AC4** — The pipeline pins the Python version, and it is **the image's
  version** unless Q-D decides otherwise (§2.5).
- **AC5** — Dependencies resolve identically on every run: a lockfile, as
  `requirements.txt` has promised since Iteration 0.

### Safety that does not depend on the model's mood

- **AC6** — **No test gates on model behaviour.** The three tests in §2.3 assert
  invariants instead: the database is unharmed, the role is read-only, nothing
  reached the database except through Gate 2. What the model chose to say is
  *reported*, never asserted.
- **AC7** — B-9 is discharged, and the discharge names what was given up: a
  refusal is no longer required, so a model that stops refusing will no longer
  be noticed by this suite. That is a real loss and it is recorded rather than
  glossed.
- **AC8** — **Nothing reaches the database except through `execute_sql()`**, and
  it is true rather than nearly true (B-10). Whatever Q-C decides, the outcome is
  either the violation removed or a second §4 exemption recorded with the same
  rigour as the first.
- **AC9** — If B-10 is closed by replacement, **the rendered schema is
  byte-identical** before and after, and a test asserts it (§2.4). The prompt
  fingerprints `0d280c367c5e`, `91036a089282` and `f971d8787f0c` still
  reproduce, so `EVALS.md` stays comparable.

### Honest, and shippable

- **AC10** — The README states what the system does and what it costs, with
  every number traceable to `EVALS.md` or a spec §2. No number appears without
  its caveat, and the held-out result is still stated as *between 90% and 100%,
  measured once at 100%*.
- **AC11** — `docker compose up` on a clean machine yields a working system
  (charter S6), verified from an empty volume rather than asserted.
- **AC12** — The `api` service reports its own health to Docker (§2.7), so
  `docker compose ps` cannot show a broken API as `Up`.

### Feedback (AC6 of `010`, inherited)

- **AC13** — A user can mark an answer good or bad, and the mark is stored
  against the **answer id**, not the question.
- **AC14** — **Nothing aggregates it.** §2.6 measured zero bad answers in the
  record; a rate computed over an empty set, or a dashboard implying one, would
  be the accuracy-claim failure `009` AC13 kept off the page.

---

## 4. Non-goals

- **A hosted deployment on a paid platform.** Q-A settles the scope, but a
  running URL that costs money per month is a commitment beyond this project's
  brief and would put the free-tier quota in front of the public internet.
- **Authentication.** The app has none, and adding it is its own iteration. It
  is named here because AC13 adds a *write* endpoint to an unauthenticated
  surface, which is a real change in kind.
- **Multi-platform CI.** One Python, one OS. §2.5 shows the project cannot yet
  claim it passes on two interpreters; claiming three platforms would be worse.
- **Test parallelism or sharding.** 95s (§2.8).
- **Any accuracy work.** No prompt change, no dataset change, no new benchmark.
  If a number in `EVALS.md` moves this iteration, something is wrong.

---

## 5. Contracts this iteration must not break

- `complete(system, user) -> str` stays one method wide, and `groq` stays
  imported by exactly one module.
- `EVALS.md` stays append-only, and its recorded fingerprints keep reproducing
  (AC9). **If they cannot, the iteration stops and asks** rather than
  re-baselining quietly.
- The deployed API still does not pace, and `/ask` still answers synchronously.
- The answer cache stays unreachable from `evals/`.
- Nothing in `db/init/` changes, because changing it requires `down -v` and that
  discards the question log along with the database.

---

## 6. Risks

- **AC2 is the criterion most likely to be implemented as a comment.** "Make
  sure the tests actually run" is a sentence; a pipeline that fails on an empty
  run is a mechanism. It needs a mutation of its own: break the database
  connection in the pipeline and confirm the build goes red.
- **The Python bump is the largest untested change here.** The suite has never
  run on 3.12. Anything it surfaces will look like a test defect and be a
  version difference.
- **B-10's replacement is the kind of refactor that looks finished early.** A
  byte-identical rendered schema for Chinook is not a byte-identical renderer;
  the mapping is only as good as the types this one database happens to use.
- **AC13 adds an unauthenticated write.** Small, but it is the first one, and
  the rate limit that protects `/ask` is the provider's rather than ours.

---

## 7. Open questions

- **Q-A — What is in scope?** Charter §6 says *"Deployed, evals running in CI,
  README with honest numbers, demo video, and feedback"*. The four items raised
  for this iteration are CI, B-9, B-10 and feedback — which leaves **deployment
  and the demo video** unaddressed.

  *My lean: CI, B-9, B-10, feedback, the lockfile and the README, and defer
  deployment and the demo video with a recorded reason.* A demo video is not
  code and needs the rest finished first; a deployment needs an answer about
  where the API key lives and who may spend it, which is a decision rather than
  a task. Deferring openly is what T1 did with feedback and it cost nothing;
  carrying them unmet is what AC13 did in `008` and cost three days.
  > **Resolved 2026-09-10: defer both, with recorded entries.** *"Production
  > deployment requires external infrastructure decisions around key
  > provisioning, secrets management, and egress billing that fall outside this
  > hardening and testing milestone."* The deferral is written into the charter
  > and `HANDOFF.md` rather than left as a gap, which is the T1 pattern.

- **Q-B — Do the live provider tests run anywhere in CI?** AC3 keeps them out
  of the default pipeline. They could still run on a schedule with a secret, or
  on demand.

  *My lean: out of CI entirely, and run locally before a release.* Three tests
  that spend tokens and depend on a third party do not belong on a gate that
  blocks a merge. A nightly job that fails for a rate limit teaches people to
  ignore it.
  > **Resolved 2026-09-10: not on the merge gate.** *"Merging must be strictly
  > deterministic and zero-cost. Merge gates should test structural constraints,
  > security gates, and mocked integration paths. Live evals belong on manual
  > dispatch or scheduled runs."* So the gate is deterministic by construction,
  > and a live path may exist only where a human chose to invoke it.

- **Q-C — How is B-10 closed?** Three options, and §2.4 priced them:
  1. **Replace the `Inspector`** with hand-written catalog SQL through
     `execute_sql()`, plus a type-name translation, with a byte-identical
     rendered schema as the test.
  2. **Record a second §4 exemption**, bounded and argued like the first.
  3. Leave it filed and open.

  *My lean: option 1, and stop if the rendered schema is not byte-identical.*
  §2.4 shows the translation is five families for this database and the test is
  exact, so the risk is bounded and visible. Option 2 doubles the exceptions to
  a rule whose whole value is having none — and the user's ruling on B-10 was
  that this is a layer violation, which option 2 concedes rather than fixes.
  > **Resolved 2026-09-10: option 1.** *"Implement the byte-identical
  > translation layer across the five Chinook base type families. Protecting the
  > cryptographic comparability of our historical `EVALS.md` fingerprints is
  > non-negotiable. The test criterion must enforce identical bytes between the
  > two renderers."* AC9 is therefore a hard gate: if the bytes differ, the task
  > stops rather than re-baselining.

- **Q-D — Which Python does the project standardise on?** §2.5 measured 3.14.6
  on the host against 3.12.14 in the image, with nothing pinning either.

  *My lean: 3.12 everywhere, matching the image.* The image is what ships, and a
  suite that has never run against the shipping interpreter is not testing what
  ships. This means recreating the local `.venv` on 3.12, and it may surface
  failures — which is the point.
  > **Resolved 2026-09-10: 3.12.14 everywhere.** *"The suite must run against
  > the exact interpreter version deployed in production. Pin this in the dev
  > tooling and CI runner to eliminate interpreter drift."*
  >
  > **Measured after the decision, and it needs a step nobody has taken:**
  > 3.12 is **not installed on this host**. Only 3.14.6 and a `uv`-managed
  > 3.14.7 are present. `uv 0.12.4` is installed and can supply it
  > (`uv python install 3.12`), which is the plan's D-4.

- **Q-E — What shape is feedback, given §2.6?** There are no bad answers in the
  record to learn from, and no users to ask.

  *My lean: the smallest honest thing.* `POST /feedback` taking an answer id and
  a good/bad mark with an optional comment, stored in the existing SQLite store,
  surfaced on `/history` as a column and **nowhere aggregated** (AC14). It
  closes the inherited criterion, it costs almost nothing, and it starts
  collecting the signal whose absence is exactly why §2.6 reads the way it does.
  The alternative — build the loop that *uses* feedback — has nothing to
  validate against and would be the unmeasured feature T1 deferred, rebuilt.
  > **Resolved 2026-09-10: the lightweight collector.** *"Implement a
  > lightweight collection mechanism attached directly to the existing
  > operational history id (e.g., `POST /feedback` with id, boolean/rating, and
  > an optional note). Focus on cleanly persisting the signal in the SQLite
  > store rather than over-engineering consumption loops before we have real
  > failure data."*
