# QueryPilot

A Text-to-SQL analytics agent: ask a business question in plain English, get the
answer, the SQL that produced it, and a chart.

Read [`specs/000-project.md`](specs/000-project.md) first — it is the source of
truth for intent, scope, non-goals, and the safety rules that bind every
iteration.

**Current state: Iteration 7 (Hardening).** `docker compose up` gives you a
working page at **<http://localhost:8000>** — ask a question, get the answer,
the SQL that produced it, and the agent's steps. Behind it: a hand-written
agent loop that reads its own execution errors and retries, a four-gate safety
layer nothing bypasses, and a 50-question benchmark with a held-out split.

Every accuracy number lives in [`EVALS.md`](EVALS.md) with its caveats, and the
numbers are deliberately not repeated here — the honest reading of the held-out
result is *between 90% and 100%, measured once at 100%*, and a README is where
that nuance would die.

---

## Quickstart

**Prerequisites:** Docker Desktop.

```bash
cp .env.example .env
```

Fetch the sample dataset (~600 KB of SQL into `db/seed/`, which is gitignored).
**This step is not optional — the stack will not start without it.**

On Windows PowerShell:

```powershell
.\db\fetch_chinook.ps1
```

On macOS, Linux, or Git Bash:

```bash
./db/fetch_chinook.sh
```

Then bring the stack up:

```bash
docker compose up --build
```

Check it:

```bash
curl http://localhost:8000/health
```

Expected:

```json
{
  "status": "ok",
  "database": {
    "connected": true,
    "user": "querypilot_ro",
    "database": "chinook",
    "public_tables": 12
  },
  "history": { "writable": true, "error": "" }
}
```

`user` must read `querypilot_ro`. If it reads anything else, the API is holding
a privileged credential and Gate 1 of the safety layer is not in place.

`history` reports whether the question log can be written. A failure there shows
up as `degraded_history` and **still returns 200**: recording a question and
answering it are independent, and reporting the service as down because logging
broke would be the cascade that arrangement exists to prevent.

Then open **<http://localhost:8000>** and ask something:

> *How many tracks are in the library?*
> *Show the 10 genres with the most tracks, giving the genre name and the count.*

You get the answer, the SQL that produced it — always visible, because an answer
nobody can check is worth less than no answer — and the agent's steps in a
collapsed panel, including any query that failed and the database error it read
before retrying.

There is no build step and nothing to install. The page is plain HTML, CSS and
JavaScript served by the same container, so `docker compose up` really is the
only setup instruction.

Or ask it over HTTP:

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"What is the total value of all invoices?"}'
```

Numbers from `NUMERIC` columns come back as JSON **strings** — `"2328.60"`, not
`2328.6`. That is deliberate: a float round trip drops the trailing zero from a
money column, and the value on screen should be the value in the database.

The answer also carries what it cost and where it came from: `usage` (the
provider's own billed token count, with `measured` saying whether it is billed
or locally estimated), `total_ms` and `provider_ms` kept apart, and `cache_hit`.
Every question is recorded in a SQLite store in the `querypilot_data` volume,
which survives `docker compose down` and is discarded only by `down -v`.

**<http://localhost:8000/history>** reads that store back: every question with
its cost, its latency, whether it was served from cache, and the agent's steps
including any query that failed. The totals there are sums over the rows shown,
so a surprising number can always be traced to the answer that caused it. Note
that a cached answer records **zero** tokens rather than replaying what the
original cost — so the token column sums to what was actually billed, with no
filtering to remember.

That page is not a benchmark, and it says so: the counts describe whatever was
typed into the box, with no known-good answers and nothing held out.
[`EVALS.md`](EVALS.md) is the record that can be compared, with its caveats.

Asking the same question twice costs **zero** tokens the second time, and the
page says so rather than presenting a reused answer as a fresh one. The key is
the exact question text plus fingerprints of the schema and the prompt, so a
schema change or a prompt edit invalidates it — a change to the *data* does not.

```bash
curl http://localhost:8000/quota
```

reports what the provider last said about its limits. It is worth knowing that
the free tier does not refuse when its per-minute token bucket runs low — it
**slows down**, from about 750ms to as much as 10s, with no error and no header
a user would ever see. That is what this endpoint and the page's banner exist to
explain. It reports two buckets and not three: the 200,000-tokens-per-day limit
appears in no response header at all, only in the body of a 429.

---

## How the database is built

Postgres runs the scripts in `db/init/` **once**, in filename order, and **only
when the data volume is empty**:

| Script | What it does |
|---|---|
| `01_load_chinook.sh` | Loads `db/seed/chinook.sql` into `$POSTGRES_DB` |
| `02_create_views.sql` | Creates the `invoice_totals` view — **before** any grant runs |
| `03_readonly_role.sh` | Creates `querypilot_ro`, grants `CONNECT`, `USAGE`, `SELECT`, adds `ALTER DEFAULT PRIVILEGES` for future relations, and pins `statement_timeout` (default `10s`) — then asserts the role holds no non-SELECT privilege, that the timeout took, and that every public relation is readable |

**The numbering is a dependency, not decoration.**
`GRANT SELECT ON ALL TABLES IN SCHEMA public` is a one-time snapshot over
relations that exist at grant time. This was verified directly against a
throwaway database: every relation created *after* the grant — table, view, and
materialized view alike — was denied to the role.

A view created after the grant would still be reported by the schema tool, so
the agent would write a valid query against a relation it cannot read: the SQL
passes validation, then fails at execution with *permission denied*, and the
agent cannot self-correct because nothing about its query is wrong.
`03_readonly_role.sh` fails startup if any public relation is unreadable, so
this cannot ship silently.

Because init scripts are skipped on a non-empty volume, **changing anything in
`db/` requires recreating the volume**:

```bash
docker compose down -v && docker compose up --build
```

---

## Running the tests

The suite runs on the **host** against the live container. One-time setup:

```powershell
python -m venv .venv
```

```powershell
.\.venv\Scripts\python.exe -m pip install -r api\requirements-dev.txt
```

Then, with the stack up:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The database location comes from **`TEST_DATABASE_URL`**, defaulting to
localhost. No test hardcodes a host or port — only `tests/conftest.py` knows
where the database is, so CI can retarget the whole suite with one variable:

```bash
TEST_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/chinook pytest
```

If nothing is listening, every test **skips** with a reason rather than failing.
An unreachable database is an environment problem, and a wall of failures would
bury the one line that says so.

### Troubleshooting

**`ERROR: seed file not found at /seed/chinook.sql`, then `dependency db failed
to start`.** The fetch step was skipped. Run the fetcher, then recreate the
volume — `docker compose up` alone will not recover.

This failure has a nasty second half worth understanding. When an init script
fails, Postgres has *already* initialised `PGDATA`. The volume is therefore
non-empty, so on the next start Postgres **skips the init scripts entirely** and
the container comes up reporting `healthy` — with zero tables and no
`querypilot_ro` role. The API then fails authentication against a database that
looks fine from the outside. Any init failure must be recovered with:

```bash
docker compose down -v && docker compose up --build
```

**`curl: Unable to connect to the remote server` on Windows.** If `db` never
became healthy, `api` never started — `depends_on: condition: service_healthy`
holds it back deliberately. Fix the database first; the API is a symptom, not
the cause. (Note that PowerShell aliases `curl` to `Invoke-WebRequest`, which
prints errors in a different format than real curl.)

---

## Running the evaluation

The benchmark scores the agent against **50** hand-written reference queries
across four tiers -- `easy`, `medium`, `hard` and `expert` -- split into a
frozen 30-question `dev` set and a 20-question held-out `test` set. It needs
the database up and `GROQ_API_KEY` in `.env`.

`--split dev` is the default, so a tuning run cannot touch the held-out
questions by omission; reaching them takes typing `--split test`.

```bash
python -m evals.run_evals                       # score and print
python -m evals.run_evals --verbose             # ... with every failing case
python -m evals.run_evals --repeat 3            # three passes, report the spread
python -m evals.run_evals --repeat 3 --record   # ... and append to EVALS.md
```

**`--record` is deliberately opt-in.** The value of `EVALS.md` is that every
entry is a real measurement; a debugging run appending to it would destroy
exactly that.

The dataset lives in [`evals/questions.yaml`](evals/questions.yaml). Two rules
about editing it are worth repeating here: an id is permanent, so a question
that changes meaning becomes a *new* id rather than an edited one — otherwise
the history in `EVALS.md` stops being comparable — and **no question is ever
edited because the model got it wrong**, in either direction.

---

## Layout

```
specs/          source of truth — one spec per feature
evals/          question set + scorer (Iteration 3)
api/
  main.py       FastAPI app; GET /, /history, /health, /quota; POST /ask
  agent/        orchestrator, tools, prompts, glossary (Iterations 1–5)
  safety/       sqlglot AST gate (Iteration 1)
  db/           read-only engine, execution, introspection
  llm/          provider behind one method, plus pacing and rate limits
  http/         shape classifier, JSON boundary, errors, cache, quota
  store/        SQLite history of every answered question (Iteration 7)
  web/          the page — plain HTML, CSS and JS, no build step (Iteration 6)
db/             dataset fetcher + Postgres init scripts
tests/          unit + integration tests
EVALS.md        accuracy log over time (Iteration 3)
```

---

## Ground rules

These are enforced by [`specs/000-project.md`](specs/000-project.md), not by
preference:

- **No agent frameworks.** The loop is hand-written Python. LangChain,
  LlamaIndex, and equivalents are out.
- **The LLM provider stays behind a swappable interface.** No vendor SDK is
  imported by the orchestrator, tools, or safety layer.
- **Nothing bypasses the safety layer.** No code path executes SQL without
  passing the validator — not tests, not scripts, not the eval runner.
- **Read-only, always.** The API only ever holds the `querypilot_ro` credential.
