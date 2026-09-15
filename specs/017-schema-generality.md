# 017 — Iteration 14: Schema generality

**Status:** drafted 2026-09-16, open questions unresolved.

Charter: `specs/000-project.md`. Raised by the user mid-B-11-planning, on the
grounds that deploying an artifact whose only proof of correctness is Chinook
is premature. Sequencing decision from that conversation: **this iteration
comes before B-11's custody matrix is acted on**, not after.

Scope, also decided in that conversation: *any Postgres schema*, not *any
database engine*. MySQL, SQL Server, SQLite and the rest stay out — the charter
already scopes this project to Postgres (`pg_catalog` introspection, sqlglot's
Postgres dialect, `SET TRANSACTION READ ONLY` semantics), and widening past
that is a rewrite of the data layer, not an extension of it.

---

## 1. What this iteration is for

The question that started this: *"why are we planning on deploying it when it
works only with chinook database, this have to work with any other database."*
The honest answer is neither "it doesn't" nor "it already does" — it is that
the **engine** was already built schema-general, largely by deliberate choice
recorded in comments that predate this conversation, but the **product** —
what an operator can configure, what the UI says, what the eval numbers claim
— has never been exercised against anything but Chinook.

### Already schema-general, confirmed by reading the code, not assumed

- `api/db/introspection.py` builds its `Schema` from three hand-written
  `pg_catalog` queries (`pg_class`, `pg_attribute`, `pg_constraint`), each
  passed through `execute_sql()`. No table or column name from Chinook appears
  in that module. This was made true at Iteration 8 T5 (charter §8 B-10,
  discharged) specifically to close a Gate-2 layering violation — genericity
  was a side effect of that fix, not its purpose, but it is real.
- `api/agent/prompts.py:234-239` keeps the foreign-key-target-column rendering
  that Chinook's schema makes redundant, with the comment *"Chinook is the
  fixture, not the deployment target: a real schema may key on a non-primary
  unique column."* The renderer was written anticipating a schema Chinook
  cannot express.
- `api/db/type_names.py` degrades an unmapped Postgres type to an uppercased
  base name rather than raising, specifically so an unfamiliar type does not
  make the schema unrenderable (charter §8 B-10 discharge note).
- `api/agent/glossary.py::render_glossary()` already takes a `terms` override
  parameter — the function was built to accept a different glossary. Nothing
  currently supplies one; every call site uses the default.

### Chinook-specific, also confirmed by reading the code

- `api/agent/glossary.py::GLOSSARY` is eight hardcoded music-store business
  terms, injected on **every** call when `glossary=True` (the default, per
  `prompts.py:456`, resolved Q-D). There is no config knob that swaps it for a
  different domain's terms, or for no terms at all.
- `evals/questions.yaml` — all 50 questions, both `dev` and `test` splits, are
  about Chinook. Every accuracy figure in `EVALS.md` is a claim about Chinook,
  and nothing in the repo has ever measured this loop against a second schema.
- `api/db/type_names.py`'s five base families are, in its own words, "what
  Chinook needs" — the B-10 discharge note names `text`, `boolean`, `date`,
  `jsonb`, `uuid`, arrays and domains as types "a real warehouse brings" that
  Chinook cannot exercise. The degrade-gracefully behavior is real; whether it
  produces a *usable* rendering for those types has never been measured.
- Gate 3's 1,000-column introspection cap is "guarded by a constructed test
  rather than by anything real" (same B-10 note) — no schema anywhere near that
  size has ever been introspected.
- `api/web/index.html:22` says *"Ask a question about the Chinook music
  store"* as the page's own description of what it does, not as a demo caption.
  `.env.example`, `README.md` and `db/fetch_chinook.sh` all assume Chinook is
  the database, with no documented path for pointing QueryPilot at a database
  that already exists and was not seeded by this repo.

So the gap is narrower than "rewrite the engine" and wider than "swap a
caption": it is *prove the engine's generality with a second schema, make the
one deliberately domain-specific piece (the glossary) configurable instead of
load-bearing, and stop the product from asserting Chinook is the only thing it
can be pointed at.*

---

## 2. Measurements

Taken 2026-09-16 against `main` at `0f76ac5` by reading the modules named
above; no live second-schema run has been done yet, which is exactly the gap
§1 identifies and §5 Q-A exists to close before this spec's plan is written.

| Claim | Evidence |
|---|---|
| Introspection has zero Chinook-specific identifiers | `grep -n "chinook" api/db/introspection.py -i` — the two hits are both comments about what Chinook *cannot* exercise, not schema names in the query text |
| Glossary costs tokens on every call regardless of question | `api/agent/prompts.py:39-45`, measured at T3 with `tiktoken`/`o200k_base`: 178 tokens per call, charged on all 50 eval questions |
| `get_schema()` is fully gated | Charter §8 B-10 discharge: 9 statements per call, all through `execute_sql()`, confirmed by `tests/test_type_names.py` banning `sqlalchemy.inspect` under `api/` by AST walk |
| The 1,000-column cap has never been hit by real data | Charter §8 B-10: *"Chinook's read is 69 rows, so this is guarded by a constructed test rather than by anything real"* |
| `jsonb`, `uuid`, arrays, domains have never been rendered from a live database | Charter §8 B-10: named explicitly as what Chinook cannot exercise; `type_names.py`'s five families are Chinook's measured answer, not a general one |

---

## 3. Acceptance criteria

- **AC1 — a second schema, not a second assertion.** Point `QUERYPILOT_DATABASE_URL`
  at a Postgres database that is not Chinook and was not shaped by this repo's
  `db/init/`. The app introspects it, answers at least a handful of hand-picked
  questions correctly, and does not crash on any type that schema contains.
  This is the acceptance criterion the rest of the spec exists to satisfy —
  everything else is either a prerequisite for it or a consequence of running
  it.
- **AC2 — the glossary is configurable, not load-bearing.** A deployment
  pointed at a non-Chinook database does not silently inject eight music-store
  definitions into every prompt. Chinook's glossary remains the shipped
  demo's default; an operator can supply a different one or none.
- **AC3 — the untested types get tested.** AC1's schema is chosen or built to
  contain at least `jsonb`, `uuid`, an array column and a domain, so
  `type_names.py`'s degrade-gracefully path is exercised by real data instead
  of asserted from its own docstring.
- **AC4 — the product stops claiming Chinook is the only answer.** UI copy,
  `.env.example` and the README's quickstart distinguish "this is the shipped
  demo, seeded with Chinook" from "this is what QueryPilot can only ever run
  against." The distinction is user-visible, not just true in the code (per
  the standing rule that architectural limitations get documented where a user
  reads them, not only disclosed in a task report).
- **AC5 — eval integrity is preserved, not blended.** Any accuracy measurement
  taken against the second schema is recorded separately from `EVALS.md`'s
  existing Chinook numbers, never averaged or merged into them. `EVALS.md`
  stays append-only and its existing rows stay Chinook-only claims.

---

## 4. Non-goals

- **Multi-engine support.** Resolved this conversation: Postgres only.
- **Auto-generated glossaries.** Having the model infer business-term
  definitions for an arbitrary schema is a materially different feature (and a
  materially different accuracy risk) from letting an operator supply one. Not
  in scope here.
- **A second full 50-question eval set, dev/test-split, to Iteration-1
  standards.** Proving the loop works on a second schema and proving it works
  *as well as* Chinook are different, much larger claims. This iteration
  targets the first.
- **Retroactively invalidating existing `EVALS.md` numbers.** They were always
  Chinook numbers; this iteration makes that scope explicit, not wrong.

---

## 5. Open questions

- **Q-A — which second schema?** Needs to be free, real (not synthetic-only,
  so it stresses schema quirks a hand-built fixture wouldn't), and small enough
  to seed in the same `db/init/`-style pipeline Chinook uses. *Lean: Pagila
  (the Postgres port of the Sakila DVD-rental sample) — different domain from
  a music store, similar size class to Chinook, and it already contains
  `jsonb`-adjacent and array-typed columns depending on version, which
  Chinook cannot offer. Needs confirming against the actual Pagila dump before
  committing, per "measure before specifying."*
- **Q-B — what's the glossary's default for a non-Chinook deployment?** *Lean:
  empty by default (`glossary=False` or an empty terms dict) unless
  `QUERYPILOT_GLOSSARY_FILE` (mirroring the `_FILE` secret-indirection pattern
  from Iteration 12) points at one. Chinook's own glossary ships as the
  demo's file, loaded the same way, so the demo's behavior doesn't change.*
- **Q-C — how much eval, not zero and not fifty?** *Lean: a small hand-written
  smoke set (5-10 questions) against the second schema, scored for "did it run
  and get the right row count" rather than tiered easy/medium/hard/expert —
  proving the loop survives a different schema, not re-deriving Iteration 1's
  dataset design for it.*
- **Q-D — does this get its own charter iteration-map entry and B-item?**
  *Lean: yes — add "Iteration 14: Schema generality" to §6 and close the loop
  by recording the gap this spec describes as a new backlog entry if any part
  of it is deferred rather than fully closed.*

Presented for review — the plan (`specs/017-schema-generality-plan.md`) gets
drafted once these four are answered.
