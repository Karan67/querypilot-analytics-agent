# QueryPilot

A Text-to-SQL analytics agent: ask a business question in plain English, get the
answer, the SQL that produced it, and a chart.

Read [`specs/000-project.md`](specs/000-project.md) first — it is the source of
truth for intent, scope, non-goals, and the safety rules that bind every
iteration.

**Current state: Iteration 12 (Production deployment).** `docker compose up` gives
you a working page at **<http://localhost:8000>** — sign in, ask a question,
get the answer, the SQL that produced it, and the agent's steps. Behind it: a
hand-written agent loop that reads its own execution errors and retries, a
four-gate safety layer nothing bypasses, and a 50-question benchmark with a
held-out split. Every endpoint but `/health` needs a credential, because
`POST /ask` spends real provider quota — see
[Who can ask](#who-can-ask-authentication). The image is now non-root,
multi-stage and deployable, with a daily spend ceiling in front of the agent
and TLS exercised through a `--profile tls` Caddy terminator — see
[Deployment](#deployment).

Every accuracy number lives in [`EVALS.md`](EVALS.md) with its caveats, and the
numbers are deliberately not repeated here — the honest reading of the held-out
result is *between 90% and 100%, measured once at 100%*, and a README is where
that nuance would die.

### What it costs, and what is measured

Every figure below is traceable to `EVALS.md` or to a spec's §2. None is an
estimate, and each carries the caveat that makes it true.

| | | where it comes from |
|---|---|---|
| one question, one user | **~750–1,250ms** | `010-hardening.md` §2.3 |
| under sustained load | **up to ~10,400ms**, silently | same |
| of which the model | **94.2%** of wall clock, up to 98.4% | `010-hardening.md` §2.2 |
| tokens per question | median **1,078**, range 1,047–1,256 | `010-hardening.md` §2.5 |
| provider calls per question | **one**, across all twelve measured | same |
| a repeated question | **0 tokens** | the answer cache; caveat below |
| free-tier ceiling | **200,000 tokens/day** ≈ 180 questions | `010-hardening.md` §2.5 |
| schema introspection | **9 statements, 29.0ms** | `011-ship.md` B-10, Iteration 8 T5 |
| the test suite | **1,547 tests** | `docker compose --profile tls up -d`, then `pytest` |
| without a database | **1,081 of them, ~41s** | `pytest -m "not needs_db"` — no Docker, no Postgres |

Three of those need their caveat stated rather than footnoted:

- **Latency is two numbers, not one, and the second one is the honest one.**
  §2.3 measured a fourteen-fold climb *with the question held constant* — the
  provider slows under sustained use with no error and no header to explain it.
  An earlier spec reported 1.20–2.51s; that sample was taken on a cold bucket
  and describes the fast mode only. This is why `/history` records `total_ms`
  and `provider_ms` apart.
- **0 tokens for a repeated question does not mean the answer is current.** The
  cache key is the question plus the schema and prompt fingerprints, so a new
  *column* invalidates it and a new *row* does not. Chinook is static, so this
  never bites here — and the page says so rather than leaving it to be found.
- **~180 questions a day is a ceiling on a free tier**, not a capacity claim.
  The per-minute bucket allows about seven in any sixty seconds before the slow
  mode above begins.

**There is no accuracy number in this table.** That is deliberate: `EVALS.md`
holds them with the caveats they need, and a percentage on a README outlives
the sentence that qualifies it.

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
  "history": { "writable": true, "error": "" },
  "auth": { "configured": true, "error": "" }
}
```

`user` must read `querypilot_ro`. If it reads anything else, the API is holding
a privileged credential and Gate 1 of the safety layer is not in place.

`auth` reports whether the credential map parsed. `/health` is the **only**
endpoint that answers without a credential, so if everything else is returning
`401` this field is where the reason is: `configured: false` with a message
naming the variable to set means the gate is refusing because it has nothing to
check against, not because your password is wrong.

`history` reports whether the question log can be written. A failure there shows
up as `degraded_history` and **still returns 200**: recording a question and
answering it are independent, and reporting the service as down because logging
broke would be the cascade that arrangement exists to prevent.

Then open **<http://localhost:8000>**. The browser will ask for a name and
password: that is HTTP Basic, and the values come from `QUERYPILOT_USERS` in
your `.env` — `analyst` / `localdevonly-change-me` if you copied `.env.example`
unchanged. **Change it before this is reachable by anyone else**, and read
[Who can ask](#who-can-ask-authentication) for what this does and does not
protect.

Ask something:

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
curl -u analyst:localdevonly-change-me \
  -X POST http://localhost:8000/ask \
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

Under each answer is a **Was this answer useful?** control, and the mark it
stores shows up on the history page:

```bash
curl -u analyst:localdevonly-change-me \
  -X POST http://localhost:8000/feedback \
  -H "Content-Type: application/json" \
  -d '{"id":"<the id from /ask>","rating":1,"note":"optional"}'
```

`rating` is `-1` or `1` and nothing else; an unknown `id` is a `404`, because a
mark attaches to an *answer* and not to a question. Marks are append-only, so
two people can disagree about one answer and both marks survive.

**Nothing aggregates them** — no count, no rate, no score, anywhere in the API
or the pages. That is a deliberate constraint rather than an unfinished
feature: at the time this was built the store held 29 answers with **zero**
failures in it, so any proportion computed over it would read "100% good" on a
sample containing nothing to be wrong about. The marks are collected; drawing
conclusions from them waits for enough of them to mean something.

Asking the same question twice costs **zero** tokens the second time, and the
page says so rather than presenting a reused answer as a fresh one. The key is
the exact question text plus fingerprints of the schema and the prompt, so a
schema change or a prompt edit invalidates it — a change to the *data* does not.

```bash
curl -u analyst:localdevonly-change-me http://localhost:8000/quota
```

reports what the provider last said about its limits. It is worth knowing that
the free tier does not refuse when its per-minute token bucket runs low — it
**slows down**, from about 750ms to as much as 10s, with no error and no header
a user would ever see. That is what this endpoint and the page's banner exist to
explain. It reports two buckets and not three: the 200,000-tokens-per-day limit
appears in no response header at all, only in the body of a 429.

---

## Who can ask (authentication)

Every endpoint except `/health` requires **HTTP Basic** credentials. It exists
for one reason: `POST /ask` spends real provider quota, and an unauthenticated
one is an open invitation to spend somebody else's — 1,208 tokens per anonymous
question, against a measured ceiling of 200,000 tokens a day.

Identities live in `QUERYPILOT_USERS`, a JSON object of name to secret:

```bash
QUERYPILOT_USERS='{"analyst":"<a long random string>","ops":"<another>"}'
```

Add a caller by adding an entry. Rotate a secret by changing it — the value is
read per request, so no rebuild and no code change. Generate one with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.

### What this protects, and what it does not

**It is a spend gate, not transport security.** Basic sends the secret
base64-encoded on every request, which is encoding and not encryption. Over
plain HTTP anyone on the network path can read it and replay it. TLS is a
deployment concern and is tracked separately; until it exists, treat these
credentials as protecting a localhost or trusted-network service and nothing
more.

**There is no per-user separation.** Every signed-in identity has the same
capabilities and sees the same data — including `/history`, where **every
signed-in user sees every question anybody has asked**, with its SQL and its
cost. Names exist so that a credential can be revoked individually, not so that
two people's work is kept apart. The answer cache is shared for the same reason:
the answer to a question does not depend on who asked it.

**There is no logout.** A browser holds Basic credentials until the tab closes,
and a wrong password entered into the dialog is awkward to clear. That is the
price of adding a gate with no login page, no cookie, no session store and no
new dependency.

**Unset means locked, not open.** With `QUERYPILOT_USERS` empty or malformed,
every protected route returns `401`. **To an authenticated caller**, `/health`
reports `auth.configured: false` with a message naming the variable — a
deployment that forgets to set it is refused, never silently public, which is
the failure mode this design spends its complexity avoiding. An **anonymous**
caller of `/health` no longer sees that message at all: since Iteration 12
(§"Deployment" below), `/health` tells a stranger only `{"status": "ok"}`, and
that includes the one case where this genuinely costs something — a deployment
whose `QUERYPILOT_USERS` is itself broken has no credential that can ever
authenticate, so this particular diagnostic is now only found in the container
log, not from outside.

**The secret is never logged.** It does not appear in an error message, a trace,
a history row, or the page — the same rule `GROQ_API_KEY` has. A refused request
gets one identical `401` whatever went wrong, so the response cannot be read as
a hint about whether the name exists.

---

## Deployment

Iteration 12 (`specs/015-production-deployment.md`) closed the engineering half
of B-11: the artifact is now something a reasonable platform would accept, and
none of it required a cloud account. Iteration 15
(`specs/019-production-deployment.md`) then closed the custody half — which
host, whose managed Postgres, whose domain — by user ruling rather than by
code, and produced the two artifacts described below. Neither iteration
creates a live Render service or Neon project on its own; that step is still
yours to take, using them.

**What changed, and what it buys:**

- **The image runs as a fixed non-root uid**, is built in two stages so no
  compiler or `pip` cache reaches the runtime layer, binds `$PORT` when a
  platform sets it, and carries its own `HEALTHCHECK` rather than depending on
  `docker-compose.yml` to supply one.
- **Every response carries a security policy**, including the `401` a wrong
  password produces: a `Content-Security-Policy` derived from what `api/web/`
  actually loads (and asserted against it, so the policy cannot drift silently
  looser than the page), `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`, and a `Strict-Transport-Security` header that is emitted
  **only** once a trusted reverse proxy confirms the request arrived over
  HTTPS — never unconditionally, because a laptop serving plain
  `http://localhost` that emitted it even once would make every other project
  ever served from that host unreachable in that browser for a year.
- **TLS is exercised, not asserted.** `docker compose --profile tls up -d`
  puts [Caddy](https://caddyserver.com/) in front of the API with its own
  internal CA, so a real HTTPS request completes end to end with no domain, no
  ACME round trip, and no network access. It is a development profile, not a
  production TLS story — see the matrix below.
- **Both secrets can be read from a file instead of an environment variable.**
  `GROQ_API_KEY_FILE` and `QUERYPILOT_USERS_FILE` point at a path; the file
  wins when both are set, and a configured-but-unreadable path fails **closed**
  rather than silently falling back to the environment variable. This matters
  because an environment variable is part of a container's configuration:
  `docker inspect` prints it in full to anyone holding the Docker socket, and
  every child process inherits it. A file does not have that problem.
- **A daily question ceiling now sits in front of the agent** — 50 per
  identity, 150 across the deployment, both configurable, both resetting at
  UTC midnight. Authentication (Iteration 10) answers *who* may spend the
  project's quota; this answers *how much*, which authentication alone never
  did. A caller who is refused is told which ceiling they hit, because a
  message that did not distinguish "you" from "everyone" would read as a bug
  the first time the less common reason fired.
- **`/health` tells an anonymous caller only `{"status": "ok"}`.** Everything
  it used to say to anyone — the database role, the database name, the table
  count, whether the credential map parsed — now requires the same credential
  every other route does. A container orchestrator's own healthcheck never
  presents one and never needs to: it reads the HTTP status code, never the
  body, and always has.
- **Postgres is no longer published on every network interface by default** —
  `127.0.0.1` only, configurable via `POSTGRES_BIND_HOST` for the one
  legitimate exception, a database client on another machine, deliberately.

**What Iteration 12 deliberately did not do**, and what Iteration 15 then
resolved. Nothing in Iteration 12 chose a host, a domain, a DNS provider, or a
managed Postgres — those are custody decisions, whose card, whose account,
whose name on the bill — and `specs/015-production-deployment.md` §7 laid
them out as a matrix rather than resolving them. Iteration 15
(`specs/019-production-deployment.md`) is that decision, ruled by the user
rather than assumed:

| Decision | Resolved |
|---|---|
| API host | Render, free tier |
| Managed Postgres | Neon, one project, Postgres 18, two databases (`chinook`, `pagila`) |
| TLS issuance | Render's own platform-managed TLS |
| Domain and DNS | Render's free `*.onrender.com` subdomain — no registrar |
| Container registry | none — Render builds `api/Dockerfile` directly from this repository |

And the sharpest of B-11's original three blockers is still exactly where it
was: **whose LLM key** a public deployment spends, and how hard the credential
gate and the spend ceiling hold against a stranger who finds the URL. A
ceiling bounds the bill; it does not make the URL safe to publish to strangers
you have not decided to trust. The deployed instance spends the project's own
free-tier Groq key, gated by the same `QUERYPILOT_USERS` credential map as any
other deployment of this image — Iteration 10's answer, unchanged.

### Deploying to Render + Neon

**What this does not do, stated up front.** History (`GET /history`) is
**ephemeral** on this deployment: Render's free-tier web services have no
persistent disk, so `/data/querypilot.db` is reset on every restart,
redeploy, or scale-to-zero. This is a stated limitation, not an oversight —
see `specs/019-production-deployment.md` §7 Q-A. `docker-compose.yml` and
local development are unaffected; this trade-off is specific to the Render
leg.

Two artifacts do the work:

- **[`render.yaml`](render.yaml)** — the Render Blueprint. Builds the same
  `api/Dockerfile` and `./api` context `docker-compose.yml`'s `api` service
  builds; declares every environment variable the deployed instance needs,
  with the secret-shaped ones (`GROQ_API_KEY`, `QUERYPILOT_USERS`, and both
  database URLs) left for Render's own encrypted dashboard rather than
  committed here.
- **[`deploy/neon/README.md`](deploy/neon/README.md)** — a one-time, by-hand
  runbook for seeding Neon. Nothing under `db/init/` or `db/init-pagila/`
  runs automatically against a managed Postgres, so this walks through
  creating the `chinook` and `pagila` databases, loading each dataset with
  the same `psql -f` commands the local init scripts run inside their
  containers, and provisioning the same read-only role (`querypilot_ro`) Gate
  1 requires everywhere else.

`docker-compose.yml` is untouched by any of this — the Render leg runs only
the `api` service, against Neon instead of the local `db`/`pagila-db`
containers, and `docker compose up -d` still works exactly as documented
above.

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

### Bring your own database

Chinook is this repo's shipped demo, not a requirement. `api/db/introspection.py`
reads whatever schema is live in the database `QUERYPILOT_DATABASE_URL` points
at — three hand-written `pg_catalog` queries, no table or column name from
Chinook anywhere in that path (`specs/017-schema-generality.md` confirmed this
by reading the code, then proved it against a second, unrelated schema —
Pagila, a DVD-rental sample this repo did not seed — with `evals/pagila_smoke.py`
and `evals/PAGILA_SMOKE.md`).

To point QueryPilot at a database you already have, rather than the one
`db/init/` builds:

1. Set `QUERYPILOT_DATABASE_URL` to a role that can `SELECT` there and nothing
   else — QueryPilot's safety layer assumes a read-only connection; it does not
   create one for you the way `db/init/03_readonly_role.sh` does for the demo.
2. `QUERYPILOT_GLOSSARY_FILE` is unset by default, which means **no business
   terms are injected** — correct for a database this repo has no domain
   knowledge about. If your schema has the same kind of population ambiguity
   Chinook's glossary exists to resolve (which rows count as "active", which
   column is authoritative when two disagree — `evals/PAGILA_SMOKE.md` records
   a real example, `customer.active` vs `customer.activebool` disagreeing on 43
   Pagila rows), point this at your own JSON file of `{"term": "definition"}`
   pairs, the same shape as `api/glossary/chinook.json`.
3. Everything else — the agent loop, the validator, the prompt renderer — needs
   no configuration change. It was built to read the catalog, not to assume
   Chinook's.

What this does **not** cover: a database engine other than Postgres. The
`pg_catalog` queries, the sqlglot validator's dialect and the
`SET TRANSACTION READ ONLY` semantics are Postgres-specific, and widening past
that is a different, much larger project.

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

### Continuous integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs the whole suite on
every push and pull request, against a real Postgres brought up by
`docker compose` — the same command this README gives, so the two cannot drift.
It needs **no secret**, so it runs on a fork's pull request, and it pins the
interpreter to the version the image ships.

The interesting part is what stops it lying. Pointed at an unreachable database
the suite **skips every test and exits 0** — the right behaviour for a developer
with the stack down, and a green build that verified nothing for a pipeline. Two
independent things prevent that:

- `QUERYPILOT_TESTS_REQUIRE_DATABASE=1` turns that skip into a failure, inside
  the fixture where the skip lives.
- [`ci/require_executed_tests.py`](ci/require_executed_tests.py) asserts a floor
  on tests *actually executed*, read from `--junitxml`, for the day somebody
  deletes the first one.

Both were verified by breaking the database on a throwaway branch and watching
the build go red — once from the flag, once from the floor alone.

The three live provider tests are excluded by `--ignore` rather than left to
skip on a missing key, so **run them by hand before a release**:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_llm_live.py -q -s
```

They cost real tokens and print what the model actually did.

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
  main.py       FastAPI app; GET /, /history, /health, /quota; POST /ask, /feedback
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
