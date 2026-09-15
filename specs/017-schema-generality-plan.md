# 017 — Iteration 14 plan: Schema generality

Spec: `specs/017-schema-generality.md`, Q-A through Q-D resolved 2026-09-16:
Pagila, `QUERYPILOT_GLOSSARY_FILE` (empty default), a 5-10 question smoke set,
formalize as charter Iteration 14. UI redesign stays a separate, later spec.

---

## Approach

Three independent tracks, ordered so the riskiest unknown (does Pagila actually
exercise what Chinook can't, and does the existing engine survive it
unmodified) is resolved first, before time is spent on the config and docs
work that only matters if it does.

1. **Stand up Pagila alongside Chinook, not instead of it.** A second compose
   service (`pagila-db`) behind a new `pagila` profile, mirroring the existing
   `tls` profile precedent (`deploy/Caddyfile`'s pattern of "exercised, not
   assumed"). Chinook stays the default stack; `docker compose --profile
   pagila up -d pagila-db` is opt-in, same shape as `--profile tls`.
2. **Make the glossary a file, not a constant.** `QUERYPILOT_GLOSSARY_FILE`
   read the same way `config.read_secret_file()` already reads a secret path —
   reusing that function directly rather than writing a second file-reader,
   since its error handling (missing file, unreadable, bad encoding, logged
   without contents) is exactly what a glossary file needs too.
3. **Prove it, record it separately, tell the product about it.** A standalone
   smoke script against Pagila, a new append-only `evals/PAGILA_SMOKE.md` (never
   `EVALS.md` — AC5), and copy changes so the UI and README stop asserting
   Chinook is the only database this can be pointed at.

---

## Decisions surfaced while planning

- **D-1 — an empty glossary renders nothing, not an empty header.**
  `render_glossary()` today always prints `GLOSSARY_HEADER` plus whatever
  entries exist. With `QUERYPILOT_GLOSSARY_FILE` unset, `terms` is `{}`; the
  question is whether `glossary=True` then still injects a bare "Business
  terms — use these definitions, not your own:" header with nothing under it.
  *Recommendation: no — when the resolved terms dict is empty, `prompts.py`
  skips the glossary block entirely, same code path as `glossary=False`.* An
  empty header costs tokens and tells the model to defer to definitions that
  don't exist.
- **D-2 — a misconfigured glossary file fails open, not closed.**
  `auth.py` fails closed on a bad `QUERYPILOT_USERS` because an open perimeter
  is a security hole. A missing or malformed glossary file is not — it just
  means less domain guidance. *Recommendation: log the error the same way
  `read_secret_file()` already does, and proceed with an empty glossary rather
  than refusing every question.* Refusing `/ask` because a business-term file
  had a typo would make a config mistake outrank a safety mistake in how hard
  it fails, which is backwards.

Both are stated here for ruling alongside the task table below, not assumed.

---

## File list

| File | Change |
|---|---|
| `db/fetch_pagila.sh` (+ `.ps1`) | New. Mirrors `db/fetch_chinook.sh`: downloads Pagila's schema + data dumps from `devrimgunduz/pagila` (upstream repo for the Postgres port of Sakila) into `db/pagila-seed/`. Exact raw-file URLs confirmed at T1, not guessed — Pagila ships schema and data as two files, unlike Chinook's one. |
| `db/init-pagila/01_load_pagila.sh` | New. `db/init/01_load_chinook.sh` adapted: loads both Pagila files in order, same `ON_ERROR_STOP=1` / table-count assertion shape. |
| `db/init-pagila/02_readonly_role.sh` | Not a new file — the *same* `db/init/03_readonly_role.sh` bind-mounted a second time into the Pagila init directory. It is already fully parameterized by env vars with no Chinook content, so mounting one file twice avoids a second copy of security-relevant logic to keep in sync. |
| `docker-compose.yml` | New `pagila-db` service, `profiles: [pagila]`, port `5433` (Chinook's `db` keeps `5432`), its own named volume, healthcheck mirroring `db`'s. Reuses `QUERYPILOT_RO_USER`/`QUERYPILOT_RO_PASSWORD`/`QUERYPILOT_STATEMENT_TIMEOUT` — different container, so no collision with Chinook's role of the same name. |
| `api/config.py` | No change — `read_secret_file()` is reused as-is for D-2's error handling. |
| `api/agent/glossary.py` | Add `current_glossary_terms()`, memoized like `auth.current_identities()`, reading `QUERYPILOT_GLOSSARY_FILE` via `config.read_secret_file()` and `json.loads`, defaulting to `{}`. `GLOSSARY` (the Chinook constant) becomes the *content* of a shipped file, not the code path's default. |
| `api/glossary/chinook.json` | New. The existing eight terms, moved out of Python and into the file format `QUERYPILOT_GLOSSARY_FILE` reads — so the demo's behavior is unchanged, just relocated. |
| `docker-compose.yml` (api service env) | `QUERYPILOT_GLOSSARY_FILE: /app/glossary/chinook.json` (or equivalent baked-image path) set by default, so the shipped demo's glossary behavior does not change. |
| `api/agent/prompts.py` | The `if glossary:` branch (~line 488) calls `current_glossary_terms()` instead of `render_glossary()` with no args, and skips the block entirely when the result is empty (D-1). |
| `evals/pagila_smoke.py` | New. 5-10 hand-written questions against Pagila, run through `answer()` directly (not through `run_evals.py`'s tiered/glossary-arm machinery, which is built for the Chinook dataset shape). Asserts `ok`, that the SQL executes, and a hand-verified expected row count per question. |
| `evals/PAGILA_SMOKE.md` | New, append-only like `EVALS.md` but a separate file — AC5 requires the numbers never blend. |
| `api/web/index.html` | Line 22's "Ask a question about the Chinook music store" becomes generic copy; a distinct, clearly-labeled line states the shipped demo is seeded with Chinook. |
| `README.md` | New "Bring your own database" section: `QUERYPILOT_DATABASE_URL` and `QUERYPILOT_GLOSSARY_FILE`, pointing at an existing Postgres database that was not seeded by this repo. |
| `.env.example` | Comment on `POSTGRES_DB=chinook` marking it as the demo default, not a requirement; document `QUERYPILOT_GLOSSARY_FILE`. |
| `specs/000-project.md` §6 | New table row for Iteration 14, linking `017-schema-generality.md`, matching the shape of rows 11-13 (a row, not a full blockquote — that convention was already dropped after Iteration 10). |
| `tests/test_glossary_config.py` | New. Unit tests, no DB needed: unset env → `{}`; set to a missing path → `{}` + logged error, not a raise; a valid file → parsed terms; malformed JSON → `{}` + logged error (D-2). |

---

## Test plan

- `tests/test_glossary_config.py` — the new unit coverage above, all `not
  needs_db`.
- Mutation check on D-1: temporarily remove the "skip when empty" guard in
  `prompts.py`, confirm a test asserting no bare header appears goes red, then
  restore it.
- Mutation check on D-2: temporarily make a bad glossary file raise instead of
  logging-and-defaulting, confirm a test asserting `/ask` still answers with a
  broken `QUERYPILOT_GLOSSARY_FILE` goes red, then restore it.
- `evals/pagila_smoke.py` is itself the AC1/AC3 verification — it is run by
  hand against the `pagila` profile, not wired into the default `pytest`
  collection, since it needs a second live database most contributors won't
  have up. Documented in README alongside the `pagila` profile.
- No change to `tests/test_auth.py`, `test_glossary.py` (Chinook's sixteen
  measured queries), or anything in `evals/` for the existing dataset — AC5
  means those stay exactly as they are.

---

## Task decomposition

| # | Task | Produces | Pause after? |
|---|---|---|---|
| T1 | Fetch scripts + `db/init-pagila/` + `pagila-db` compose service | A second Postgres instance loadable with `docker compose --profile pagila up -d pagila-db` | No |
| T2 | Bring `pagila-db` up, confirm live: table count, and that `tsvector`/array/domain columns are actually present in `public` | Real measurements replacing this spec's §2 placeholder — confirms or corrects the Q-A lean | **Yes** — report the real numbers before writing a line of generality-handling code against them |
| T3 | Point `answer()` at Pagila (temporary `QUERYPILOT_DATABASE_URL` override, not the running demo container), hand-verify 5-10 questions, write `evals/pagila_smoke.py` + `PAGILA_SMOKE.md` | AC1, AC3, AC5 satisfied | **Yes** — this is the spec's central claim; report pass/fail honestly before touching the glossary or UI work |
| T4 | `current_glossary_terms()`, `chinook.json`, `prompts.py` wiring, D-1/D-2 behavior, `test_glossary_config.py`, both mutation checks | AC2 satisfied | No |
| T5 | UI copy, README section, `.env.example` comment | AC4 satisfied | No |
| T6 | Charter §6 row | Q-D discharged | No |
| T7 | Full suite (`pytest -m "not needs_db"`, then the database lane), report | Close-out | Yes — final report |

---

## Risks

- **Pagila's actual column types might not include everything Q-A assumed.**
  T2 exists specifically to catch this before any code is written against an
  assumed type inventory — if it's short on domains or arrays, the plan's
  fallback is a small hand-added column on a Pagila table for the one type
  missing, documented as a deliberate fixture patch, rather than abandoning
  Pagila.
- **Two Postgres containers on one laptop is real resource cost.** The
  `pagila` profile keeps this opt-in; nobody running the default stack pays
  for it.
- **`answer()` called directly (T3) bypasses whatever the eval ledger or
  ordinary request path assumes about a single configured database.** Worth
  confirming at T3 that no global/module-level state (e.g. a cached schema
  keyed only by connection, not by DSN) leaks between a Chinook call and a
  Pagila call in the same process — this is exactly the shape of bug B-10/B-14
  already found once.

---

## Task table (compact)

| # | Task | AC/Q closed | Pause |
|---|---|---|---|
| T1 | Pagila fetch + compose profile | infra for AC1 | — |
| T2 | Bring Pagila up, measure real types | corrects/confirms Q-A | **yes** |
| T3 | Point agent at Pagila, smoke questions, `PAGILA_SMOKE.md` | AC1, AC3, AC5 | **yes** |
| T4 | Configurable glossary (`QUERYPILOT_GLOSSARY_FILE`, D-1, D-2) | AC2 | — |
| T5 | UI copy + README + `.env.example` | AC4 | — |
| T6 | Charter §6 row | Q-D | — |
| T7 | Full suite + close-out report | — | yes |

Waiting on: D-1, D-2 ruling, and approval to start T1.
