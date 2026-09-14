# 013 — Iteration 10 plan: Authentication

Status: **DRAFT — presented 2026-09-12**, D-1 through D-6 open · Created: 2026-09-12

Implements [`013-auth.md`](013-auth.md), whose §7 is resolved. This document says
*how*, and surfaces the decisions the design itself raised.

> **Resolved by the spec, and binding here.** **HTTP Basic** across the API and
> the page, named identities from an env-configured map, compared with
> `secrets.compare_digest` · **everything protected except `/health`** · **no
> per-user history** and no migration against the live store · **the answer cache
> stays shared**, with a test pinning that its key carries no user · and **no
> global autouse credential fixture** — an explicit authenticated client, plus a
> negative suite proving 401 with zero provider calls and zero tokens.

---

## 1. Approach

Three measurements taken while drafting this plan changed its shape, and two of
them changed it before a line was written.

**A route dependency cannot protect the static mount.** Tested rather than
assumed, against a deny-all app-level dependency:

```
GET /            -> 401
GET /static/x.js -> 200
```

`app.mount("/static", StaticFiles(...))` is a sub-application, and the parent's
dependency injection does not run for it. So the gate is **middleware**, not
`Depends` — see D-1, which is the first decision because everything else sits on
it.

**HTTP Basic does what Q-C needs, confirmed end to end.** A missing credential
returns `401` carrying `WWW-Authenticate: Basic`, which is what makes a browser
prompt; a malformed `Authorization` header returns `401` rather than raising;
and `httpx`'s `auth=("user", "secret")` tuple works in `TestClient`, which is
what the explicit fixture will use.

**The suite already owns the fixture AC2 needs.**
`tests/conftest.py::provider_that_must_not_be_called` exists, is deliberately
opt-in, and its docstring explains why an autouse version would be wrong. AC2 —
*a rejected request spends nothing* — is that fixture plus a 401 assertion.

The ordering is driven by one constraint: **the suite must not spend a task in
the red.** Wiring the gate first breaks 100 call sites at once. So the
credential logic lands unwired, the tests learn to authenticate against a gate
that is not yet live, and only then is it turned on — with the negative suite
written in the same task, because a gate that is switched on under tests that
already authenticate is a gate nothing has proven works.

---

## 2. The gate

### 2.1 Shape

One new module, `api/http/auth.py`, holding three things and no HTTP:

```
load_identities(raw: str | None) -> dict[str, str]    # parse QUERYPILOT_USERS
verify(identities, username, password) -> str | None  # constant-time, returns the name
UNAUTHORIZED_HEADERS = {"WWW-Authenticate": 'Basic realm="QueryPilot"'}
```

and one middleware in `api/main.py` that calls them. The split follows the rule
`api/agent/tools.py` states about itself: the module is the implementation, the
wiring is thin.

### 2.2 Constant time, including for names that do not exist

`secrets.compare_digest` on the password is the obvious half. The half that gets
missed is the **username**: a dictionary lookup that misses returns immediately,
so an unknown name is measurably faster than a known one with a wrong password,
and that difference enumerates the user list.

So `verify` compares against a fixed dummy secret when the name is unknown, and
does the same amount of work either way. AC4 is asserted against the parsed AST
— `secrets.compare_digest` is called and `==` is not used on the secret — for
the reason this project always asserts structure against the AST rather than by
grepping.

### 2.3 One refusal, whatever went wrong

AC6 says a wrong credential and a missing one are indistinguishable. There are
four ways in, and all four produce the identical response — same status, same
body, same headers:

| | |
|---|---|
| no `Authorization` header | 401 |
| a malformed header | 401 |
| an unknown username | 401 |
| a known username, wrong secret | 401 |

`WWW-Authenticate: Basic realm="QueryPilot"` rides on all of them, because
without it a browser will not prompt and Q-C's whole argument for Basic
collapses.

**A 401 is not a failed question.** `013-auth.md` §5 makes this a contract: it
must not be routed through `api/http/errors.py`'s category table, which maps
*answer* failures onto status codes. An unauthorised caller has not asked a
question; there is nothing to report `ok: false` about.

### 2.4 What is exempt, and how that cannot rot

`/health` alone (§2.7 of the spec — the compose healthcheck probes it, and a
gate there makes the container permanently unhealthy).

An exemption expressed as `if path == "/health"` inside the middleware is a
string comparison that will eventually be wrong about `/health/`, and a new
route added next year is protected only if somebody remembers. Both are the
shape this project keeps getting caught by, so the mechanism is a
**completeness test** in the pattern `RETRY_POLICY` and `api/http/errors.py`
already use: walk `app.routes` *and* the mounts, and require every path to be
either covered by the middleware or present in an explicit exemption set. A new
unprotected route fails the suite until somebody decides which it is.

---

## 3. Teaching 100 calls to authenticate, without disabling the gate

§2.6 of the spec counted the call sites. The user's directive is explicit and it
is the right one: **no global autouse credential fixture.**

```python
@pytest.fixture
def authed_client(monkeypatch):
    """Explicit, never autouse. A client that presents a valid identity."""
    monkeypatch.setenv(USERS_ENV, json.dumps({TEST_USER: TEST_SECRET}))
    ...
    return TestClient(app, auth=(TEST_USER, TEST_SECRET))
```

Seven files move from `client` to `authed_client`. The plain `client` fixture
**stays**, unauthenticated, because the negative suite needs it — and that is
the point: the two fixtures make "authenticated" a visible property of each
test rather than an ambient one.

**The negative suite is a separate file**, `tests/test_auth.py`, and it walks
every protected route rather than sampling one:

- each protected route returns **401** with no credentials, with a malformed
  header, with an unknown user, and with a wrong secret;
- `/health` returns **200** with none;
- `POST /ask` rejected while `provider_that_must_not_be_called` is installed —
  **AC2's zero provider calls**, asserted by the provider raising if touched
  rather than by counting;
- and the 401 body is byte-identical across all four failure modes.

---

## 4. What this iteration deliberately does not touch

**No migration** (Q-D). The store keeps its three tables and no user column.
History becomes *private to authenticated users* rather than *per-user*, and
AC10 requires both the README and the `/history` page to say exactly that —
"every signed-in user sees every question" is a sentence a reader deserves
before they type a question into it.

**No cache partitioning** (Q-E). `cache_key(question, schema_fp, prompt_fp)`
keeps its signature. A test pins that it takes no user argument and that two
identities get the same key for the same question — which is the property that
keeps a hit free, and the property that must be revisited the instant a cache
entry starts carrying anything user-specific.

---

## 5. Files and decomposition

| task | what | verified by |
|---|---|---|
| **T1** | Charter §6 row for Iteration 10; note the scope against B-11 | Read it. **Pause.** |
| **T2** | `api/http/auth.py` — the credential map and constant-time verify, **not wired** | Unit tests; timing defence mutated |
| **T3** | The `authed_client` fixture; move seven files onto it | Suite green, gate still not live |
| **T4** | Wire the middleware; `tests/test_auth.py`; the realm header | **Remove the middleware — the negative suite must go red. Pause.** |
| **T5** | The route-completeness test, covering routes *and* mounts | Add an unprotected route; the test must fail |
| **T6** | Q-E's cache test; `.env.example`, compose, README, the `/history` note | Round trip with `curl -u`; docs read |
| **T7** | Board, `HANDOFF.md`, non-destructive boot, PR | Full suite; `down`/`up`. **Pause.** |

**New:** `api/http/auth.py`, `tests/test_auth.py`.
**Edited:** `api/main.py`, `tests/conftest.py`, `.env.example`,
`docker-compose.yml`, `README.md`, `api/web/history.js` (or `index.html`, per
where the note lands), `specs/000-project.md`, `HANDOFF.md`, and the seven test
files from §2.6.

---

## 6. Decisions

- **D-1 — Middleware or route dependencies?** Measured above: an app-level
  dependency leaves `/static/*` open at 200.

  *My lean: middleware, plus the completeness test of §2.4.* It is the only
  mechanism that covers a mounted sub-app, it cannot be forgotten when a route
  is added, and one gate is easier to reason about than a decorator that must
  appear in seven places. What it costs is that the exemption becomes a path
  check rather than an absence of a decorator, which is why the completeness
  test is part of the same decision rather than a nicety.

- **D-2 — What happens when `QUERYPILOT_USERS` is unset?** This is the decision
  I would most like you to take rather than infer, because both answers are
  defensible and one of them is dangerous.

  *My lean: **fail closed** — no identities configured means every protected
  route returns 401, and `/health` still answers so the container can report
  what is wrong.* Failing open would mean a deployment with a typo in the
  variable name is silently public, which is the same shape as `011` §2.1's
  green-and-empty pipeline: the mechanism exists, it is switched off, and
  nothing says so. The cost is real and falls on the developer: `docker compose
  up` with no `.env` entry gives a page that will not load until they set one,
  so T6 must make `.env.example` and the README unmistakable. I would **not**
  ship a default credential in compose — a default secret is worse than none,
  because it survives into a deployment.

- **D-3 — Is `/static/*` protected?** Middleware makes it nearly free either way.

  *My lean: protect it.* The scripts hold nothing secret, so this is not about
  the content — it is about having one rule with one exemption instead of two
  rules. A browser that has authenticated for `GET /` sends the header on the
  script requests automatically, so nothing about the page changes.

- **D-4 — Does `/health` keep reporting what it reports?** It is about to become
  the only endpoint an unauthenticated caller can reach, and it currently
  returns the database user, the database name, the public table count and
  whether the history store is writable.

  *My lean: leave it exactly as it is, and record the decision.* Every field is
  there because Iteration 0 through 8 needed it, the compose healthcheck and CI
  both read it, and none of it is a secret worth the churn — `querypilot_ro` and
  `chinook` are in the README. I raise it because "the only public endpoint" is
  a different job from "the endpoint the healthcheck reads", and if you would
  rather it degraded to `{"status": "ok"}` for anonymous callers and stayed full
  for authenticated ones, that is cheap to build now and awkward later.

- **D-5 — One credential map, or a name per caller?** Q-A chose named
  identities; the plan reads them from one JSON object in one variable.

  *My lean: one variable, `QUERYPILOT_USERS`, holding a JSON object.* It is a
  single thing to set in a secrets store, it round-trips through compose without
  ceremony, and adding a caller is an edit rather than a deployment change. The
  weakness is that a malformed JSON value fails for every identity at once,
  which D-2's fail-closed makes a hard outage — so `load_identities` must fail
  **loudly at startup**, naming the variable, rather than silently yielding an
  empty map that looks like "no identities configured".

- **D-6 — Do the seven files move wholesale to `authed_client`?** 100 call sites,
  and a mechanical rename is tempting.

  *My lean: yes, wholesale, with one exception made deliberately.* Every test
  that is not *about* authentication should authenticate and say nothing more
  about it. The exception is `tests/test_ask_endpoint.py`'s two AC4 structural
  tests, which parse `api/main.py` and never make a request — they need no
  client at all and should not acquire one. If any other test turns out to
  depend on being anonymous, that is a finding worth reporting rather than
  papering over.

---

## 7. Risks

- **T4 is the task where this can go quietly wrong.** By then the tests
  authenticate, so switching the gate on changes nothing visible — and a gate
  that is broken, mis-wired or exempting everything looks identical to one that
  works. The negative suite lands in the same task and the mutation is
  mandatory: remove the middleware, and `tests/test_auth.py` must go red. Until
  that has been *seen*, AC1 is unmet.
- **The completeness test can go vacuous the way absence assertions do.** If it
  walks `app.routes` and finds nothing, it passes. It needs its own
  counter-assertion — the number of routes it examined — for the reason the
  AC13 comment-stripper needed one.
- **Fail-closed plus a typo is a total outage**, and it is the failure mode
  D-2 chooses on purpose. AC9's "set and rotate without editing code" is what
  makes it survivable; if the error message does not name the variable, it is
  not survivable.
- **Basic over plaintext protects nothing on the wire.** It is a spend gate, not
  transport security, and `013-auth.md` §4 already says TLS belongs to B-11.
  Worth repeating in the README rather than letting a reader infer more safety
  than exists.
- **The browser prompt is a UX cliff.** A wrong password in a Basic dialog is
  hard to clear without closing the tab, and there is no "log out". That is the
  price Q-C accepted for zero new dependencies and zero frontend change, and the
  README should say so rather than let it be discovered.
