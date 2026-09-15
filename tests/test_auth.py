"""Iteration 10 T4 — the gate, proven by being refused.

**This file is the reason T3's fixture was not enough.** After T3 every one of
the 100 endpoint calls in the suite authenticates, so switching the gate on
changes nothing visible: a gate that is broken, mis-wired, or exempting every
path looks exactly like one that works. `013-auth-plan.md` §7 names this as the
task where the iteration can go quietly wrong, and the answer is that the
negative suite lands in the same commit as the wiring, with a mandatory
mutation — **remove the middleware and every test below must go red.**

Three properties, and the third is the one worth having:

1. every protected route refuses an unauthenticated caller with **401**;
2. the four ways of failing are **indistinguishable** — same status, same body,
   same headers (AC6);
3. a refused `POST /ask` **spends nothing** (AC1, AC2), asserted by a provider
   that raises if it is touched and by the history store staying empty, not by
   trusting that a 401 implies no work happened.

**The gate itself needs no database** — it runs above routing, so a refused
request never reaches a handler, which is itself part of what is asserted here.
That is a property of the code and **not** of this file's behaviour: the
`configured_database` fixture is session-scoped and autouse, so a stack that is
down skips this file along with every other. Measured rather than assumed: with
the database stopped, all 64 tests below skip. Worth knowing before reading a
green run as proof the gate was exercised.
"""

from __future__ import annotations

import base64
import json
import pathlib
import re

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from api.http.auth import OPEN_PATHS, USERS_ENV
from api.main import app
from tests.conftest import TEST_SECRET, TEST_USER

# --- what the gate must cover -------------------------------------------------
#
# Enumerated by hand here and asserted *complete* against `app.routes` at T5.
# The two mechanisms answer different questions: this one says each of these
# refuses, and T5's says nothing else exists that this list has forgotten.

#: `(method, path, body)` for every path a caller must present a credential to
#: reach. `/static/app.js` is in the list because resolved D-3 protects the
#: mount, and because a route dependency provably could not — `013-auth-plan.md`
#: §1 measured `GET /static/x.js` returning **200** behind an app-level deny-all.
PROTECTED = [
    ("GET", "/", None),
    ("GET", "/quota", None),
    ("GET", "/history", None),
    ("GET", "/history/data", None),
    ("GET", "/static/app.js", None),
    ("GET", "/static/styles.css", None),
    ("POST", "/ask", {"question": "How many tracks are in the library?"}),
    ("POST", "/feedback", {"id": "any-id", "rating": 1}),
]

#: The four ways in, all of which must produce the identical refusal (AC6).
#:
#: Expressed as request kwargs so each is sent the way a real client would send
#: it: `auth=` lets `httpx` do the Basic encoding, and the malformed case is a
#: raw header because no client library would produce one.
CREDENTIAL_MODES = {
    "none": {},
    "malformed-header": {"headers": {"Authorization": "Basic !!!not-base64!!!"}},
    "unknown-user": {"auth": ("nobody-configured", TEST_SECRET)},
    "wrong-secret": {"auth": (TEST_USER, "not-the-secret")},
}


def _send(client: TestClient, method: str, path: str, body, **kwargs):
    """One request, in whichever credential mode `kwargs` carries."""
    if method == "POST":
        return client.post(path, json=body, **kwargs)
    return client.get(path, **kwargs)


@pytest.fixture
def unusable_provider(monkeypatch, provider_that_must_not_be_called):
    """A provider that raises, installed **where `api/main.py` looks it up**.

    `provider_that_must_not_be_called` alone is not sufficient here and
    `013-auth-plan.md` §3 was wrong to imply it was. It patches
    `api.llm.factory.get_provider`, but `api/main.py` did
    `from api.llm.factory import get_provider` at import time, so the endpoint
    holds its own reference and never consults the factory again — the same trap
    `_answer` in `tests/test_ask_endpoint.py` documents.

    Both are installed. The factory patch covers anything that resolves the
    provider late; this one covers the endpoint, which is the path under test.
    """
    monkeypatch.setattr("api.main.get_provider", lambda: provider_that_must_not_be_called)
    return provider_that_must_not_be_called


# --- AC1: every protected route refuses, in every mode -----------------------


@pytest.mark.parametrize("mode", list(CREDENTIAL_MODES), ids=list(CREDENTIAL_MODES))
@pytest.mark.parametrize(
    "method,path,body", PROTECTED, ids=[f"{m}-{p}" for m, p, _ in PROTECTED]
)
def test_every_protected_route_refuses_every_bad_credential(
    anonymous_client, method, path, body, mode
):
    """AC1, walked across the whole surface rather than sampled.

    Sampling one route is how `/static/*` stayed open for a whole iteration in
    the dependency-injection version of this design: the sample passed and the
    mount did not exist in it.
    """
    response = _send(anonymous_client, method, path, body, **CREDENTIAL_MODES[mode])

    assert response.status_code == 401, (
        f"{method} {path} answered {response.status_code} with {mode} "
        f"credentials; it is not behind the gate"
    )


@pytest.mark.parametrize(
    "method,path,body", PROTECTED, ids=[f"{m}-{p}" for m, p, _ in PROTECTED]
)
def test_the_refusal_prompts_a_browser(anonymous_client, method, path, body):
    """Resolved Q-C's entire argument for Basic — no login page, no cookie, no
    JavaScript change — depends on the browser putting up its own dialog, and it
    only does that for a 401 carrying `WWW-Authenticate`.

    Asserted on every route rather than once, because the page and its scripts
    are the ones a human meets and they are served by three different handlers.
    """
    response = _send(anonymous_client, method, path, body)

    assert response.headers["www-authenticate"] == 'Basic realm="QueryPilot"'


# --- AC6: the four modes are indistinguishable -------------------------------


def test_all_four_failures_are_byte_identical(anonymous_client):
    """AC6. A gate that says *"unknown user"* tells an attacker the name half of
    their guess was wrong, which turns a two-dimensional search into two
    one-dimensional ones — and tells them the endpoint is worth attacking.

    Compared as raw bytes and as the exact header set, not as a parsed body:
    a difference in whitespace or in `Content-Length` is still a difference an
    attacker can measure.
    """
    seen = {}
    for mode, kwargs in CREDENTIAL_MODES.items():
        response = anonymous_client.get("/quota", **kwargs)
        seen[mode] = (
            response.status_code,
            response.content,
            response.headers["www-authenticate"],
            response.headers["content-type"],
        )

    distinct = set(seen.values())
    assert len(distinct) == 1, (
        f"the four failure modes produce {len(distinct)} distinct responses, so "
        f"a caller can tell them apart: {seen}"
    )


def test_the_refusal_says_nothing_about_what_went_wrong(anonymous_client):
    """The body is checked for the words that would leak the shape of the
    guess. `tests/test_auth_logic.py` asserts this of the constant; this asserts
    it of what actually crosses the wire, because the two are only the same
    thing while nothing wraps the response on the way out.

    **The status assertion is the load-bearing line**, and it was added after
    the T4 mutation caught this test passing without it. With the middleware
    removed, `/quota` answered **200** with a quota payload that happens to
    contain none of the words below — so the test was green against no gate at
    all. An absence assertion that never established it was reading the right
    response is the same defect this project has now been caught by five times.
    """
    response = anonymous_client.get("/quota", auth=("nobody", "nothing"))

    assert response.status_code == 401, "not the refusal, so the body below is not it"

    body = response.text.lower()
    for leak in ("user", "name", "password", "secret", "unknown", "exist", "wrong"):
        assert leak not in body, f"the refusal body hints at {leak!r}: {body!r}"


def test_a_401_is_not_a_failed_question(anonymous_client):
    """`013-auth.md` §5 makes this a contract.

    `api/http/errors.py` maps *answer* failures onto status codes, and every one
    of them returns a payload with `ok`, `category` and `error` so the page can
    render it. An unauthorised caller has not asked a question — there is
    nothing to report `ok: false` about, and a 401 wearing that shape would tell
    the page to render a failure card for a request the agent never saw.
    """
    payload = anonymous_client.post("/ask", json={"question": "anything"}).json()

    assert set(payload) == {"detail"}, (
        f"the 401 body carries answer-shaped fields: {sorted(payload)}"
    )


# --- AC2: a refused request spends nothing -----------------------------------


def test_a_refused_question_never_reaches_the_provider(anonymous_client, unusable_provider):
    """**AC1's real content and AC2's assertion.** The refusal is the point; the
    unspent token is the property worth asserting.

    Proven by a provider that *raises* rather than by counting calls afterwards:
    a counter left at zero is also what a test that forgot to make the request
    would show, while an exception can only be avoided by the call not
    happening.
    """
    response = anonymous_client.post("/ask", json={"question": "How many tracks?"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_a_refused_question_leaves_no_trace_of_work(anonymous_client, unusable_provider):
    """The independent half of AC2, and it needs no provider at all.

    `POST /ask` writes a history row for every question it processes, including
    failed ones — that is what `011` AC4 built the reader on. So an empty store
    after a refused request is evidence that no request was *processed*, arrived
    at from a different direction than the provider assertion above.

    The store is the tmp one `isolated_history_store` installs, so this counts
    what this test caused and nothing else.
    """
    from api.store import history

    anonymous_client.post("/ask", json={"question": "How many tracks?"})

    assert history.recent(limit=10) == [], (
        "a refused request wrote a history row, so the handler ran"
    )


def test_a_refused_question_reports_no_usage(anonymous_client, unusable_provider):
    """AC2's *zero tokens*, asserted on the response rather than inferred.

    There is no `usage` block to be zero, because there is no answer payload at
    all — which is the strongest form of the claim and the one worth pinning:
    a 401 that carried a usage block would mean something had been costed.
    """
    payload = anonymous_client.post("/ask", json={"question": "?"}).json()

    assert "usage" not in payload


# --- AC3: what stays open, and only that -------------------------------------


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_health_answers_without_a_credential(anonymous_client):
    """AC3, and `013-auth.md` §2.7 measured that it must.

    The compose healthcheck probes `/health` with a bare `urlopen` and no
    credentials, so a gate here makes the container permanently unhealthy — the
    container reports itself broken *because* it is correctly secured.

    Asserted as "not 401, and the handler ran" rather than as 200, because the
    status depends on the database being up and this file's whole point is that
    the gate is decided before any handler needs one. `/health`'s own 200/503
    behaviour is tested in `tests/test_ask_recording.py`.
    """
    response = anonymous_client.get("/health")

    assert response.status_code != 401
    assert "database" in response.json(), "the handler did not run"


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_health_with_a_trailing_slash_is_open_too(anonymous_client):
    """The hazard `013-auth-plan.md` §2.4 named before the code existed.

    The middleware runs above routing, so FastAPI's own trailing-slash redirect
    never gets the chance to normalise `/health/` into `/health`. An exemption
    written as a bare `==` would refuse the healthcheck the day somebody put a
    slash in the compose file.
    """
    assert anonymous_client.get("/health/").status_code != 401


def test_nothing_else_is_open(anonymous_client):
    """The counter-assertion, in the shape `013-auth-plan.md` §7 requires.

    An exemption list is an absence assertion wearing a dict, and this project
    has been caught four times by an absence assertion that could not fail. If
    `OPEN_PATHS` ever grows a second entry, this test fails until somebody
    writes down why — which is the decision AC3 wants recorded in the charter
    rather than discovered in a diff.
    """
    assert set(OPEN_PATHS) == {"/health"}, (
        f"something new is unauthenticated: {sorted(set(OPEN_PATHS) - {'/health'})}"
    )
    assert len(PROTECTED) >= 8, (
        f"the protected list has shrunk to {len(PROTECTED)} entries; a route "
        f"removed from it stops being tested rather than stops being protected"
    )


# --- T5: the completeness walk ------------------------------------------------
#
# `PROTECTED` above is a hand-written list, and a hand-written list is exactly
# what forgets the route somebody adds next year. This section asks the *app*
# what it exposes and probes every answer, so the two mechanisms fail in
# different ways: the list above says each of these refuses, and the walk below
# says nothing exists that the list has forgotten.

#: The paths a walk of `app.routes` must find and probe. A floor rather than an
#: equality, so adding a route does not fail this for the wrong reason — but a
#: walk that quietly stops discovering things drops below it and goes red.
#:
#: **Measured**: 11 method/path pairs from `app.routes` plus 1 mount probe. Four
#: of the eleven are FastAPI's own — `/openapi.json`, `/docs`,
#: `/docs/oauth2-redirect` and `/redoc` — which is precisely the kind of surface
#: a hand-written list omits, and `/openapi.json` publishes the entire API shape
#: to anyone who asks.
ROUTES_FLOOR = 12

#: The one path that may answer without a credential (AC3). Written literally
#: here and **not** read from `auth.OPEN_PATHS`, which is the whole point: a test
#: that derives its expectation from the thing under test cannot notice the
#: thing under test changing. Exempting a second path fails this test until
#: somebody comes here and writes down the decision.
EXPECTED_OPEN = {"/health"}


def _probe_path(route) -> str:
    """A concrete URL for one route, including one under a mount.

    A mount is a sub-application with no path of its own to probe, so a real
    file inside it is discovered from the `StaticFiles` directory rather than
    hardcoded — naming `app.js` here would keep passing after somebody renamed
    it, which is a test that has stopped checking the mount.
    """
    if isinstance(route, Mount):
        directory = pathlib.Path(route.app.directory)
        served = sorted(p.name for p in directory.iterdir() if p.is_file())
        assert served, f"the mount at {route.path} serves nothing to probe"
        return f"{route.path}/{served[0]}"

    # No route takes a path parameter today. Substituting rather than skipping,
    # so the first one that does is probed instead of silently dropped.
    return re.sub(r"\{[^}]+\}", "1", route.path)


def _declared_endpoints() -> list[tuple[str, str]]:
    """`(method, path)` for everything the app exposes, asked of the app."""
    found = []
    for route in app.routes:
        if isinstance(route, Mount):
            found.append(("GET", _probe_path(route)))
            continue
        for method in sorted(set(route.methods or []) - {"HEAD", "OPTIONS"}):
            found.append((method, _probe_path(route)))
    return found


def test_the_walk_finds_more_than_the_hand_written_list(anonymous_client):
    """**The counter-assertion, in the shape `013-auth-plan.md` §7 demands.**

    A completeness test that walks `app.routes` and finds nothing passes, which
    is the absence-assertion failure wearing a `for` loop. So this pins that the
    walk found at least as many endpoints as were there when it was written, and
    that it found things `PROTECTED` does not list — because a walk that only
    rediscovered the hand-written list would be an expensive way to run the same
    test twice.
    """
    walked = _declared_endpoints()

    assert len(walked) >= ROUTES_FLOOR, (
        f"the walk found {len(walked)} endpoints and there were {ROUTES_FLOOR} "
        f"when it was written; it has stopped discovering things"
    )

    listed = {(method, path) for method, path, _ in PROTECTED}
    assert set(walked) - listed - {("GET", "/health")}, (
        "the walk discovered nothing the hand-written list had not already; it "
        "is not earning its keep"
    )


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_every_endpoint_the_app_declares_refuses_an_anonymous_caller(anonymous_client):
    """AC3, asked of the app rather than of a list somebody maintains.

    **The expectation is written literally and not derived from
    `auth.OPEN_PATHS`.** Reading the exemption set here would make the test
    agree with the code by construction: adding `/openapi.json` to the exemptions
    would change both sides at once and stay green. Comparing against
    `EXPECTED_OPEN` means a second exemption fails the suite until somebody
    records the decision AC3 asks for.

    FastAPI's own four routes are covered by this and by nothing else in the
    file. `/openapi.json` is the one that matters: it is a complete description
    of every endpoint, its parameters and its response shapes.
    """
    answered_without_a_credential = set()

    for method, path in _declared_endpoints():
        response = _send(anonymous_client, method, path, {})
        if response.status_code != 401:
            answered_without_a_credential.add(path)

    assert answered_without_a_credential == EXPECTED_OPEN, (
        f"open: {sorted(answered_without_a_credential)}; "
        f"expected exactly {sorted(EXPECTED_OPEN)}"
    )


def test_every_exemption_is_one_the_app_actually_serves():
    """The other direction, and it is the one that rots quietly.

    An exemption for a path that no longer exists is dead configuration — it
    looks like a considered decision and guards nothing, and the next reader
    trusts it. If `/health` is ever renamed, this fails rather than leaving a
    stale hole in the list for a future route to fall into.
    """
    declared = {path for _, path in _declared_endpoints()}

    for exempt in OPEN_PATHS:
        assert exempt in declared, (
            f"{exempt} is exempt from the gate and the app does not serve it"
        )


# --- the gate is not simply a deny-all ---------------------------------------


def test_a_valid_credential_is_let_through(authed_client):
    """**Without this the whole file passes on a middleware that refuses
    everything**, which is the vacuous-gate failure in its most embarrassing
    form: 100 per cent of the negative suite green, and the product broken.

    The rest of the suite covers this a hundred times over, but a reader of this
    file should not have to know that to trust it.
    """
    assert authed_client.get("/quota").status_code == 200


def test_the_credential_is_checked_and_not_merely_present(authed_client):
    """One client, two credentials — the second is refused.

    A gate that accepted any syntactically valid `Authorization` header would
    pass every test above except this one: the malformed and missing cases would
    still fail, and the two wrong-credential cases would silently pass for the
    wrong reason.
    """
    good = authed_client.get("/quota", auth=(TEST_USER, TEST_SECRET))
    bad = authed_client.get("/quota", auth=(TEST_USER, TEST_SECRET + "x"))

    assert good.status_code == 200
    assert bad.status_code == 401


# --- fail closed: the unconfigured deployment (resolved D-2) ------------------


@pytest.fixture
def unconfigured_client(monkeypatch):
    """A client against an app with **no credential map at all**.

    Distinct from `anonymous_client`, which configures identities and then
    presents none. This is the deployment that shipped with a typo in the
    variable name, and D-2 chose what happens to it.
    """
    monkeypatch.delenv(USERS_ENV, raising=False)
    return TestClient(app)


@pytest.mark.parametrize(
    "method,path,body", PROTECTED, ids=[f"{m}-{p}" for m, p, _ in PROTECTED]
)
def test_an_unconfigured_deployment_refuses_everything(
    unconfigured_client, method, path, body, unusable_provider
):
    """**Fail closed** (resolved D-2), and the alternative is the reason.

    Failing open would mean a deployment that never set `QUERYPILOT_USERS` — or
    set `QUERYPILOT_USER`, singular — is silently public while looking exactly
    like a secured one. That is `011-ship.md` §2.1's green-and-empty pipeline in
    a second costume: the mechanism is present, it is switched off, and nothing
    says so.

    Presenting *correct* credentials changes nothing, because there is nothing
    for them to be correct against.
    """
    response = _send(
        unconfigured_client, method, path, body, auth=(TEST_USER, TEST_SECRET)
    )

    assert response.status_code == 401


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_an_unconfigured_deployment_still_says_why(unconfigured_client):
    """The other half of D-2, and what makes fail-closed survivable.

    D-2 chose 401-everything over refusing to boot *so that an operator can ask
    the container what is wrong*. If the answer were only "401" the choice would
    have bought nothing: a container that will not start and one that refuses
    everything for an unstated reason are equally undiagnosable from outside.

    So `/health` stays open, reports `configured: false`, and the message names
    the variable to set (AC9 — set and rotate without editing code).
    """
    payload = unconfigured_client.get("/health").json()

    assert payload["auth"]["configured"] is False
    assert USERS_ENV in payload["auth"]["error"], (
        f"the message does not name the variable to set: {payload['auth']['error']!r}"
    )


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_a_configured_deployment_reports_itself_configured(anonymous_client):
    """The positive half, so the field above cannot be a constant `false`."""
    payload = anonymous_client.get("/health").json()

    assert payload["auth"] == {"configured": True, "error": ""}


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_the_health_payload_never_carries_the_secret(unconfigured_client, monkeypatch):
    """AC5, at the one place a credential could plausibly reach a public body.

    `/health` is the only endpoint an anonymous caller can reach, and it now
    reports a parse failure whose input is the credential map. A message built
    from `exc.doc` — which `json.JSONDecodeError` carries and which is the whole
    raw value — would publish every secret in it to an unauthenticated GET.
    """
    monkeypatch.setenv(USERS_ENV, '{"admin": "hunter2-do-not-leak" oops}')

    body = unconfigured_client.get("/health").text

    assert "hunter2-do-not-leak" not in body
    assert '{"admin"' not in body


# --- a rotated credential needs no restart -----------------------------------


def test_a_rotated_secret_takes_effect_without_a_restart(monkeypatch):
    """The property the identity memo is keyed on a *value* to get (T4).

    The obvious implementation — load once into a module global at import or at
    startup — makes rotating a secret require a restart, and a secret that needs
    a restart to rotate is a secret that does not get rotated. Keyed on the raw
    configuration string, the new value is simply a different key.

    Driven through the app rather than against the helper, because the helper
    already has its own test in `tests/test_auth_logic.py` and this is the
    adoption question: does the *gate* see the rotation.
    """
    client = TestClient(app)

    monkeypatch.setenv(USERS_ENV, json.dumps({"analyst": "first-secret"}))
    assert client.get("/quota", auth=("analyst", "first-secret")).status_code == 200

    monkeypatch.setenv(USERS_ENV, json.dumps({"analyst": "second-secret"}))
    assert client.get("/quota", auth=("analyst", "first-secret")).status_code == 401
    assert client.get("/quota", auth=("analyst", "second-secret")).status_code == 200


# --- the header, end to end ---------------------------------------------------


def test_a_hand_built_basic_header_is_accepted(anonymous_client):
    """Not everything is `httpx`.

    `curl -u`, a browser dialog and the compose healthcheck all build the header
    themselves, so the gate is asserted against the wire format rather than
    against one client library's encoder — which is the same reason the shape
    classifier is tested on values and not on a `TestClient` response.
    """
    encoded = base64.b64encode(f"{TEST_USER}:{TEST_SECRET}".encode()).decode()

    response = anonymous_client.get(
        "/quota", headers={"Authorization": f"Basic {encoded}"}
    )

    assert response.status_code == 200
