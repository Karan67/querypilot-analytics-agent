# 019 — Iteration 15: The custody half of B-11

**Status:** drafted 2026-09-17, §7 resolved by user ruling (no open questions).
Discharges the remaining half of **B-11**.

Charter: `specs/000-project.md` §8, B-11 entry. Engineering half already
discharged by `specs/015-production-deployment.md` (Iteration 12).
Precondition (schema generality proven on a live non-Chinook database) met by
`specs/017-schema-generality.md` (Iteration 14).
Measurements: taken 2026-09-17 against Render's and Neon's published docs and
this repository's `docker-compose.yml` / `api/config.py` at commit `cfb7aa6`;
sources named in §2.
Plan: `specs/019-production-deployment-plan.md` (to follow, once this draft is
approved).

B-11 has been open since Iteration 8. Iteration 12 finished the part of it
that was code — a non-root image, a movable port, a spend ceiling, secrets
that don't leak from `docker inspect`. What was left was custody: whose host,
whose managed Postgres, whose domain, whose registry. Those aren't
measurable, so `specs/015` §7 put them in a matrix in front of a human instead
of guessing. This iteration is that decision, now made, plus the concrete
artifact — a Render blueprint and a Neon seeding runbook — that the decision
implies.

---

## 1. What this iteration is for

Two things this iteration is **not**: it does not create a live Render
service or a live Neon project. Doing that means spending the user's own
account and card, which is a real-world action outside what a checked-in
spec can authorize on its own (`specs/015` Q-B drew the same line: "a
deployable artifact only," and nothing about that precondition changed by
having an answer for the custody matrix instead of an open one). It also does
not touch `docker-compose.yml`'s local-dev services — `db`, `pagila-db`,
`data-init`, `caddy` — which is what proves this deployment leg is additive,
not a fork of the one every test and eval already runs against.

What it *is*: the render.yaml blueprint, the Neon seeding runbook, and the
README section that together let the user run `git push` and a handful of
one-time manual steps and end up with a live URL — built once, reviewed once,
not re-derived by hand every time the custody matrix comes up again.

The five items `specs/015` §7 left as Q-J–Q-N (plus the two custody questions
outside that table) are resolved below in §7, by the user's explicit ruling
rather than by this document assuming anything on their behalf.

---

## 2. Measurements

All three below were taken live on 2026-09-17, against the vendors'
documentation and this repository — not estimated, and re-checked here
because the resumption brief that started this iteration named a platform
before any of them were confirmed.

| What | Measured | Source |
|---|---|---|
| Neon Postgres 18 availability | Supported; **default for new projects since June 2026** (was preview from September 2025) | Neon changelog / Postgres version support policy docs |
| Render free-tier persistent disk | **Not available.** Free web services have an ephemeral filesystem: reset on every restart, redeploy, or scale-to-zero. Persistent disks are a paid-tier feature | Render `docs/disks`, Render `docs/free` |
| Neon free-tier project ceiling | **100 projects**, 10 branches each, 0.5 GB storage per project, compute scales to zero after 5 minutes idle | Neon pricing / free-plan-limits docs |

**What this changes about the artifact, measured rather than assumed:**

- `pagila-db`'s requirement for `uuidv7()` (Postgres ≥ 18, per
  `docker-compose.yml`'s comment on that service) is satisfiable on Neon —
  confirmed, not a blocker.
- The `querypilot_data` named volume and the `data-init` chown step
  (`docker-compose.yml` lines 139–147) exist to give the SQLite history store
  a writable, correctly-owned mount. **Neither has anywhere to act on Render's
  free tier** — there is no disk to chown and nothing survives a restart
  regardless. The Render leg of this deployment does not run `data-init` and
  does not mount a volume; `QUERYPILOT_HISTORY_PATH` is left at its
  container-local default and accepted as ephemeral (§7, decision on history
  persistence).
- The isolation-preference decision (one Neon project vs. two) is free either
  way at this scale — Chinook's and Pagila's catalogs are each a few MB, far
  under the 0.5 GB per-project ceiling, so the 100-project allowance was never
  the constraint.

---

## 3. Acceptance criteria

### The Render blueprint

- **AC1** — A `render.yaml` at the repository root declares one web service
  (`type: web`, `env: docker`, `dockerfilePath: ./api/Dockerfile`,
  `dockerContext: ./api`), matching the build context `docker-compose.yml`
  already uses for the `api` service (`context: ./api`).
- **AC2** — The blueprint's health check path is `/health` — the same
  endpoint `docker-compose.yml`'s `api` healthcheck already probes — so
  Render's own deploy gate and the local Docker healthcheck agree on what
  "up" means.
- **AC3** — The blueprint declares every environment variable the `api`
  service's `environment:` block sets in `docker-compose.yml` (§2's table in
  `specs/015` already enumerates the secret-shaped ones), marked `sync: false`
  for anything secret so Render prompts for a value in its dashboard rather
  than the blueprint carrying one.
- **AC4** — The blueprint sets no value for `QUERYPILOT_SECRETS_DIR` or
  either `_FILE` variable — §7's decision is plain env vars for this leg, and
  a test asserts the blueprint does not reference `/run/secrets`.
- **AC5** — A test parses `render.yaml` (not greps it) and asserts the
  service's `dockerfilePath`/`dockerContext` actually point at files that
  exist in this repository, so the blueprint cannot silently drift from the
  Dockerfile it claims to build.

### The Neon seeding runbook

- **AC6** — A new `deploy/neon/` directory documents, as an ordered runbook
  (not automation — this touches an account this project doesn't hold), how
  to: create one Neon project on Postgres 18, create the `chinook` and
  `pagila` databases inside it, run `db/init/01_load_chinook.sh`'s and
  `db/init-pagila/01_load_pagila.sh`'s SQL against each with `psql` using the
  project's connection string, and then run the read-only role scripts
  (`db/init/03_readonly_role.sh` and its Pagila-facing wrapper) the same way —
  because none of `docker-entrypoint-initdb.d` runs against a managed
  Postgres the container never initializes.
- **AC7** — The runbook states explicitly, rather than leaving it to be
  discovered, that this is a one-time manual procedure with no scripted
  idempotency guard — re-running it against a non-empty database is the
  operator's own risk, exactly as it would be running any of `db/init/` by
  hand.
- **AC8** — The runbook names the two resulting connection strings and which
  environment variable each becomes (`QUERYPILOT_DATABASE_URL` for `chinook`,
  `QUERYPILOT_PAGILA_DATABASE_URL` for `pagila`), so filling in Render's
  dashboard is a copy, not a re-derivation.

### Honest about itself

- **AC9** — The README gains a "Deploying to Render + Neon" section stating
  the five §7 decisions in plain language, including the ephemeral-history
  limitation in the same terms `specs/015`'s AC21 used for that iteration's
  own limitations: what the artifact does not do, stated next to what it
  does.
- **AC10** — Charter §8's B-11 entry and `specs/000-project.md`'s B-11 §9
  narrative are updated once this spec is implemented, recording that the
  custody half is now discharged by user ruling (§7 here) and by which
  artifact, mirroring how `specs/015`'s AC23 closed out the engineering half.
- **AC11** — Nothing in `docker-compose.yml` changes. A diff of that file
  against `HEAD` is empty at the end of this iteration — the blueprint is
  additive, and the local `docker compose up -d` path stays exactly what
  `HANDOFF.md` §5 documents today.

---

## 4. Non-goals

- **No live Render service or live Neon project is created by this
  iteration.** Both need the user's own account and card; this iteration
  produces the blueprint and runbook that make creating them a known, bounded
  set of steps rather than an open-ended one.
- **No custom domain and no DNS zone.** §7 rules the platform subdomain.
- **No history persistence beyond the container's ephemeral filesystem** on
  the Render leg. Moving history storage to a durable store is out of scope —
  it would need a write-capable Postgres credential, which the charter's §4
  gate (the API holds only `querypilot_ro`) does not permit without a
  separate decision this spec does not make.
- **No change to `docker-compose.yml`, the `tls` Caddy profile, or any
  existing test.** This iteration adds files; it does not modify the stack
  that 1,547 tests and the eval harness already run against.
- **No image registry.** §7 rules Render's own Dockerfile build from the
  connected repository.

---

## 5. Contracts this iteration must not break

- Gate 1 (`querypilot_ro`-only credential) and Gate 2 (sqlglot validation)
  are unaffected — nothing here touches `api/db/execution.py` or
  `api/safety/validator.py`. The Render leg reaches Postgres exactly the way
  the local stack does, through the same `QUERYPILOT_DATABASE_URL` contract
  `api/targets.py` already reads.
- The provider boundary (`complete(system, user) -> str`, `api/llm/base.py`)
  is untouched; this is a hosting decision, not a provider change, and per
  the standing note that a provider/model change is never bundled into
  another iteration, none is made here.
- `specs/015`'s AC15–AC17 (`_FILE` secret indirection) stay available and
  correct for anyone who *does* mount `/run/secrets` — §7 chooses plain env
  vars for the Render leg specifically, it does not remove the `_FILE`
  mechanism or its tests.

---

## 6. Risks

- **Ephemeral history is a real behavior change for anyone using
  `/history` on the deployed instance** — a restart (including Render's free-
  tier scale-to-zero after inactivity) silently empties it. AC9's README
  section exists so this is disclosed rather than discovered.
- **Render free tier sleeps after inactivity and cold-starts on the next
  request.** Not new to this project's own guards (the quota banner and
  `/quota` already exist for provider-side slowness), but worth naming
  because a sleeping service plus `/health`'s existing behavior means the
  first request after idle time is slow for a reason the health check alone
  won't explain.
- **One Neon project for both databases means one project's worth of
  quota (100 CU-hours/month, 0.5 GB storage) is shared** between Chinook and
  Pagila traffic. At this project's measured ~180 questions/day ceiling and
  each catalog's small size, headroom is large, but a future high-traffic
  demo could hit the shared ceiling sooner than two separate projects would.
  §7 records this as an accepted tradeoff, not an unnoticed one.

---

## 7. Decisions

All five ruled by the user directly, not inferred. Recorded here in the same
style `specs/015` §7 used for its already-resolved Q-A–Q-I — a line each,
*Resolved: ...* — because nothing below is left open for this document.

- **Q-A — History persistence on Render's free tier.** *Resolved: accept
  ephemeral SQLite at the container's default `/data/querypilot.db` path. No
  external volume, no durable history on this leg. Stated as a limitation in
  the README (AC9), not silently accepted.*
- **Q-B — Neon topology.** *Resolved: one Neon project, Postgres 18, holding
  two databases — `chinook` and `pagila` — rather than two separate
  projects.*
- **Q-C — Domain (continuing `specs/015`'s Q-M).** *Resolved: Render's
  platform subdomain (`*.onrender.com`), with Render's own platform-managed
  TLS. No registered domain, no DNS zone.*
- **Q-D — Registry (continuing `specs/015`'s Q-N).** *Resolved: Render builds
  directly from this repository's `Dockerfile` on `main`. No image registry,
  no registry credential to provision.*
- **Q-E — Local parity.** *Resolved: `docker-compose.yml`'s `data-init`,
  `db`, and `pagila-db` services remain untouched and continue to back local
  development exactly as today; the Render leg runs only the `api` web
  service, against Neon instead of those containers.*

Two custody questions from `specs/015` §7 that are answered by continuity
rather than restated here: **whose LLM key** (the project's own free-tier
Groq key, gated by the existing `QUERYPILOT_USERS` credential map — Iteration
10's answer, unchanged) and **TLS issuance** (Render's platform-managed
certificate, which needed no separate ruling once Q-C chose the platform
subdomain).
