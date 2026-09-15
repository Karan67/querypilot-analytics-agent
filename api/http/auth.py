"""Who is allowed to spend the provider's quota — `specs/013-auth.md`.

**This module is the implementation; `api/main.py` holds the wiring.** It is the
rule `api/agent/tools.py` states about itself, applied here because the
interesting parts — parsing a credential map and comparing a secret without
leaking timing — are testable with no HTTP in the way, and because a gate whose
logic lives inside a middleware closure cannot be unit tested at all.

Nothing here imports FastAPI, and nothing here reads the environment. Both are
the caller's job, which is what lets every branch below be driven from a test
without a running app or a patched `os.environ`.

**T4 added three more pieces and kept that rule.** `parse_basic` turns an
`Authorization` header into a name and a secret, `current_identities` memoises
the parse, and `OPEN_PATHS` names what the gate does not cover. All three are
here rather than inside the middleware for the same reason as the other two: a
malformed header, a rotated credential map and an exempt path are each a branch
somebody has to be able to drive from a test, and a branch inside a middleware
closure cannot be reached without an HTTP request.

---

## Fail closed, and where "closed" is decided

`load_identities` **raises** on anything it cannot turn into a usable credential
map — missing, blank, malformed, empty, or holding an entry that would
authenticate nobody safely. It never returns an empty map, because an empty map
is indistinguishable from a working one that happens to reject everybody, and
that is exactly the shape `011-ship.md` §2.1 measured on the test suite: a
mechanism that is switched off looks identical to one that is working.

**Raising is not the same as refusing to boot**, and the distinction is
deliberate. Resolved D-2 requires that a misconfigured deployment still answers
`/health`, so that an operator can see *why* nothing works. So this function
raises, and the caller catches once at startup and remembers that it is
unconfigured — every protected route then returns 401 while `/health` keeps
reporting. The loud failure belongs in the logs and the health payload, not in a
process that will not start.

`GROQ_API_KEY` is handled the same way and for the same reason, recorded in
`docker-compose.yml`: *"A container that refused to boot without an LLM key
would make the database work unreachable too."*

## What is never in an exception message

The raw value. A `json.JSONDecodeError` carries the whole document on `.doc`,
so interpolating the exception object's attributes — or the input — into a
message would write the secret into a log. Measured: `str(e)` alone is
`"Expecting ',' delimiter: line 1 column 38 (char 37)"` and holds no document
text, which is why only `str(e)` is ever quoted below.

This is the rule `GroqProvider._safe_message` already follows for the API key,
and charter §5 states it absolutely: the credential never appears in chat, a
commit, a log, or an error message.
"""

from __future__ import annotations

import base64
import json
import secrets

#: The environment variable holding the credential map, as JSON.
#:
#: One variable rather than one per identity (resolved D-5): a single thing to
#: put in a secrets store, and adding a caller is an edit rather than a
#: deployment shape change.
USERS_ENV = "QUERYPILOT_USERS"

#: Sent with every refusal. Without it a browser will not prompt, and resolved
#: Q-C's whole argument for Basic — no login page, no cookie, no JavaScript
#: change — depends on the browser prompting.
UNAUTHORIZED_HEADERS = {"WWW-Authenticate": 'Basic realm="QueryPilot"'}

#: The body of every refusal, whatever went wrong.
#:
#: **One body for four failure modes** (AC6): no header, a malformed header, an
#: unknown name, and a known name with the wrong secret. A gate that
#: distinguishes them tells an attacker which half of the guess was right, and
#: "no such user" tells them the endpoint is worth attacking at all.
UNAUTHORIZED_BODY = {"detail": "Unauthorized"}

#: Compared against when the presented name is unknown, so that the miss costs
#: the same as a hit (see `verify`). Its content is irrelevant and it is not a
#: secret; only the fact that a comparison happens matters.
_DUMMY_SECRET = b"$dummy$never$matches$any$configured$secret$"

#: What the gate does not cover, and **why** — AC3 requires the reason, not just
#: the exemption.
#:
#: A dict rather than a set because the reason is the load-bearing half: an
#: exemption with no stated justification is how a second one gets added next
#: year on the grounds that a first one exists. T5's completeness test reads
#: this, so a route that is neither protected nor listed here fails the suite.
OPEN_PATHS: dict[str, str] = {
    "/health": (
        "the compose healthcheck probes it with a bare urlopen and no "
        "credentials; a gate here makes the container permanently unhealthy "
        "(013-auth.md §2.7 measured this)"
    ),
}


class AuthConfigurationError(Exception):
    """The credential map is missing or unusable.

    A distinct type rather than `ValueError`, so the caller can catch exactly
    this at startup and degrade to *refuse everything protected, keep answering
    `/health`* without also swallowing a genuine bug in its own wiring.
    """


def load_identities(raw: str | None) -> dict[str, str]:
    """Parse the credential map, or raise saying what to fix.

    Takes the raw string rather than reading `USERS_ENV` itself, so a test can
    drive every branch without touching the environment, and so the caller
    decides where configuration comes from.

    Rejects, loudly, in every case where the alternative is a map that looks
    configured and is not:

    - **missing or blank** — the unconfigured case, which must never silently
      become "no identities, so nobody gets in quietly";
    - **not valid JSON**;
    - **not a JSON object** — a list or a string parses fine and means nothing
      here;
    - **empty** — `{}` authenticates nobody, which is the same outage as being
      unset and should read the same way;
    - **a non-string name or secret** — `{"admin": 1234}` is a configuration
      somebody meant to work;
    - **an empty name or secret**. This one is not pedantry:
      `secrets.compare_digest(b"", b"")` is **True**, so an entry with an empty
      secret authenticates any caller who sends an empty password. Measured, not
      assumed.

    Returns a plain `dict[str, str]`, which the caller is expected to hold for
    the process's lifetime rather than re-parse per request.
    """
    if raw is None or not raw.strip():
        raise AuthConfigurationError(
            f"{USERS_ENV} is not set, so no request can be authorised. Set it to "
            f'a JSON object of name to secret, e.g. {USERS_ENV}=\'{{"analyst": '
            f'"<a long random string>"}}\'.'
        )

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        # `str(exc)` only -- never `exc.doc`, which is the whole raw value and
        # would put a secret in the log.
        raise AuthConfigurationError(
            f"{USERS_ENV} is not valid JSON: {exc}. Expected a JSON object of "
            f"name to secret."
        ) from None

    if not isinstance(parsed, dict):
        raise AuthConfigurationError(
            f"{USERS_ENV} must be a JSON object of name to secret, not a "
            f"{type(parsed).__name__}."
        )

    if not parsed:
        raise AuthConfigurationError(
            f"{USERS_ENV} is an empty object, so no request can be authorised. "
            f"Add at least one name and secret."
        )

    identities: dict[str, str] = {}
    for name, secret in parsed.items():
        # The name is safe to quote; a secret never is, so the message names the
        # offending *key* and says nothing about its value.
        if not isinstance(name, str) or not name:
            raise AuthConfigurationError(
                f"{USERS_ENV} has an entry whose name is not a non-empty string."
            )
        if not isinstance(secret, str):
            raise AuthConfigurationError(
                f"{USERS_ENV} entry {name!r} has a {type(secret).__name__} "
                f"secret; it must be a string."
            )
        if not secret:
            raise AuthConfigurationError(
                f"{USERS_ENV} entry {name!r} has an empty secret, which would "
                f"authorise any caller sending an empty password."
            )
        identities[name] = secret

    return identities


def verify(identities: dict[str, str], username: str, password: str) -> str | None:
    """The name that authenticated, or `None`.

    **The obvious half is `secrets.compare_digest` on the secret.** The half
    that gets missed is the *name*: a dictionary miss returns immediately, while
    a hit goes on to compare a password, so an unknown name answers measurably
    faster than a known one with the wrong secret — and that difference
    enumerates the configured identities one guess at a time.

    So an unknown name compares the presented password against `_DUMMY_SECRET`
    and throws the result away. Both paths do one comparison, and
    `tests/test_auth_logic.py` asserts that by counting calls rather than by
    timing anything, because a timing assertion in a unit test is a flake.

    **What this does and does not buy.** `compare_digest` is content-independent,
    so it does not leak *which byte* differed. It is not length-independent, and
    neither is the dummy path — a caller can still learn something about secret
    lengths by measurement. Fixing that means hashing both sides to a fixed
    width, which is a real option and a different decision; what is claimed here
    is only that the presence-or-absence of a comparison no longer distinguishes
    a known name from an unknown one.

    Both sides are encoded to UTF-8 first. `compare_digest` raises `TypeError`
    on `str` arguments containing non-ASCII, so a password with an accent in it
    would otherwise be a 500 rather than a 401 -- measured, not assumed.
    """
    presented = password.encode("utf-8")
    expected = identities.get(username)

    if expected is None:
        secrets.compare_digest(presented, _DUMMY_SECRET)
        return None

    if secrets.compare_digest(presented, expected.encode("utf-8")):
        return username

    return None


def normalise_path(path: str) -> str:
    """The path an exemption is decided on.

    A trailing slash is stripped because FastAPI would redirect `/health/` to
    `/health` anyway, and the middleware runs *before* routing — so without this
    the healthcheck's URL and the same URL with a slash would get different
    answers from the gate. `013-auth-plan.md` §2.4 names this exact hazard:
    *"a string comparison that will eventually be wrong about `/health/`"*.

    `"/"` survives, because the page is a protected path and not an empty one.
    """
    stripped = path.rstrip("/")
    return stripped or "/"


def is_open(path: str) -> bool:
    """Whether this path is reachable without a credential.

    Membership in `OPEN_PATHS`, and deliberately not a prefix match: a prefix
    would make `/health-internal` and `/healthz` open by accident, and the one
    exemption this project has does not need a wildcard.
    """
    return normalise_path(path) in OPEN_PATHS


def parse_basic(header: str | None) -> tuple[str, str] | None:
    """`(username, password)` from an `Authorization` header, or `None`.

    **Every malformed shape returns `None` rather than raising**, because the
    caller turns `None` into the same 401 as a wrong password — AC6 says a
    caller cannot tell which half was wrong, and a header that crashes the
    middleware would be a 500 that says *something about this input was
    special*.

    The shapes that return `None`: absent, empty, a scheme that is not `Basic`,
    a scheme with nothing after it, base64 that does not decode, bytes that are
    not UTF-8, and a decoded value with no colon in it. A password containing a
    colon is **not** malformed — RFC 7617 splits on the first one only, which
    `partition` does.
    """
    if not header:
        return None

    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None

    try:
        # `validate=True` so that a header of stray punctuation is refused here
        # rather than silently decoding to rubbish and failing as a wrong
        # password. `binascii.Error` is a `ValueError`, so one clause covers
        # both it and a bad padding length.
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None

    username, separator, password = decoded.partition(":")
    if not separator:
        return None

    return username, password


#: The last raw configuration and what it parsed to. Keyed on the **raw string**,
#: which is what makes this self-invalidating: a rotated `QUERYPILOT_USERS` is a
#: different key and re-parses, so a stale map cannot outlive the value it came
#: from. A test that sets the variable and makes a request gets the map it just
#: set, with no restart and no hook call.
_memo: tuple[str | None, dict[str, str]] | None = None


def current_identities(raw: str | None) -> dict[str, str]:
    """`load_identities`, parsed once per distinct configuration.

    The middleware calls this on every protected request, so the parse is
    memoised — but on the value, never on time or on a boolean "loaded" flag.
    Both of those alternatives make a rotated credential require a restart, and
    a restart to rotate a secret is how a secret ends up not being rotated.

    Raises exactly what `load_identities` raises, and a failure is **not**
    memoised: a deployment fixed by correcting the environment recovers on the
    next request rather than staying broken until somebody notices.
    """
    global _memo

    if _memo is not None and _memo[0] == raw:
        return _memo[1]

    identities = load_identities(raw)
    _memo = (raw, identities)
    return identities


def reset_identities_cache() -> None:
    """Forget the memo.

    Not needed for a changed value — `current_identities` notices that itself —
    and present for the case it cannot notice: a test that wants to prove the
    parse happens, and any caller that wants the next request to re-derive
    everything from scratch.
    """
    global _memo

    _memo = None


def identify_caller(header: str | None, raw_identities: str | None) -> str | None:
    """The identity behind an `Authorization` header, or `None` — never raises.

    Iteration 12 T10. `/health` is the one route `require_credentials` never
    reaches (`OPEN_PATHS`), and it now needs to tell an authenticated caller
    from an anonymous one anyway, to decide how much of its own body to show.
    This is that check, factored out rather than re-assembled at the call
    site: `require_credentials` already composes `current_identities`,
    `parse_basic` and `verify` in exactly this order, and a second hand-written
    copy is a second place their order could drift apart.

    **Deliberately collapses every failure to the same `None`.** The gate
    itself keeps `AuthConfigurationError` distinct from a bad credential, to
    decide whether to log a configuration problem — but both paths still end
    at the identical 401. A caller of `/health` never gets a 401 at all, so
    there is nothing here for the distinction to change, and collapsing it is
    what makes this function usable without the caller re-deriving the same
    three-way branch.
    """
    try:
        identities = current_identities(raw_identities)
    except AuthConfigurationError:
        return None

    presented = parse_basic(header)
    if presented is None:
        return None

    return verify(identities, *presented)
