# 013 — Iteration 10: Authentication

Status: **DRAFT — presented 2026-09-12**, §7 open · Created: 2026-09-12

Charter §6's map gained a row for Iteration 9 rather than being outgrown; this
one gains another. The subject was chosen from B-11's three blockers — *whose
key a deployed instance spends, where the secret lives, and who may spend it* —
because **only the third is engineering.** The first two are decisions about
custody and a platform bill. `011-ship.md` §4 already ruled that adding
authentication "is its own iteration"; this is that iteration, and it is
deliberately taken **before** deployment rather than inside it.

**Nothing here deploys anything.** B-11 stays open, and this iteration is what
makes the remaining conversation about it short.

---

## 1. What this iteration is for

The app has been unauthenticated since Iteration 6, which was correct while it
ran on one machine for one person. Two things have changed since: it spends a
metered credential on every question, and Iteration 8 added its first
unauthenticated **write**.

The measurements below say the gap is wider than "there is no login":

- **there is no user anywhere in the system** — not in the store's schema, not
  in the answer cache's key, not in `/history`, which shows every question to
  everyone;
- **the store has no way to add one**, because its schema is applied by
  `executescript()` on every connect and SQLite has no
  `ADD COLUMN IF NOT EXISTS`;
- and the limitation is documented in two specifications and **in neither of
  the two places a person actually meets this system**.

So "add authentication" is not one change. §7 asks which of them are in scope,
and the honest answer for at least one of them is probably *not this iteration*.

---

## 2. Measurements

Taken 2026-09-12 against the running stack and the current tree. No estimates.

### 2.1 Seven routes and a static mount, none of them authenticated

| route | what it does | spends? | writes? |
|---|---|---|---|
| `POST /ask` | the product | **yes — a provider call** | yes, a history row |
| `POST /feedback` | a mark against an answer id | no | **yes** |
| `GET /history` · `GET /history/data` | every question anyone has asked | no | no |
| `GET /quota` | what the provider last said about its limits | no | no |
| `GET /` · `/static/*` | the page and its scripts | no | no |
| `GET /health` | liveness plus database readiness | no | no |

A search of `api/main.py` and `api/http/` for any credential, token, session,
cookie or login concept returns **nothing**. There is no gate to widen; there is
no gate.

### 2.2 One anonymous request costs 1,208 tokens

Measured today, through the endpoint, on the deployed configuration:

```
ok=True  tokens=1208  calls=1  measured=True
```

Consistent with `010` §2.5's median of 1,078 over a 1,047–1,256 range. Against
the measured ceilings — **200,000 tokens a day** and about **seven questions a
minute** before the slow mode — that is roughly **165 questions a day**, and a
day's allowance can be spent in **under half an hour** of sustained asking.

**This is the whole argument for the iteration**, and it is worth stating in the
form B-11 does: an unauthenticated public URL in front of this is a
denial-of-service surface with a bill attached, and no amount of code decides
whose bill.

### 2.3 There is no user anywhere, and one place is a surprise

`api/store/schema.sql` in full: `ask`, `feedback` and `step`. **No user column
on any of them.** The store holds **27 answers and 1 feedback mark** today, all
of them attributable to nobody.

The surprise is the answer cache. `api/http/cache.py::cache_key` hashes
**question + schema fingerprint + prompt fingerprint** and nothing else, so a
cached answer is served to whoever asks next. That is defensible — every
identity would be reading the same read-only database and getting the same
answer, and sharing is what makes a hit free — but it is currently a property
nobody chose, and it becomes a decision the moment identities exist (Q-E).

### 2.4 The store cannot add a column

`api/store/schema.sql` is applied with `executescript()` **on every connect**,
and every statement is `CREATE ... IF NOT EXISTS` so that opening an existing
database is a no-op. That pattern has no way to alter an existing table, and
SQLite offers no escape:

```
sqlite3 3.53.1: ALTER TABLE t ADD COLUMN IF NOT EXISTS b TEXT
  -> OperationalError: near "EXISTS": syntax error
```

**So attributing history to a user is not a column addition; it is this
project's first schema migration**, against a live database holding real rows
that `docker compose down` deliberately preserves. That is a materially bigger
task than a gate, and §7's Q-D asks whether it belongs here at all.

### 2.5 A shared secret needs no new dependency; accounts do

Checked inside the container:

| | |
|---|---|
| `fastapi.security`, `hmac`, `secrets`, `hashlib` | **available** |
| `passlib`, `bcrypt`, `jwt`, `itsdangerous` | **missing** |

`api/requirements.txt` has six runtime dependencies. A shared-secret or
named-token scheme is buildable from what is already installed —
`secrets.compare_digest` and `HTTPBearer`. **Password accounts are not**: they
need a hashing library, and that is a new runtime dependency in a project whose
frontend rule is literally "no build step and no new dependency".

### 2.6 The gate would meet 100 existing calls

Endpoint calls in the test suite, by route:

| route | calls |
|---|---|
| `POST /ask` | 39 |
| `GET /quota` | 15 |
| `GET /history/data` | 16 |
| `GET /` | 9 |
| `POST /feedback` | 8 |
| `/static/*` | 7 |
| `GET /history` | 4 |
| `GET /health` | 2 |

Across **seven test files**. Every one of them meets whatever gate this
iteration adds, which makes the fixture that authenticates them **the single
most dangerous object in the task**: if it is autouse, the gate is never
exercised and AC1 is vacuous. §6 says so and the plan will give it a mutation.

### 2.7 `/health` cannot be gated

`docker-compose.yml`'s `api` healthcheck probes `/health`, and Iteration 8 T7
added it precisely so `docker compose ps` cannot report a broken API as `Up`.
A gate on that endpoint makes the container permanently unhealthy and
`up --wait` block forever.

This is not an argument about what *should* be public — it is a measured
constraint that removes one option from Q-B.

### 2.8 The limitation is recorded where nobody reads it

| where it says the app has no authentication | |
|---|---|
| `011-ship.md` §4 | yes |
| charter §8, B-11 | yes |
| **`README.md`** | **no** |
| **the page at `/`** | **no** |

Both places a person actually meets this system are silent. That is a standing
rule of this project's own — architectural limits belong in the README and on
the page, not only in a task report — and it is currently unmet regardless of
what this iteration builds.

---

## 3. Acceptance criteria

### The gate

- **AC1** — An unauthenticated request to a protected endpoint is refused with
  **401**, and **the provider is never called**. The refusal is the point; the
  unspent token is the property worth asserting.
- **AC2** — AC1 is measured rather than assumed: a test asserts **zero provider
  calls and zero tokens** on a rejected `/ask`, not merely the status code.
- **AC3** — What stays open is **named with a reason**, and `/health` is open
  because §2.7 measured that it must be. Anything else left open is a decision
  recorded in the charter, not an omission.
- **AC4** — Comparison of the presented credential is **constant-time**
  (`secrets.compare_digest`), asserted against the parsed AST rather than by
  grepping for the name.

### Not leaking the thing that guards it

- **AC5** — The credential never appears in a log, an error message, a trace, a
  history row, or the page — the same rule `GROQ_API_KEY` already has, and
  `GroqProvider._safe_message` is the precedent.
- **AC6** — A wrong credential and a missing one are **indistinguishable to the
  caller**: same status, same body. A gate that says "wrong token" tells an
  attacker the endpoint is worth attacking.

### Cost, and what is not paid

- **AC7** — **No new runtime dependency**, unless Q-A chooses a scheme that
  demands one — in which case the dependency is named, justified against §2.5,
  and added to both `requirements.txt` and the lockfile.
- **AC8** — If Q-D puts a user column in the store, it ships with a **migration
  proven against a copy of the live 27-row database**, never against an empty
  one. §2.4 makes this the largest risk in the iteration.

### Honest, and operable

- **AC9** — An operator can set and rotate the credential **without editing
  code**, and `docker compose up` still works from the documented steps.
- **AC10** — The README and the page state the security posture the iteration
  actually ships (§2.8), whatever that turns out to be. If the answer is "one
  shared secret, no user separation", both say so plainly.
- **AC11** — No prompt, dataset or scorer change. `EVALS.md` does not move.

---

## 4. Non-goals

- **Deployment.** B-11 stays open. This iteration removes one of its three
  blockers and touches neither of the others.
- **Per-user accounts with passwords**, unless Q-A asks for them — §2.5 prices
  them at a new runtime dependency and §2.4 at this project's first migration.
- **Authorisation.** Whatever identities exist, they all get the same
  capabilities. Roles are a different iteration and there is nothing yet to
  differentiate.
- **Transport security.** TLS is a deployment concern and belongs to B-11; a
  bearer token over plaintext HTTP on localhost is exactly as safe as the
  localhost it runs on, and no safer once deployed.
- **Any accuracy work.** AC11.

---

## 5. Contracts this iteration must not break

- `/health` answers unauthenticated (§2.7), and still reports `history.writable`
  and the database user.
- **A failed question is still `200` with `ok: false`.** A 401 is a different
  thing from a failed question and must not be routed through
  `api/http/errors.py`'s category table, which maps *answer* failures.
- Every failure category still appears in `api/http/errors.py`, mapped or
  declared `UNREACHABLE` — the test that walks every module under `api/` still
  passes.
- `complete(system, user) -> str` stays one method wide; `groq` stays imported
  by exactly one module.
- Nothing reaches the database except through `execute_sql()`.
- Feedback stays collected and **not consumed** (`011` AC14).
- `EVALS.md` stays append-only and its fingerprints keep reproducing.

---

## 6. Risks

- **The test fixture is the most dangerous object in this iteration.** §2.6
  counted 100 calls that will meet the gate. An autouse fixture that
  authenticates them all makes AC1 vacuous while the suite stays green — the
  exact shape `HANDOFF.md` §6 records twice, most recently at Iteration 9 T6
  where a helper was correct, tested and unreachable. The gate needs a test that
  drives it *without* the fixture.
- **A migration against a live database is the largest thing here** if Q-D says
  yes. The store holds 27 rows that `down` preserves deliberately, and this
  project has never written a migration. §2.4 is the measurement; the answer may
  well be to defer it.
- **Gating the page and gating the API are different problems.** The page is
  served by the same app and calls it with relative `fetch()`. A token in a
  header is easy for the API and awkward for `GET /`, which a browser requests
  with no JavaScript in play (Q-C).
- **A gate is a new way to make the product unusable.** Every failure mode this
  adds — a missing variable, a rotated secret, a stale browser tab — is a
  100%-failure mode, unlike a rate limit which degrades. AC9 exists for that.
- **Auth with no deployment is an unmeasured feature**, the critique that
  deferred feedback at Iteration 7 T1. The defence is that unlike feedback, this
  has an invariant that can be asserted without users: an unauthenticated
  request is refused and spends nothing. If the iteration cannot show that, the
  critique wins.

---

## 7. Open questions

- **Q-A — What is an identity?** §2.5 measured that the answer decides whether
  this iteration adds a dependency.

  1. **One shared secret.** A single token gates the surface. No identity, no
     schema change, no dependency. Answers B-11's *"who may spend it"* with
     *"whoever was given the token"*.
  2. **Named tokens.** An allowlist of `name -> secret`, so requests are
     attributable. Still stdlib-only; still no user table unless Q-D says yes.
  3. **Accounts with passwords.** A users table, a hashing dependency, sessions.

  *My lean: (2), named tokens.* It costs almost exactly what (1) costs — the
  comparison is against a small mapping instead of one value — and it is the
  difference between "somebody spent 40,000 tokens today" and "who did". That is
  the question a deployed instance will actually ask, and retrofitting
  attribution later means the migration in §2.4 anyway. (3) is a real product
  decision that this project has no users to justify.

- **Q-B — What is protected?** §2.7 removes `/health` from the discussion; the
  rest is open. `POST /ask` and `POST /feedback` are not in doubt. `GET /quota`,
  `GET /history` and `GET /history/data` are read-only and leak **every question
  anyone has asked**.

  *My lean: everything except `/health`, including the page.* The history
  endpoints are the ones I would not leave open under any reading — questions
  are the most sensitive thing this system holds, far more than the answers, and
  §2.3 says they are currently pooled with no attribution. Leaving `/quota` open
  is defensible and I would still close it, because it reports the state of a
  credential the public has no business knowing.

- **Q-C — How does a browser get in?** The page and the API are one app, and the
  page calls it with relative `fetch()`. A bearer header is natural for the API
  and unavailable to the browser's initial `GET /`.

  *My lean: a signed cookie set by a minimal login form, with the bearer header
  accepted in parallel for `curl` and scripts.* Two mechanisms is more than I
  would like, and the alternative — HTTP Basic on everything — is one mechanism
  that browsers already understand, needs no login page, and no new dependency.
  **I lean Basic if you want this iteration small**, and cookie-plus-bearer if
  the page's experience matters more than the iteration's size. This is the
  question I am least confident about.

- **Q-D — Does history become per-user?** §2.4 makes this the migration
  question, not the auth question.

  *My lean: no, not this iteration — and say so on the page.* Attribution needs
  a `user` column on `ask`, which needs this project's first migration against a
  live 27-row store, which is a task with its own risks and deserves its own
  measurement rather than riding along. Named tokens (Q-A) make it *possible*
  later without re-deciding anything. What this iteration should do is stop the
  history being **public**; making it **private per user** is the next step, and
  AC10 requires the page to say which of the two it currently is.

- **Q-E — Does a cached answer cross identities?** §2.3 found the cache keyed on
  question and fingerprints with no caller, so today it does.

  *My lean: yes, leave it shared, and record it as a decision.* Every identity
  reads the same read-only database through the same prompt, so the answer is
  genuinely the same and a shared hit is what makes it free — partitioning the
  cache per identity would multiply provider spend by the number of users to
  produce identical rows. The thing that makes this safe is that the cache holds
  *answers*, not *who asked*; the moment a cache entry carries anything
  user-specific, this flips. Worth a test pinning that it does not.
