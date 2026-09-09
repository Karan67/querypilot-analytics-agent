# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

QueryPilot answers business questions about a Postgres database in plain English
and returns three things: the answer, the SQL that produced it, and a chart. The
distinguishing property is a **bounded self-correction loop** — the model reads
its own execution errors and retries.

`specs/000-project.md` is the charter and wins any disagreement with this file.
`HANDOFF.md` carries the current state and the traps. Both are worth reading
before non-trivial work.

---

## Commands

The stack runs in Docker; the test suite and the eval runner run on the **host**
against the container. That split is deliberate — dev dependencies are not in
the runtime image.

```bash
cp .env.example .env          # then add GROQ_API_KEY and QUERYPILOT_DATABASE_URL
./db/fetch_chinook.sh         # or db\fetch_chinook.ps1 — NOT optional, stack won't start
docker compose up -d
curl http://localhost:8000/health          # user must read querypilot_ro
```

```bash
.venv/Scripts/python.exe -m pytest              # 984 tests, ~2m20s
.venv/Scripts/python.exe -m pytest tests/test_orchestrator.py -q
.venv/Scripts/python.exe -m pytest tests/test_expert_tier.py -q -k "ac12"
```

Tests **skip** rather than fail when the database is unreachable, so a wall of
skips means the stack is down. The DSN comes from `TEST_DATABASE_URL`, known
only to `tests/conftest.py`.

**There is no linter or formatter configured.** Don't invent a lint command.

### The HTTP surface (Iteration 6)

`docker compose up -d`, then <http://localhost:8000> for the page, or:

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"How many tracks are in the library?"}'
```

`GET /` serves `api/web/` — plain HTML, CSS and JS, **no build step and no new
dependency**. The files live under `api/` because the Docker build context is
`./api` with `COPY . ./api/`, so anything outside it is not in the image.
Resolve their path from `__file__`, never from the working directory.

`POST /ask` returns `ok`, `sql`, `columns`, `rows`, `shape`, `series`, `trace`,
`category`, `error`. Three rules govern it:

- **A failed question is not an HTTP error** — `200` with `ok: false`. `503` is
  for the service failing (unreachable database or provider, exhausted quota),
  where reporting "no results" would be a lie about the data.
- **`NUMERIC` crosses as a JSON string**, exactly (`"2328.60"`). `float()` drops
  the trailing zero from a money column. Parsing to float is correct in exactly
  one place: SVG bar widths.
- **`shape` and `series` are computed server-side** by `api/http/shapes.py`, the
  same classifier that produced the spec's measurements. The client renders; it
  does not re-derive.

Every failure category must appear in `api/http/errors.py` — mapped, or declared
`UNREACHABLE` with a reason. A test walks every module under `api/` and fails on
anything unaccounted for.

### The evaluation harness

```bash
.venv/Scripts/python.exe -m evals.run_evals --split dev
.venv/Scripts/python.exe -m evals.run_evals --split dev --verbose        # prints failing cases + SQL
.venv/Scripts/python.exe -m evals.run_evals --repeat 3 --record          # appends to EVALS.md
```

Key flags: `--rendering {ddl,compact}`, `--glossary` / `--no-glossary`,
`--schema {full,withheld}`, `--strategy {loop,single-shot}`, `--max-calls`.

**Quota is a real constraint and the guards are load-bearing.** The free tier
has three limits, and the one that bites is invisible to every response header:

| limit | capacity | how the guard knows |
|---|---|---|
| tokens per minute | 8,000 | live headers — advisory note, never a refusal |
| requests per day | 1,000 | live headers — refuses before the first question |
| **tokens per day** | **200,000** | `evals/ledger.py`, corrected by any 429 |

- The pre-flight refusal is computed from a **worst case** of three calls per
  question. A dev pass projects ~86,000–102,000 and actually bills ~35,000, so
  the guard refuses runs that would have fit. Recompute the projection **per
  arm** — it scales with the prompt, so a glossary-off arm projects far less.
- `.querypilot/spend.json` is UTC-keyed and gitignored. It is a **floor**: it
  counts what the eval runner spent, not what the API or another checkout did.
  Calling `answer()` directly bypasses it.
- Never raise `--daily-token-limit` past the measured 200,000 without being
  told to. `--max-projection` raises only the run's own ceiling, which is what
  lets a `--token-budget` breaker coexist with a large projection.

---

## Architecture

### The request path, and the four gates it cannot skip

```
question → build prompt (schema + glossary) → provider.complete()
         → parse "ACTION:" line → tool → observation → loop (max 3 calls)
         → final answer
```

Every database read, from any caller, goes through `execute_sql()` in
`api/db/execution.py`, which layers:

| gate | what it is | where |
|---|---|---|
| 1 | the API only ever holds the `querypilot_ro` credential | `db/init/`, compose |
| 1b | `BEGIN; SET TRANSACTION READ ONLY` — refuses even a superuser | `db/execution.py` |
| 2 | sqlglot AST validation of the candidate SQL | `api/safety/validator.py` |
| 3 | `SET LOCAL statement_timeout` + row cap via `fetchmany` | `db/execution.py` |

`api/safety/` is the gate; `api/db/execution.py` *consults* it. Nothing —
not tests, not scripts, not the eval runner — executes SQL another way. There
is exactly **one recorded exemption**, documented in `specs/000-project.md` §4.

### The agent loop

`api/agent/orchestrator.py::answer()` is the entry point. It is an explicit
`while` over an explicit `_State`, returning an `AgentResult` carrying `ok`,
`sql`, `result`, `steps`, `attempts_used`, `category`, `error` and `usage`.

The model calls tools through a **text action protocol** (`ACTION:` lines,
`api/agent/protocol.py`) rather than vendor tool schemas. That choice exists to
keep the provider interface one method wide, and it means nothing above
`api/llm/` moves when the provider changes.

`api/agent/tools.py` is a **registry, not an implementation** — each tool is a
thin wrapper delegating to its domain module (`db/introspection.py`,
`safety/validator.py`, `db/execution.py`). Apply that pattern to new tools.

### The provider boundary

`complete(system, user) -> str` is the entire contract (`api/llm/base.py`), and
`groq` is imported by exactly one module. A structural test enforces this by
walking the AST. Anything richer — token accounting (`last_usage`), rate-limit
telemetry (`last_rate_limit`) — is a **best-effort attribute on the concrete
provider**, read with `getattr`, never a widening of the protocol.

`api/llm/pacing.py` wraps the provider for benchmarks only. **The deployed API
does not pace** — sleeping inside a user's request trades a rare refusal for a
guaranteed delay.

### The eval harness

`evals/questions.yaml` holds 50 questions (dataset v3) across `easy`, `medium`,
`hard` and `expert` tiers, split into a frozen 30-question `dev` and 20-question
`test`. `--split dev` is the default so tuning cannot touch held-out questions
by omission. Reaching them takes typing `--split test`, and per-question failure
detail there is gated behind `--reveal-test-failures`, which gets recorded.

`expert` questions are hard by **interpretation**, not syntax: they turn on a
business term defined in `api/agent/glossary.py`. Each carries a `naive_sql`
that is never scored — it exists so a test can prove the question discriminates.
Some entries are now *observed* model readings rather than assumed ones; the
field's documentation in `evals/dataset.py` explains which and why.

---

## Rules that are not preferences

These are enforced by the charter and, mostly, by structural tests.

- **No agent frameworks.** No LangChain, LlamaIndex, or equivalent. The loop is
  hand-written and stays that way.
- **No vendor SDK outside `api/llm/`.** Adding a second provider means widening
  `test_ac2_no_vendor_sdk_outside_the_llm_package`, which currently scans only
  for `groq` — otherwise the guarantee silently narrows to one vendor.
- **Nothing bypasses the safety layer**, including temporary debug helpers.
- **No test hardcodes a DSN.** `TEST_DATABASE_URL`, defaulted in `conftest.py`
  alone, so CI can retarget the suite with one variable.
- **The API key never appears** in chat, a commit, a log, or an error message;
  `GroqProvider._safe_message` scrubs it defensively.
- **`EVALS.md` is append-only.** Bad numbers stay. Entries are headline
  benchmark runs, not dev-split tuning runs.
- **Never edit an eval question because the model got it wrong**, in either
  direction. Defective questions are *retired with new ids* and the loader
  refuses to reuse a retired id.

---

## How work is done here

Each iteration is `specs/NNN-name.md` (what and why, acceptance criteria, open
questions) then `specs/NNN-name-plan.md` (how, task decomposition, decisions).
The spec is drafted and **presented before any code**; ambiguity is asked about
rather than assumed. Carried debt lives in charter §8 as `B-N` entries.

**Measure before specifying.** Every spec's §2 holds numbers from the live
database or real model calls — never estimates, and never a character count
divided by four. A measurement has repeatedly changed a spec's shape.

**Mutation testing is mandatory.** After implementing a defence, remove it and
confirm a test goes red. This has caught defences a fully green suite hid in
every iteration. The recurring shape: *a default elsewhere silently stands in
for the code under test.* Make the mutation strong — replacing a value with a
textually identical one only tests the string check.

**Report faithfully.** Say which numbers are floors rather than measurements,
and what a plan got wrong.

---

## Traps that have cost real time

- **A test asserting "this string must not appear" must read code, not
  commentary.** The single most repeated bug in this repository, in four
  costumes: twice a Python structural test matched its own docstring, then an
  AC13 test matched the HTML comment *explaining* AC13 ("no accuracy claim…"),
  then an `innerHTML` test matched the JS comment *promising* not to use
  `innerHTML`. For Python, assert against the parsed AST (`ast.walk`). For HTML
  and JS, strip comments first — and pair it with a test proving the stripper
  did not gut the file, because an over-matching regex makes every absence
  assertion pass. Beware `//` inside string literals: `"http://www.w3.org/2000/svg"`
  lives in `api/web/app.js`, so only strip `//` at the start of a line.
- **sqlglot's taxonomy is not intuitive.** `exp.Drop`, `exp.Alter`,
  `exp.TruncateTable`, `exp.Set`, `exp.Grant` are **not** subclasses of
  `exp.DML`/`exp.DDL`/`exp.Command`. Catch `SqlglotError`, not `ParseError`.
  `count(DISTINCT x)` parses as `Count(this=Distinct(...))`.
- **Shared state a test can reach will eventually be written by one.** Both
  `EVALS.md` and the spend ledger were written by real tests. Resolve paths at
  call time and isolate shared state with an autouse fixture.
- **Chinook is arithmetically consistent**, so `sum(invoice.total)` equals
  `sum(line.unit_price * quantity)` exactly. Metric ambiguity is untestable
  here; population ambiguity (active vs all customers) discriminates.
- **Write prose-heavy files with the Write/Edit tools**, not shell heredocs —
  apostrophes and escaping mangle them. Pass commit messages and PR bodies via
  `git commit -F` and `gh pr create --body-file`.
- **The repo is LF throughout, and `grep -c $'\r'` lies on Windows** — Git Bash
  strips CR in text mode and reports zero. Check line endings by counting bytes
  after any scripted file edit; a Python `write_text` will silently convert a
  whole file to CRLF.
- **An init-script failure leaves Postgres half-initialised.** The volume is
  non-empty, so the next start skips init entirely and the container reports
  healthy with zero tables. Recover with `docker compose down -v`.
