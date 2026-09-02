# QueryPilot

A Text-to-SQL analytics agent: ask a business question in plain English, get the
answer, the SQL that produced it, and a chart.

Read [`specs/000-project.md`](specs/000-project.md) first — it is the source of
truth for intent, scope, non-goals, and the safety rules that bind every
iteration.

**Current state: Iteration 3 (Evaluations).** The stack comes up under Docker
Compose, the four tools work, single-shot generation answers questions
end-to-end through the safety layer, and there is now a **measured baseline**:
see [`EVALS.md`](EVALS.md). Iteration 4 replaces single-shot with the agent
loop, and its success criterion is a measurable jump against that number.

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
  }
}
```

`user` must read `querypilot_ro`. If it reads anything else, the API is holding
a privileged credential and Gate 1 of the safety layer is not in place.

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

The benchmark scores the agent against 40 hand-written reference queries. It
needs the database up and `GROQ_API_KEY` in `.env`.

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
  main.py       FastAPI app; /health lives here
  agent/        orchestrator, tools, prompts (Iterations 1–4)
  safety/       sqlglot AST gate (Iteration 1)
  db/           read-only engine
db/             dataset fetcher + Postgres init scripts
frontend/       Next.js chat UI (Iteration 6)
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
