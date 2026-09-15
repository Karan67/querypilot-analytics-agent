# 015 — Iteration 12: Production deployment

**Status:** drafted 2026-09-15, §7 resolved. Discharges the engineering half of **B-11**.

Charter: `specs/000-project.md`. Discharges the engineering half of **B-11**.
Measurements: taken live on 2026-09-15 against `querypilot-api` and
`querypilot-db` at commit `cc7e108`; every command is named in §2.
Plan: `specs/015-production-deployment-plan.md`.

B-11 has been deferred four times, and the reason is that it is two things
under one id. One half is code — an image that runs as root, a port that cannot
move, a response with no security headers, a secret that `docker inspect` will
read back. That half can be finished on a laptop, tested, and merged, and it is
what this iteration does. The other half is custody: whose host, whose domain,
whose card, whose API key. That half is a decision, it cannot be tested, and
§7's matrix puts it in front of a human instead of letting it block the code for
a fifth iteration.

---

## 1. What this iteration is for

Iteration 10 answered B-11's third blocker. A caller now needs a credential
before they can spend the project's quota, and an unset credential map fails
closed rather than open. That was the blocker with a code-shaped answer, and it
got one.

The two that remain do not have code-shaped answers in the same way.

**Key provisioning** is B-11's sharpest point, stated in the charter as: the
project has one free-tier key with a ceiling of roughly 180 questions a day,
and *"a public URL in front of that is a denial-of-service surface with a bill
attached."* Authentication narrows who can pull that trigger; it does not stop
a single authenticated identity, or a browser tab left reloading, from emptying
the day's tokens before lunch. The perimeter answered *who*. Nothing yet
answers *how much*.

**Secrets management** is the one the charter admits got worse. There are now
two secrets — `GROQ_API_KEY` and `QUERYPILOT_USERS` — and both live the same
way: interpolated from a gitignored `.env` into a compose `environment:` block.
That is fine on a laptop and wrong on a host, because the values are then
readable from the container's environment by anything that can reach the Docker
socket, and they are printed by `docker inspect` in full.

Underneath both sits a container that was never built to be deployed. It was
built to be run by `docker compose up` on the machine of the person who wrote
it, and it shows: it runs as root, it ships its own bytecode cache, it binds a
port it cannot be told to change, and it carries no liveness probe of its own
because compose was always there to supply one.

So this iteration has one goal and one refusal.

The goal is that the artifact becomes deployable: an image that a reasonable
platform would accept without special pleading, a TLS story that is exercised
rather than assumed, secrets that are not readable from a process listing, and a
spend ceiling that survives an authenticated caller behaving badly. All of it
merges without a cloud account.

The refusal is that this iteration does not pick a host. Choosing one means
choosing a card, a domain, a DNS zone and a Postgres custodian, and those are
the user's to choose, not mine to assume. §7's matrix lays them out with what
each costs and what each forecloses. Whichever way they go, the code in this
iteration is the same code.

---

## 2. Measurements

Every number here was taken on 2026-09-15 against the running stack at commit
`cc7e108`. The commands are named so they can be re-run. Where two commands
disagree, both are printed.

### The image as it stands

`docker image inspect querypiolt-api:latest`, `docker history`, `docker top`:

| What | Measured | Command |
|---|---|---|
| Base | `python:3.12.13-slim`, Debian trixie | `inspect` env `PYTHON_VERSION` |
| Stages | **1** — no builder stage | `history`, linear |
| Layers | 8 | `inspect .RootFS.Layers` |
| Reported size | **297 MB** | `docker images` |
| Named layers sum | **227.1 MB** | `docker history` |
| Runtime user | **`root`** | `docker top querypilot-api -o user` |
| Image `HEALTHCHECK` | **none** (`<nil>`) | `inspect .Config.Healthcheck` |
| `CMD` | `uvicorn api.main:app --host 0.0.0.0 --port 8000` | `inspect .Config.Cmd` |

**The 297 MB and the 227.1 MB do not reconcile**, and I am not going to pretend
they do. `docker images` reports 297 MB; the eight layers `docker history`
names sum to 227.1 MB; `docker image inspect .Size` reports a third figure,
70,271,069 bytes. This host runs Docker 29.7.2, where the containerd snapshotter
changes what `.Size` counts. The actionable number is the layer breakdown,
which is consistent with itself:

| Layer | Size |
|---|---|
| Debian trixie rootfs | 87.4 MB |
| CPython build + apt deps | 41.4 MB |
| ca-certificates | 4.95 MB |
| **`pip install -r requirements.lock`** | **92.2 MB** |
| `COPY . ./api/` | 1.1 MB |
| everything else | < 40 kB |

The dependency layer is the only large one this project controls. The 133.8 MB
beneath it belongs to the base image and moves only by changing base.

### The process, live

`docker top`, against containers up 5 minutes and reporting healthy:

- `querypilot-api` → **`root`**, pid 492, `uvicorn api.main:app --host
  0.0.0.0 --port 8000`.
- `querypilot-db` → **UID 70**, the Alpine `postgres` account.

The database image already declines to run as root. The one we build does not.

### The build context

There is **no `.dockerignore`**, at the repository root or under `api/`.
`api/Dockerfile:23` is `COPY . ./api/` against a build context of `./api`:

| | Files | Bytes |
|---|---|---|
| Whole context | 106 | 809,363 |
| Source only (no `__pycache__`) | 45 | 346,978 |
| **Shipped and unwanted** | **61** | **462,385 (57%)** |

Seven `__pycache__` directories are inside the context. The `COPY` layer
measures 1.1 MB against 347 kB of source, which is consistent with the bytecode
having shipped. `PYTHONDONTWRITEBYTECODE=1` is set at `api/Dockerfile:9` — it
stops the container writing *new* bytecode, and does nothing about bytecode
copied in from the host.

`api/requirements-dev.txt` and `api/requirements-dev.lock` also land in the
image. They are never installed — the Dockerfile only ever reads
`requirements.lock` — so this is inventory, not dev dependencies in production.

### What the perimeter returns

`curl` against `localhost:8000`, live:

| Request | Result |
|---|---|
| `GET /health` | `200`, 197 ms, **no credential required** |
| `GET /` | `401` |
| `POST /ask` (anonymous) | `401` |
| `GET /nope` (no such route) | `401` |

The last row is a property worth naming: the gate is middleware, not a route
dependency, so an anonymous caller cannot enumerate routes by their status
codes. A 404 would be a disclosure. This must survive the iteration.

### Headers on the wire

The complete set of response headers on a `200`:

```
date · server: uvicorn · content-length · content-type
```

On a `401`, additionally `www-authenticate: Basic realm="QueryPilot"`.

That is the whole list. There is **no** `Strict-Transport-Security`, no
`X-Content-Type-Options`, no `X-Frame-Options`, no `Content-Security-Policy`,
no `Referrer-Policy`, and no CORS header of any kind. `server: uvicorn` names
the server and its absence would cost nothing.

### What `/health` tells a stranger

`/health` is deliberately public — the compose healthcheck probes it with a bare
`urlopen` and no credentials (`docker-compose.yml:122-135`). Its body, to an
unauthenticated caller:

```json
{"status":"ok","database":{"connected":true,"user":"querypilot_ro",
"database":"chinook","public_tables":12},"history":{"writable":true,
"error":""},"auth":{"configured":true,"error":""}}
```

On a laptop this is exactly the diagnostic it was built to be. On a public URL
it hands an anonymous caller the database role name, the database name and the
table count. This is a measurement, not yet a verdict — Q-H asks what to do.

### Ports, as published today

`docker ps`:

```
querypilot-api   0.0.0.0:8000->8000/tcp
querypilot-db    0.0.0.0:5432->5432/tcp
```

**Postgres is published on all interfaces.** Compose does this because the host
test suite and the eval runner both connect over the mapped port, which is a
real requirement on a laptop and an unforced hole on a host.

### Port mobility

- `api/Dockerfile:25,27` — `EXPOSE 8000`, and `8000` hardcoded in `CMD`.
- `docker-compose.yml:97` — `"${API_PORT:-8000}:8000"`; only the host side moves.
- `docker-compose.yml:127` — healthcheck hardcodes `http://127.0.0.1:8000/health`.
- **No module under `api/` reads `PORT` or `HOST`.**

Render, Fly.io and Railway all inject `$PORT` and expect the process to bind it.
Today the container cannot comply.

### Secrets, as the daemon sees them

`GROQ_API_KEY` and `QUERYPILOT_USERS` arrive as compose `environment:` entries
(`docker-compose.yml:53-88`), interpolated from a gitignored `.env`. Anything
holding the Docker socket reads both in plaintext from `docker inspect`. There
is no `*_FILE` indirection, no Docker secret, and no secrets manager.

The image's own baked environment is clean — `PATH`, `LANG`, `GPG_KEY`,
`PYTHON_VERSION`, `PYTHON_SHA256`, `PYTHONDONTWRITEBYTECODE`,
`PYTHONUNBUFFERED`, and nothing else. No secret is in a layer.

### What exists to build on

- **Deploy manifests: none.** No `fly.toml`, `render.yaml`, `railway.json`,
  `Procfile`, Kubernetes manifest, nginx or Caddy config, or Terraform.
- **Workflows: one.** `.github/workflows/ci.yml`. It boots the real stack and
  runs the suite. There is no build, publish or deploy workflow.
- **Logging config: none.** Three `getLogger` call sites, no `basicConfig` or
  `dictConfig`; uvicorn's defaults apply. The auth path is audited against
  secret leakage (`tests/test_auth_logic.py:116-152`); no other path is.

### The charter line that is now false

`specs/000-project.md:196` still reads:

> **Deployment** — Vercel (frontend), Render or Fly.io (API), Supabase (Postgres).

Since Iteration 6 the frontend is served by FastAPI from `api/web/` with no
build step and no npm, mounted at `api/main.py:719`. **There is no separately
deployable frontend.** The line predates the decision that removed it. Q-M asks
whether to amend it.

---

## 3. Acceptance criteria

### The image

- **AC1** — The runtime image runs as a non-root user. Enforced by a test that
  reads the built image's config rather than the Dockerfile text, so a `USER`
  line that a later stage overrides cannot pass.
- **AC2** — The image is multi-stage: dependencies are built in a stage that is
  discarded, and no compiler, no build toolchain and no `pip` cache reaches the
  runtime stage.
- **AC3** — A `.dockerignore` exists and excludes `__pycache__`, `*.pyc`,
  `.venv`, `.git`, and the dev requirement files. Enforced by a test that
  asserts the built image contains **zero** `__pycache__` entries **under
  `/app`**, not by a test that reads `.dockerignore` — the file's existence is
  not the property.

  > **Scope corrected during T2.** This criterion was drafted as "zero
  > `__pycache__` entries" full stop, and that is unachievable: the built image
  > carries 201 of them under `/usr/local/lib`, written by `pip install` when
  > it compiles the dependencies it was asked to install. Those are not the
  > leak — they are what installing a package does. The leak is host bytecode
  > arriving through the build context, all of which lands under `/app`, and
  > that count is now **0** where it was 6.
  >
  > The patterns also had to change to get there. Docker matches with
  > `filepath.Match`, not gitignore semantics, so the first attempt's
  > `__pycache__/` matched only the context root and 6 of the 7 directories
  > shipped anyway. Nested paths need a `**/` prefix, and the trailing slash
  > carries no meaning at all.
- **AC4** — The image declares its own `HEALTHCHECK`, so a platform that never
  reads `docker-compose.yml` still has a liveness probe.
- **AC5** — The image's size is recorded before and after in the spec's own
  §2, as a measurement, with the layer breakdown. A reduction is expected but
  is **not** an acceptance criterion; the criteria are AC1–AC4, and a larger
  image that satisfies them passes.

### The port

- **AC6** — The container binds `$PORT` when it is set and `8000` when it is
  not, so the same image runs under compose and under a platform that injects a
  port. The compose healthcheck is derived from the same value rather than
  hardcoding `8000` a second time.

### The perimeter on the wire

- **AC7** — Every response carries `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy` and a `Content-Security-Policy`, including error responses
  and including the `401`. Enforced by a walk over every declared route in the
  style of `tests/test_auth.py:314-439`, with a route-count floor, not a
  hand-written list.
- **AC8** — The CSP is proved against the real page: a test asserts that
  `api/web/` contains no inline `<script>` or inline `style` attribute that the
  declared policy would block. A policy the application violates is worse than
  none, because it will be loosened at the first bug report.
- **AC9** — `Strict-Transport-Security` is emitted only when the request
  arrived over HTTPS, so a laptop on plain HTTP does not pin itself.
- **AC10** — The application honours `X-Forwarded-Proto` and
  `X-Forwarded-For` **only** from a configured set of trusted proxy addresses,
  and ignores them otherwise. An unconditionally trusted forwarded header lets
  any client claim HTTPS, which makes AC9 a lie.
- **AC11** — `server: uvicorn` no longer identifies the server.
- **AC12** — The 401-for-unknown-routes property measured in §2 survives:
  an anonymous caller cannot distinguish a real route from an absent one.

### Disclosure

- **AC24** — `/health` answers an anonymous caller with `{"status":"ok"}` and
  nothing more; the database role name, database name and table count require a
  credential. The compose healthcheck, which presents none, still passes because
  it reads only `status`.

### TLS

- **AC13** — TLS is exercised, not asserted. `docker compose --profile tls up`
  brings up a terminating reverse proxy in front of the API, and a test drives
  a real HTTPS request through it end to end. A deployment story that only
  works on a platform nobody here has an account with is not a story.
- **AC14** — With the TLS profile up, plain HTTP is redirected rather than
  served, and the redirect is to the same host and path.

### Secrets

- **AC15** — `GROQ_API_KEY` and `QUERYPILOT_USERS` can each be supplied from a
  file rather than an environment variable, via a `_FILE` suffix, and the file
  form wins when both are present.
- **AC16** — With the file form in use, neither secret's value appears in
  `docker inspect` output for the API container. Enforced by a test that runs
  the inspection and greps the result.
- **AC17** — The `_FILE` reader never logs the file's contents, and reports a
  missing or unreadable file by naming the path, never the value — the
  discipline already proved for auth at `tests/test_auth_logic.py:116-152`.

### Spend

- **AC18** — An authenticated identity is subject to a per-day question ceiling,
  refused with a mapped failure category rather than an unhandled error. This
  reverses `specs/010-hardening.md` §Q-B, which deliberately rejected an inbound
  limiter; the reversal is deliberate and Q-F asks for confirmation.
- **AC19** — The ceilings are configuration, not code: **50 questions per
  identity per day** and **150 per day globally**, both overridable by
  environment variable, and unset means those documented defaults rather than
  unlimited.
- **AC20** — Every new failure category is in `api/http/errors.py` or declared
  `UNREACHABLE`, so `test_every_category_in_the_project_is_accounted_for`
  (`tests/test_error_mapping.py:79-96`) stays green without being widened.

### Honest about itself

- **AC21** — The README gains a deployment section stating what the artifact
  protects and what it does not, in the register of its existing *"What this
  protects, and what it does not"* — in particular that a per-day ceiling
  bounds the bill but does not make the URL safe to publish.
- **AC22** — The production posture does not publish Postgres on all
  interfaces. The laptop posture still may, because the host test suite and the
  eval runner need the mapped port.
- **AC23** — Charter §8's B-11 entry is updated to record which blockers this
  iteration discharged and which remain custody decisions.

---

## 4. Non-goals

- **Choosing a host, a registrar or a DNS provider.** These need a card and an
  identity. §7's matrix presents them; it does not resolve them.
- **Actually deploying.** Nothing in this iteration is expected to result in a
  live public URL. If one is wanted afterwards, it is a separate, short
  iteration against whatever §7 decides.
- **Per-user data isolation.** Charter §3's non-goal stands. `/history` still
  shows every signed-in user every question anybody asked. A spend ceiling is
  per-identity accounting, which is not the same as per-identity privacy, and
  the README must not let the two be confused.
- **Replacing HTTP Basic.** Iteration 10 chose it with its eyes open. TLS
  addresses the transport objection, which was the only one that deployment
  raises.
- **A secrets manager.** File-based secrets are the largest improvement
  available without a vendor. Vault, SOPS and Doppler each add a dependency and
  an account, and belong to the custody decision.
- **A logging configuration overhaul.** Worth doing and not this. The auth path
  is already audited; the rest is carried debt, not a deployment blocker.
- **Managed Postgres migration.** Whether the target database moves to Supabase
  or Neon is in the matrix. Making it move is not in this iteration.

---

## 5. Contracts this iteration must not break

- **Nothing bypasses the safety layer.** Every read still goes through
  `execute_sql()`. Charter §4's exemption count stays at **one**.
- **The API holds only `querypilot_ro`.** No privileged DSN enters the runtime
  configuration, whatever the host. Seeding and migration credentials stay
  outside it.
- **The deployed API does not pace.** `test_ac5_the_deployed_api_does_not_pace`
  (`tests/test_ask_endpoint.py:143-160`) AST-walks `api/main.py` for pacing
  imports. A spend ceiling **refuses**; it must never sleep.
- **No vendor SDK outside `api/llm/`**, and no agent framework.
- **`/health` stays reachable without a credential**, or the compose healthcheck
  — which presents none — fails and the container is marked unhealthy.
- **Unset credentials still fail closed.** No change here may turn an
  unconfigured deployment into an open one.
- **No default secret ships**, in compose, in a manifest, or in an example file
  that a deployment could copy unchanged.
- **The test suite keeps its hermetic lane.** Iteration 11's 923 hermetic tests
  stay hermetic; anything needing the proxy or the daemon declares `needs_db`
  or an equivalent marker rather than quietly widening the lane.
- **A test asserting "this string must not appear" reads code, not commentary.**
  Four bugs of this exact shape are on the record. New structural tests parse
  YAML, parse the AST, or inspect the built image — they do not grep text.

---

## 6. Risks

- **The CSP breaks the page and gets loosened.** The likeliest failure. AC8
  exists to make the policy answer to the page rather than the page answer to
  the policy; if `api/web/` turns out to need inline script, the honest move is
  a narrower policy stated plainly, not `unsafe-inline` with a comment.
- **The TLS profile becomes CI-only scenery.** A proxy that only ever runs in a
  test is not evidence about production. AC13 requires it be the same
  configuration a deployment would use.
- **A per-day ceiling that fires in the demo.** A ceiling low enough to protect
  the key is low enough to interrupt a live demonstration. The default needs to
  be chosen against the ~180 question/day figure with the demo in mind.
- **Non-root and the SQLite history store collide.** `QUERYPILOT_HISTORY_PATH`
  defaults to `/data/querypilot.db` on a named volume. A volume created before
  the `USER` change is owned by root, and the container will fail to write
  without being obvious about why. This will bite on upgrade, not on a fresh
  start, which is the worse of the two.
- **Trusted-proxy configuration is fiddly and silent.** Get AC10 wrong in the
  permissive direction and AC9 becomes decorative. Get it wrong in the strict
  direction and HTTPS requests look like HTTP. Neither failure announces itself.
- **The image shrinks less than it looks like it should.** 133.8 MB is base
  image. A multi-stage build attacks the 92.2 MB dependency layer only, and
  `psycopg[binary]` and `sqlglot` are most of it. AC5 deliberately does not set
  a size target, so that a disappointing number can be reported rather than
  chased.
- **The global ceiling and the per-identity ceiling disagree in an unhelpful
  way.** Three identities at 50 each reach 150, so the global limit binds first
  and the identity who happens to ask last is refused for a reason that is not
  their fault. The refusal message has to say *which* ceiling was hit, or it
  will read as a bug.

---

## 7. Open questions

Fourteen, Q-A to Q-N. **Q-A to Q-I are engineering** and decide what gets built.
**Q-J to Q-N are custody**, and are laid out as a matrix below because they
share one property: none of them can be answered by measuring anything.

Q-A to Q-I were resolved before implementation began; Q-J to Q-N were deferred.

### The engineering questions

- **Q-A — spec and plan together, or spec first?** *Resolved: write the spec,
  then draft the plan immediately, and pause before implementation rather than
  between documents.*

- **Q-B — does this iteration produce a deployment, or a deployable artifact?**
  *Resolved: a deployable artifact only. No live cloud resource, no card.*

- **Q-C — how far does the multi-stage build go?** *Resolved: (1), multi-stage
  wheel build on `python:3.12-slim`. Alpine and distroless are not touched.*

- **Q-D — named user or numeric UID, and what owns `/data`?** *Resolved: (2),
  fixed numeric UID `10001`, with an explicit volume-permissions migration note
  in the README.*

- **Q-E — does `$PORT` win, or does `8000` stay fixed?** *Resolved: (1), bind
  `$PORT` with `8000` as the fallback.*

- **Q-F — does the spend ceiling ship, and does it reverse 010's Q-B?**
  *Resolved: (1), the ceiling ships and the reversal of
  `specs/010-hardening.md` Q-B is recorded as deliberate.*

- **Q-G — what is the daily ceiling's default?** *Resolved: (3), both — 50
  questions per identity per day, and a 150/day global ceiling beneath the
  key's measured ~180.*

- **Q-H — does `/health` stay as candid as §2 measured it?** *Resolved: (2), an
  anonymous caller gets `{"status":"ok"}`; the full diagnostic body requires a
  credential.*

- **Q-I — which reverse proxy terminates TLS in the compose profile?**
  *Resolved: (1), Caddy.*

### The custody matrix

None of these is answerable by measurement. Each needs an account, a card, or
both. The code in §3 is identical whichever row is chosen — that is the point
of separating them.

| # | Decision | Options | What it costs | What it forecloses | Reversible? |
|---|---|---|---|---|---|
| **Q-J** | API host | Fly.io · Render · Railway · a VPS · none | card on file; free tiers all sleep or expire | Q-K and Q-L largely follow from it | yes, if the image stays portable — which is what AC6 buys |
| **Q-K** | Postgres | container on the same host · Supabase · Neon · host-native | free tiers exist; all have cold starts | managed means `db/init/` never runs, so Chinook needs a different seeding path | painful — data has to move |
| **Q-L** | TLS issuance | platform-managed · Caddy + Let's Encrypt on a VPS | none in money | platform-managed needs no domain; the Caddy path needs Q-M first | yes |
| **Q-M** | Domain + DNS | none, use the platform subdomain · a registered domain + a DNS zone | ~£10–15/yr plus a registrar account | no domain means no custom host, and HSTS on a shared platform subdomain affects siblings | yes |
| **Q-N** | Registry | GHCR · Docker Hub · platform-native build | GHCR is free for this repo | platform-native build means no image to run anywhere else | yes |

Q-J to Q-N are **recorded and deferred** until the artifact is built and tested.

Two further custody questions that are not in the table because they are not
about vendors:

- **Whose LLM key, and with what ceiling?** The charter assumes the project's
  own free-tier key. A deployment that anyone can reach is spending it. The
  alternative is that a deployment is credential-gated to people the user
  trusts — which is what Iteration 10 built — and Q-F/Q-G decide how hard that
  gate holds. This is the question B-11 has really been waiting on, and Q-F and
  Q-G now answer it.
- **Does the charter get amended?** Two lines are now false or stale.
  `specs/000-project.md:196` names a separately deployable Vercel frontend
  that Iteration 6 removed. Charter §3's non-goal *"Multi-tenancy, auth, user
  accounts"* was partially reversed by Iteration 10 and never amended.
  *Resolved: amend both, in this iteration, under AC23.*
