# 011 — Iteration 8 plan: Ship

Status: **approved 2026-09-10**, D-1 through D-7 resolved · Created: 2026-09-10

T1-T4 done. T5 (B-10) is next.

Implements [`011-ship.md`](011-ship.md), whose §7 is resolved. This document says
*how*, and surfaces the decisions the design itself raised.

> **Resolved by the spec, and binding here.** Deployment and the demo video are
> deferred with recorded entries · no live provider call runs on a merge gate ·
> B-10 is closed by a byte-identical translation layer or not at all · Python
> **3.12.14** everywhere · feedback is collected and not consumed.

---

## 1. Approach

The spec's §2 puts the tasks in an order that is not the order they were raised
in, because two of them gate everything after:

1. **Pin the interpreter first.** CI has to run the version the suite passes on,
   and §2.5 says nobody knows whether that is 3.12. Building the pipeline before
   answering that risks encoding a version that does not work.
2. **Make the suite deterministic second.** B-9's three tests are the only
   nondeterminism in 1,109 tests. Debugging a pipeline while the suite can flake
   on its own is two problems wearing one coat.
3. **Then the pipeline**, which is mostly configuration once those hold.
4. **Then B-10**, which is the largest code change and touches the prompt. It is
   last among the code tasks precisely because AC9 can stop it dead, and a task
   that might not land should not sit under three others.
5. **Then feedback and the shipping polish.**

**Nothing in this iteration changes the prompt, the dataset or the scorer.** If
a number in `EVALS.md` moves, something is wrong (§4).

---

## 2. CI, and the thing it must not be

### 2.1 The empty-run problem is the design

§2.1 measured a pipeline that reports success having skipped all 1,109 tests. A
workflow that merely *has* a Postgres service does not fix that — it fixes it
only while the service happens to come up. The mechanism has to be that **an
unusable database is a failure, not a skip**, and that has to live where the
skip lives.

`tests/conftest.py::configured_database` currently calls `pytest.skip(...)` on an
unreachable DSN. It gains a gate:

```python
REQUIRE_DATABASE_ENV = "QUERYPILOT_TESTS_REQUIRE_DATABASE"
...
except SQLAlchemyError as exc:
    if os.environ.get(REQUIRE_DATABASE_ENV) == "1":
        pytest.fail(f"...")   # CI: an unusable database is a failed run
    pytest.skip(f"...")       # a developer with the stack down
```

The developer's experience is unchanged and CI sets the variable. **This is the
task's mutation**: unset it in the workflow, point the DSN at nothing, and
confirm the build goes red rather than green-and-empty.

A second belt, because one flag is one line somebody can delete: the workflow
asserts a **floor on tests actually executed**, read from a machine-readable
report rather than from stdout. `--junitxml` gives counts; the job fails if
`tests - skipped` is below a floor. See D-1 for the shape.

### 2.2 The database CI gets

`docker compose up -d`, not a GitHub Actions `services:` block. Three reasons,
in descending order of weight:

- **The init scripts are the product.** `db/init/` runs in filename order on an
  empty volume, and its ordering is load-bearing — `03_readonly_role.sh` grants
  on relations `01` and `02` created, and asserts the role holds no non-`SELECT`
  privilege. A `services:` container cannot easily mount them, so CI would test a
  database built a different way from every other one.
- **Charter S6 wants exactly this verified**: *`docker compose up` on a clean
  machine yields a working system*. A pipeline that does it on every push is the
  cheapest proof that claim will ever get.
- It is the same command the README gives, so the pipeline cannot drift from the
  documentation.

The cost is a slower job — an image pull and a Chinook load — and a dependency on
the Chinook download, which D-2 addresses.

### 2.3 What the gate runs, and what it cannot

| | on the merge gate | elsewhere |
|---|---|---|
| the 1,106 deterministic tests | **yes** | |
| the 3 live provider tests | **no** (resolved Q-B) | manual dispatch |
| `evals/run_evals.py` | **no** — it spends tokens and needs a key | manual, as now |
| `node --check` on the page scripts | yes, if the runner has node | skips otherwise |

The gate needs **no secret**, so it works on a fork and a pull request from
outside. That property is worth protecting: the moment the gate needs
`GROQ_API_KEY`, every external contribution fails on a permissions error that
looks like a test failure.

---

## 3. B-9: assert the invariant, report the behaviour

Each of the three tests keeps its subject and changes what it asserts.

| test | asserts now | will assert |
|---|---|---|
| injection | the model refuses | `track` still has 3,503 rows; whatever SQL was produced reached the database only through Gate 2; the role is still read-only |
| simple question | rows equal `((3503,),)` | the call round-trips through the loop and the gate, and *if* it answered, the answer came from a validated `SELECT` |
| clean content | no `<think>`, no fences | **`extract_sql` copes with whatever came back** — the parser's robustness, which is ours, not the model's |

The third is the interesting one. Asserting the model emits no fences tests the
model; asserting our parser handles fences *and* bare SQL *and* prose tests the
code we own, and it is deterministic because the inputs can be fixtures.

**What is given up, recorded rather than glossed (AC7):** nothing will notice if
the model stops refusing injections. That was never a defence — Gates 1 and 2
are — but it was a canary, and the canary is going. The honest replacement is
that a refusal is *printed* by the live test when a human runs it.

---

## 4. B-10: the same bytes, through the gate

### 4.1 Shape

`api/db/introspection.py` stops using `Inspector` and builds its `Schema` from
catalog queries through `execute_sql()`. Three queries, not fifty-two:

```
relations   relname, relkind                    -> Table.name, Table.kind
columns     attname, format_type(...), attnotnull, pk membership
foreign keys  conrelid, conname, pg_get_constraintdef(oid) parsed, or
              conkey/confkey joined to attnames
```

A new `api/db/type_names.py` translates `format_type` output into the spelling
the `Inspector` produced (§2.4): five families, and the formatting detail that
`numeric(10,2)` becomes `NUMERIC(10, 2)` **with a space**.

### 4.2 How byte-identity is proven without running the old path

This is the part the design had to solve rather than choose. Comparing the two
renderers means *running* the `Inspector`, and the `Inspector` is the violation
being removed — a test that calls it is a test that bypasses Gate 2, which
charter §4 forbids for tests explicitly.

So the comparison happens **once**, during implementation, and its output is
frozen into the test as an expected string:

1. Render the schema through the `Inspector` in both renderings (`compact` and
   `ddl`) and write the two strings to `tests/fixtures/rendered_schema_*.txt`.
2. The test renders through the new path and asserts equality with those files.
3. The `Inspector` is then deleted, and nothing in the repository calls it.

The frozen strings are safe against a reseed for the same reason the prompt
fingerprints are: Chinook is fixed, and the dataset's row-count fingerprint
already catches a reseed. See D-3.

### 4.3 The stop condition

**AC9 is a gate, not a goal.** If the rendered bytes differ and cannot be made
identical, the task stops, the work is reverted, and B-10 goes back to the board
with what was learned. Re-baselining `EVALS.md` to accommodate a refactor is not
an available outcome.

---

## 5. Feedback: collect, do not consume

```sql
CREATE TABLE IF NOT EXISTS feedback (
  id         TEXT PRIMARY KEY,          -- uuid4
  ask_id     TEXT NOT NULL REFERENCES ask (id),
  created_at TEXT NOT NULL,             -- ISO 8601, UTC
  rating     INTEGER NOT NULL,          -- -1 or 1; see D-6
  note       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS feedback_ask ON feedback (ask_id);
```

`POST /feedback` takes `{id, rating, note?}`, writes a row, returns `201`.
Several marks per answer are allowed and none replaces another — append-only,
for the reason `EVALS.md` is.

**Unknown `ask_id` is a `404`** (D-6). Accepting it would store a row nothing can
ever interpret, which is the orphan version of the empty-list-versus-503 mistake
T7 avoided.

`/history` gains a column, and **nothing aggregates it** (AC14). No rate, no
percentage, no "3 of 4 answers were good" — §2.6 measured zero bad answers in
the record, and a proportion over that is the accuracy claim `009` AC13 banned.

The note is length-capped like `AskRequest.question`, because this is the first
unauthenticated **write** in the project.

---

## 6. Files and decomposition

| task | what | verified by |
|---|---|---|
| **T1** | Charter: defer deployment and the demo video; retire §9's four stale questions | Read it. **Pause.** |
| **T2** | Python 3.12.14 + a lockfile (AC4, AC5) | The suite passes on 3.12. **Pause** — it may not. |
| **T3** | B-9: three invariant tests (AC6, AC7) | Run them repeatedly; no flake |
| **T4** | CI: workflow, the require-database gate, the executed-tests floor (AC1–AC3) | **The empty-run mutation.** **Pause.** |
| **T5** | B-10: catalog SQL, type translation, frozen renderings (AC8, AC9) | Byte-identical rendering; fingerprints reproduce. **Pause.** |
| **T6** | Feedback: table, `POST /feedback`, the `/history` column (AC13, AC14) | Round trip; the no-aggregation structural test |
| **T7** | `api` healthcheck, README numbers, clean-machine boot (AC10–AC12) | `down -v` then `up`. **Pause.** |

New: `.github/workflows/ci.yml`, `api/requirements.lock`,
`api/db/type_names.py`, `tests/fixtures/rendered_schema_compact.txt`,
`tests/fixtures/rendered_schema_ddl.txt`, `tests/test_type_names.py`,
`tests/test_feedback.py`, `tests/test_ci_guards.py`.
Edited: `api/db/introspection.py`, `api/store/schema.sql`,
`api/store/history.py`, `api/main.py`, `api/web/history.js`,
`tests/conftest.py`, `tests/test_llm_live.py`, `docker-compose.yml`,
`api/Dockerfile`, `README.md`, `specs/000-project.md`.

---

## 7. Decisions

- **D-1 — What is the executed-tests floor, and where does it live?** AC2 needs a
  mechanism, not a sentence. The `REQUIRE_DATABASE` flag fixes the root cause; a
  floor is the belt in case the flag is removed.

  *My lean: a floor in the workflow, read from `--junitxml`, set to a round
  number well below the current count (1,000).* Not the exact count, because a
  floor that fails on every added test is a floor people raise without reading;
  not a percentage, because there is nothing stable to take a percentage of. The
  flag is the fix and this is the smoke alarm.

- **D-2 — Where does CI get Chinook?** `./db/fetch_chinook.sh` downloads ~600KB
  from an external URL. Every run depends on that host being up, and the seed is
  gitignored deliberately.

  *My lean: fetch, but cache it, keyed on the fetcher script's own hash.* An
  external download on every push is a pipeline that goes red for reasons
  unrelated to the code — the failure mode people learn to re-run past.
  Committing the seed is the other option and it contradicts a standing
  decision; vendoring 600KB of third-party SQL into the tree is not mine to
  reverse.

- **D-3 — Are frozen rendered-schema fixtures acceptable evidence?** §4.2 freezes
  two strings produced by the code being deleted, and from then on the test
  compares against a file rather than against the truth.

  *My lean: yes, and it is the only option that does not violate §4.* The
  alternative is a test that calls the `Inspector`, which is the bypass being
  removed. The residual risk is that the fixtures are wrong at the moment they
  are written, so they are generated **and** the fingerprints are checked to
  reproduce — two independent confirmations before the old path goes.

- **D-4 — How does 3.12 get onto this machine?** It is not installed (§2.5's
  addendum). `uv 0.12.4` is present and can fetch it.

  *My lean: `uv python install 3.12`, then recreate `.venv` from it.* It needs no
  administrator rights and no change to how the project is run — the commands
  stay `.venv/Scripts/python.exe -m pytest`. **This installs software on your
  machine, so it is a decision rather than a step I will take unasked.**

- **D-5 — What generates the lockfile?** A fully-pinned file, versus `pip-tools`,
  versus hashes.

  *My lean: `pip freeze` on 3.12 into `api/requirements.lock`, no new tooling,
  and the ranged files stay as the human-edited source.* Hashes are the stronger
  answer and they need `pip-compile --generate-hashes` and a workflow to
  regenerate; the project has no build step and adding one to ship a lockfile
  inverts the cost. Revisit if the pipeline ever publishes an artefact.

- **D-6 — What is a rating, and what happens to an unknown answer id?** A boolean
  is the smallest thing; a 1–5 scale collects more and invites averaging.

  *My lean: `-1` or `1`, stored as an integer, and `404` on an unknown id.* Two
  values cannot be averaged into a number that looks like accuracy, which is
  exactly the pressure AC14 exists to resist — and §2.6 says there is no data to
  justify a finer instrument yet. The integer column means a scale can arrive
  later without a migration.

- **D-7 — Does `sample_rows` come back?** Not asked for, and named because T5
  touches introspection and the temptation will be there. *No.* It was retired at
  Iteration 5 T1 on measurement, and this iteration changes no prompt.

---

## 8. Risks

- **AC2 is the criterion most likely to be satisfied by a comment.** It gets an
  explicit mutation in T4: break the DSN in the workflow and require the build to
  go red. Until that has been *seen*, the criterion is unmet.
- **T2 is the least predictable task here.** The suite has never run on 3.12.
  Anything it surfaces will present as a test defect and be an interpreter
  difference. `psycopg[binary]`, `sqlglot` and `tiktoken` all ship wheels per
  version and are the likely places for it to show.
- **T5 can fail legitimately.** If the bytes will not match, the right outcome is
  a reverted task and an updated B-10, not a widened test. §4.3 says so in
  advance so that the decision is not made under sunk cost.
- **The first unauthenticated write.** T6 adds one. It is length-capped and
  append-only, and it is worth stating plainly that the project still has no
  authentication and this iteration does not add any (§4 of the spec).
- **CI will be the first thing that runs this repository on a machine nobody
  configured.** Expect it to find one or two assumptions about paths, line
  endings or a running Docker daemon that have been true for eight iterations
  because one machine made them true.
