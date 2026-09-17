# 019 — Iteration 15 plan: The custody half of B-11

Status: **DRAFT — presented 2026-09-17**, §4 decisions open for approval ·
Created: 2026-09-17

Spec: `specs/019-production-deployment.md`, AC1–AC11. Discharges the custody
half of **B-11** by artifact (blueprint + runbook), not by a live account —
see the spec's §1 refusal.

---

## 1. Approach

### 1.1 Four tasks, ordered so each one's inputs already exist

**T1 (blueprint) before T2 (runbook) before T3 (docs) before T4
(verification).** AC8 requires the runbook to name the exact environment
variables the deployed instance reads; those names only become fixed once
T1's `render.yaml` declares them (AC3). T3's README section describes both
artifacts, so it comes after both exist. T4 is last because "the suite is
still green and `docker-compose.yml` is untouched" is a claim about the
finished state, not something to check mid-task.

### 1.2 What does not move

This iteration adds files; it does not touch the request path. No task here
runs anywhere near `execute_sql()`, the provider boundary, or
`docker-compose.yml`'s existing services — spec §5 and §4 already commit to
that, and T4 is what confirms it rather than assumes it.

### 1.3 Mutation testing, scoped to what's actually new

There's no request-path defense to mutate this iteration — the "defenses"
are structural: a blueprint that must build the real Dockerfile, a runbook
that must name both databases, docs that must say what the deployment does
and doesn't do. Each gets a test that fails when the property it guards is
removed, verified by deliberately breaking it once (§2–§3 below name the
mutation for each).

---

## 2. T1 — the Render blueprint

`render.yaml` at the repo root, one `web` service:

```yaml
services:
  - type: web
    name: querypilot
    env: docker
    dockerfilePath: ./api/Dockerfile
    dockerContext: ./api
    healthCheckPath: /health
    envVars:
      - key: QUERYPILOT_DATABASE_URL
        sync: false
      - key: QUERYPILOT_PAGILA_DATABASE_URL
        sync: false
      - key: GROQ_API_KEY
        sync: false
      - key: QUERYPILOT_USERS
        sync: false
      - key: QUERYPILOT_LLM_PROVIDER
        value: groq
      - key: QUERYPILOT_LLM_MODEL
        value: openai/gpt-oss-120b
      - key: QUERYPILOT_GLOSSARY_FILE
        value: /app/api/glossary/chinook.json
      - key: QUERYPILOT_DAILY_IDENTITY_LIMIT
        value: "50"
      - key: QUERYPILOT_DAILY_GLOBAL_LIMIT
        value: "150"
      - key: QUERYPILOT_HISTORY_PATH
        value: /data/querypilot.db
```

`dockerContext`/`dockerfilePath` mirror `docker-compose.yml`'s
`api.build.context: ./api` exactly — same Dockerfile, same context, so the
image Render builds is the image `docker compose build api` builds. No
`_FILE` variable and no `QUERYPILOT_SECRETS_DIR` appear (spec Q-A/AC4): the
Render leg reads secrets from its own encrypted env var store, not a mounted
file. No `QUERYPILOT_TRUSTED_PROXIES` entry, since a fresh value is only
needed if Render's edge sets a header this project doesn't already trust by
default (loopback + RFC-1918) — left absent rather than guessed at, and the
runbook (T2) notes it as a one-time check to make after the first live
request.

`tests/test_render_blueprint.py` (new, hermetic — no `needs_db`, no
network):

- **AC1/AC2**: parses `render.yaml` with `yaml.safe_load` (already a dev
  dependency, `api/requirements-dev.txt:16`) and asserts the one service's
  `env`, `dockerfilePath`, `dockerContext`, `healthCheckPath` — not a grep
  over the raw text, per the standing rule that absence/presence assertions
  read parsed structure.
  *Mutation: change `healthCheckPath` to `/healthz` → test goes red.*
- **AC3**: asserts every `envVars[].key` needed by `api/config.py`'s secret
  readers and `api/targets.py`'s target registration is present.
  *Mutation: delete the `QUERYPILOT_PAGILA_DATABASE_URL` entry → red.*
- **AC4**: asserts no key or value in the parsed YAML contains `/run/secrets`
  or ends in `_FILE`.
  *Mutation: add `QUERYPILOT_USERS_FILE` → red.*
- **AC5**: resolves `dockerfilePath` and `dockerContext` relative to the repo
  root with `pathlib` and asserts both exist on disk.
  *Mutation: point `dockerfilePath` at `./api/Dockerfile.nope` → red.*
- **New, not in the AC list but guarding Q-E (spec §7)**: asserts
  `render.yaml` contains no reference to `docker-compose.yml` or its service
  names, and — in a second, existing test file — that
  `docker-compose.yml` contains no reference to `render.yaml`. This is the
  durable guard for "additive, not coupled" now that both files exist side by
  side.
  *Mutation: add a stray `# see render.yaml` comment to either file → red.*

---

## 3. T2 — the Neon runbook

`deploy/neon/README.md` (new). An ordered, numbered procedure, not a script —
spec AC7 is explicit that this touches an account the project doesn't hold,
so nothing here runs unattended:

1. Create one Neon project, Postgres 18.
2. Create databases `chinook` and `pagila` inside it.
3. `psql "<chinook connection string>" -f db/init/01_load_chinook.sh`-derived
   SQL, then the read-only role grant from `db/init/03_readonly_role.sh`,
   naming the exact `psql` invocations rather than paraphrasing them.
4. The Pagila-facing equivalent, naming `db/init-pagila/01_load_pagila.sh`
   and its role wrapper.
5. A table mapping each resulting connection string to the Render env var it
   becomes (`QUERYPILOT_DATABASE_URL`, `QUERYPILOT_PAGILA_DATABASE_URL`) —
   AC8.

`tests/test_neon_runbook.py` (new, hermetic): reads the file as text and
asserts — after collapsing whitespace, per the standing trap about phrases
split across a line break — that it names both database identifiers
(`chinook`, `pagila`), both target env var names, and the phrase "one-time"
or "manual" near the seeding steps (AC7's disclosure). This is a weaker
guarantee than T1's parsed-structure tests, appropriately: the file is prose
instructions for a human, not data a program consumes.
*Mutation: remove the `pagila` seeding section entirely → red.*

---

## 4. T3 — README and charter

- `README.md` gains "Deploying to Render + Neon", stating the five §7
  decisions in the same plain terms the spec uses, plus the ephemeral-history
  limitation up front rather than buried (AC9). References `render.yaml` and
  `deploy/neon/README.md` rather than duplicating their content.
- `specs/000-project.md` §8's B-11 row and its §9 narrative entry are
  updated: custody half discharged by user ruling, citing this spec, in the
  same terms `specs/015`'s AC23 used for the engineering half (AC10).
- `HANDOFF.md` is **not** touched by this task — it's session-transient
  status, not project documentation, and the charter update is the
  authoritative record.

`tests/test_readme_render_deploy_section.py` (new, hermetic): asserts the
heading text is present in `README.md` and that it appears before the
existing "Commands" section closes (i.e., it's a real section, not a
dangling fragment) — a heading-presence check is safe here since this is
prose markdown, not HTML/JS where the comment-stripping trap applies.
*Mutation: delete the heading but leave the body text → red* (proves the
test checks structure, not just any occurrence of "Render" in the file).

No new test for the charter edit — `specs/000-project.md` isn't part of any
existing structural-test surface (015's AC23 wasn't tested either), and
adding one here would be the first instance of testing prose charter text
rather than code.

---

## 5. T4 — verification

Not a new file. In order:

1. `.venv/Scripts/python.exe -m pytest -m "not needs_db"` — confirms the
   three new hermetic test files pass and nothing else regressed.
2. `.venv/Scripts/python.exe -m pytest` against the live stack (`docker
   compose --profile pagila up -d` first, since T1's blueprint references
   the Pagila target) — confirms the database-dependent lane is unaffected.
3. `git diff --stat docker-compose.yml` — expected empty, confirming AC11
   directly rather than by inspection.
4. `python -c "import yaml; yaml.safe_load(open('render.yaml'))"` outside
   pytest, as a sanity check that the file is valid YAML independent of the
   test suite's own parser call.

Nothing here is committed as a new automated gate beyond the three test files
above; step 3 is a one-time confirmation for this PR; a permanent `HEAD`-diff
test would go stale the moment this branch merges; it becomes a durable
guarantee to the extent T1's own "no cross-reference" tests keep it that way
afterward.

---

## 6. Files and decomposition

| Task | New files | Existing files touched | AC |
|---|---|---|---|
| T1 | `render.yaml`, `tests/test_render_blueprint.py` | `docker-compose.yml` (read-only, test asserts no reference *to* it) | AC1–AC5, independence guard |
| T2 | `deploy/neon/README.md`, `tests/test_neon_runbook.py` | — | AC6–AC8 |
| T3 | `tests/test_readme_render_deploy_section.py` | `README.md`, `specs/000-project.md` | AC9–AC11 |
| T4 | — | — (verification only) | AC11 (confirmed) |

---

## 7. Decisions

- **D-1 — Test strength matches artifact type.** T1's `render.yaml` gets
  parsed-structure tests (it's data a program reads); T2/T3's prose gets
  presence-after-whitespace-collapse tests (it's data a human reads). Mixing
  the two — e.g., demanding AST-equivalent rigor from a runbook — would
  either be impossible (there's no AST for "did the human read this
  correctly") or would produce a brittle test asserting exact wording rather
  than the property that matters.
- **D-2 — No permanent `docker-compose.yml`-unchanged test.** A test
  comparing the file to a frozen snapshot goes stale at the next legitimate
  edit to that file, for any reason. The independence guard (render.yaml and
  docker-compose.yml don't reference each other) is the durable version of
  the same concern; the literal "diff is empty" check is a one-time PR
  confirmation (T4 step 3), matching how `specs/015`'s AC11 (no default
  credential) was verified by inspection rather than a snapshot test.
- **D-3 — `QUERYPILOT_TRUSTED_PROXIES` ships unset in the blueprint.**
  Setting it correctly needs Render's actual edge IP behavior, which is only
  observable after a real deploy. Shipping a guess risks being wrong in the
  direction that matters (trusting a header from an untrusted source);
  shipping unset keeps the existing loopback/RFC-1918 default, which is
  conservative. The runbook (T2) notes checking this after first deploy
  rather than the spec asserting a value it can't measure.

---

## 8. Risks

- **`render.yaml`'s schema isn't validated against Render's own JSON schema**
  — only against this project's own expectations (AC1–AC5). A field Render
  silently ignores or a since-renamed key would pass every test here and
  still fail at actual deploy time. Out of reach without a live account
  (spec's own non-goal), so this is a residual risk T4 cannot close, named
  rather than hidden.
- **The runbook's `psql` invocations are unexercised** — there is no Neon
  project to run them against in this iteration. T2's test checks that the
  right things are *named*, not that the commands are *correct* SQL/shell.
