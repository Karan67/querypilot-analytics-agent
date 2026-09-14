"""Iteration 10 T2 — the credential map and the comparison, with no HTTP.

`api/http/auth.py` is deliberately free of FastAPI and of the environment, so
every branch below runs without an app, without a database and without patching
`os.environ`. The gate itself is wired at T4; until then this module is
unreachable from the request path on purpose, and the suite stays green.

**The timing defence is asserted by counting, never by timing.** A test that
measures elapsed nanoseconds is a flake on a shared machine, and this project
has enough of those. What can be asserted deterministically is that the unknown
name and the known-but-wrong secret perform the *same number of comparisons* —
which is the property the dummy exists to create.
"""

from __future__ import annotations

import ast
import base64
import pathlib

import pytest

from api.http.auth import (
    OPEN_PATHS,
    UNAUTHORIZED_BODY,
    UNAUTHORIZED_HEADERS,
    USERS_ENV,
    AuthConfigurationError,
    is_open,
    load_identities,
    parse_basic,
    verify,
)

SOURCE = pathlib.Path("api/http/auth.py").read_text(encoding="utf-8")

GOOD = '{"analyst": "s3cret-one", "ops": "s3cret-two"}'


# --- load_identities: every way a configuration can be unusable --------------


def test_a_valid_map_round_trips():
    assert load_identities(GOOD) == {"analyst": "s3cret-one", "ops": "s3cret-two"}


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(None, id="unset"),
        pytest.param("", id="empty"),
        pytest.param("   \n\t ", id="whitespace"),
    ],
)
def test_a_missing_map_is_refused_loudly(raw):
    """**The fail-closed invariant** (resolved D-2).

    Returning `{}` here would be the dangerous answer: a map that authorises
    nobody is indistinguishable from a working one that happens to reject this
    caller, and a deployment with a typo in the variable name would look exactly
    like a deployment with strict credentials. That is `011-ship.md` §2.1's
    green-and-empty shape — the mechanism present, switched off, and silent.
    """
    with pytest.raises(AuthConfigurationError) as caught:
        load_identities(raw)
    assert USERS_ENV in str(caught.value), "the message must name the variable to set"


@pytest.mark.parametrize(
    "raw,id_",
    [
        ('{"admin": "x" oops}', "malformed-json"),
        ("[1, 2, 3]", "json-array"),
        ('"just-a-string"', "json-string"),
        ("42", "json-number"),
        ("{}", "empty-object"),
        ('{"admin": 1234}', "non-string-secret"),
        ('{"admin": null}', "null-secret"),
        ('{"admin": ""}', "empty-secret"),
        ('{"": "x"}', "empty-name"),
    ],
)
def test_an_unusable_map_is_refused(raw, id_):
    with pytest.raises(AuthConfigurationError):
        load_identities(raw)


def test_an_empty_secret_is_refused_because_it_would_admit_everyone():
    """Not pedantry — measured.

    `secrets.compare_digest(b"", b"")` is **True**, so an entry with an empty
    secret authorises any caller who sends an empty password. That is a wide
    open door that reads, in a config file, as a filled-in field.
    """
    import secrets as _s

    assert _s.compare_digest(b"", b"") is True, "the hazard this test guards"

    with pytest.raises(AuthConfigurationError) as caught:
        load_identities('{"admin": ""}')
    assert "empty secret" in str(caught.value)


# --- the thing that must never be in a message -------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param('{"admin": "hunter2-do-not-leak" oops}', id="malformed"),
        pytest.param('{"admin": ""}', id="empty-secret"),
        pytest.param('{"admin": 1234}', id="non-string"),
        pytest.param('["hunter2-do-not-leak"]', id="array"),
    ],
)
def test_no_error_message_ever_quotes_a_secret(raw):
    """AC5, and the leak this guards is a real one.

    `json.JSONDecodeError` carries the entire raw document on `.doc`. Formatting
    the exception's attributes — or the input — into a message would write the
    credential map into the container log, where it would sit until the log
    rotated. Only `str(exc)` is ever quoted, and `str(exc)` is measured to
    contain no document text.
    """
    with pytest.raises(AuthConfigurationError) as caught:
        load_identities(raw)

    message = str(caught.value)
    assert "hunter2-do-not-leak" not in message
    assert "1234" not in message


def test_the_module_never_formats_the_raw_value_or_the_documents_text():
    """The structural half, because the test above can only cover the strings it
    happens to try.

    Asserted against the parsed AST rather than by grepping the source, which
    this repository has twice done and twice regretted — a substring search here
    would match this docstring.
    """
    tree = ast.parse(SOURCE)
    banned = {"doc"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in banned:
            raise AssertionError(
                f"api/http/auth.py reads .{node.attr}, which on a "
                f"JSONDecodeError is the whole raw credential map"
            )


# --- verify: the comparison ---------------------------------------------------


def test_a_correct_secret_returns_the_name():
    identities = load_identities(GOOD)
    assert verify(identities, "analyst", "s3cret-one") == "analyst"
    assert verify(identities, "ops", "s3cret-two") == "ops"


@pytest.mark.parametrize(
    "username,password,id_",
    [
        ("analyst", "wrong", "known-name-wrong-secret"),
        ("nobody", "s3cret-one", "unknown-name-valid-secret-of-another"),
        ("nobody", "wrong", "unknown-both"),
        ("", "", "empty-both"),
        ("analyst", "", "known-name-empty-secret"),
        ("ANALYST", "s3cret-one", "name-is-case-sensitive"),
        ("analyst ", "s3cret-one", "name-is-not-trimmed"),
        ("analyst", "s3cret-one ", "secret-is-not-trimmed"),
    ],
)
def test_anything_short_of_an_exact_match_fails(username, password, id_):
    assert verify(load_identities(GOOD), username, password) is None


def test_a_non_ascii_password_is_a_refusal_not_a_crash():
    """`secrets.compare_digest` raises `TypeError` on `str` arguments holding
    non-ASCII, so comparing without encoding first would turn a wrong password
    with an accent in it into a 500 instead of a 401. Measured, not assumed."""
    identities = load_identities('{"analyst": "caf\\u00e9-secret"}')

    assert verify(identities, "analyst", "café-secret") == "analyst"
    assert verify(identities, "analyst", "café-wrong") is None


# --- the timing defence, counted rather than timed ---------------------------


def test_an_unknown_name_costs_the_same_comparison_as_a_known_one(monkeypatch):
    """**The defence, and the reason it exists.**

    Without the dummy, an unknown name returns before comparing anything while a
    known name pays a `compare_digest` — so an attacker distinguishes "no such
    user" from "wrong password" by measurement, and enumerates the configured
    identities one guess at a time.

    Counted, not timed: a wall-clock assertion on a shared machine is a flake,
    and B-13 cost this project six weeks of exactly that. What is deterministic
    is the *number of comparisons*, and equalising it is what the dummy is for.
    """
    import api.http.auth as auth

    calls: list[tuple[bytes, bytes]] = []
    real = auth.secrets.compare_digest

    def counting(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.secrets, "compare_digest", counting)
    identities = load_identities(GOOD)

    calls.clear()
    verify(identities, "analyst", "wrong")
    known = len(calls)

    calls.clear()
    verify(identities, "no-such-name", "wrong")
    unknown = len(calls)

    assert known == unknown == 1, (
        f"a known name with a wrong secret performs {known} comparison(s) and an "
        f"unknown name performs {unknown}; an attacker can tell them apart by "
        f"timing and enumerate the identity list"
    )


def test_the_comparison_is_constant_time_and_not_an_equality_check():
    """AC4, asserted against the parsed AST.

    Two things have to hold and only one of them is obvious: `compare_digest` is
    called, **and** no `==` compares the secret. A module that did both would
    short-circuit on the first differing byte in the `==` and the
    `compare_digest` would be decoration.
    """
    tree = ast.parse(SOURCE)

    verify_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "verify"
    )

    called = {
        node.func.attr
        for node in ast.walk(verify_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "compare_digest" in called, "verify() must compare in constant time"

    for node in ast.walk(verify_fn):
        if isinstance(node, ast.Compare):
            for op in node.ops:
                assert isinstance(op, ast.Is | ast.IsNot), (
                    f"verify() uses {type(op).__name__} on a value; only "
                    f"identity checks against None are allowed, because an "
                    f"equality comparison on a secret short-circuits"
                )


# --- the constants the wiring at T4 will depend on ---------------------------


def test_the_refusal_carries_a_realm_so_a_browser_prompts():
    """Resolved Q-C's whole argument for Basic — no login page, no cookie, no
    JavaScript change — depends on the browser putting up its own dialog, and it
    only does that for a 401 carrying this header."""
    assert UNAUTHORIZED_HEADERS["WWW-Authenticate"].startswith("Basic ")
    assert 'realm="QueryPilot"' in UNAUTHORIZED_HEADERS["WWW-Authenticate"]


def test_the_refusal_body_says_nothing_about_which_half_was_wrong():
    """AC6. One body for four failure modes, so the response cannot be read as
    a hint about whether the name exists."""
    rendered = str(UNAUTHORIZED_BODY).lower()
    for leak in ("user", "name", "password", "secret", "unknown", "exist"):
        assert leak not in rendered, f"the refusal body hints at {leak!r}"


# --- T2's unwired-on-purpose test lived here, and T4 deleted it --------------
#
# `test_the_module_is_not_wired_in_yet` asserted that `api/main.py` did not
# import this module, so that the 100 call sites counted in `013-auth.md` §2.6
# kept passing until T3 had taught them to authenticate. Its own docstring said
# it would be **deleted rather than inverted** when T4 wired the gate, because
# it recorded a property of the task sequence and not of the design. Inverting
# it would have left a test asserting that an import exists, which is what
# `tests/test_auth.py` proves properly by making requests.


# --- the exemption, and the path it is decided on ----------------------------


def test_health_is_the_only_open_path():
    """AC3. One exemption, and the list is short enough to assert literally."""
    assert set(OPEN_PATHS) == {"/health"}


def test_every_exemption_carries_its_reason():
    """AC3 says what stays open is *named with a reason*.

    Asserted rather than trusted, because the reason is the half that stops a
    second exemption being added next year on the grounds that a first one
    exists. A blank string would satisfy the dict and satisfy nobody.
    """
    for path, reason in OPEN_PATHS.items():
        assert len(reason) > 40, f"{path} is exempt with no stated reason"


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/health", True),
        ("/health/", True),
        # Refused, and that is the right answer rather than an oversight: a
        # doubled leading slash matches no route, so it was going to be a 404
        # either way, and normalising it would widen the one exemption this
        # project has to cover a shape nothing sends. Fail closed.
        ("//health//", False),
        ("/", False),
        ("/ask", False),
        ("/healthz", False),
        ("/health-internal", False),
        ("/health/deep", False),
        ("/static/app.js", False),
    ],
)
def test_only_the_exempt_path_is_open(path, expected):
    """**The trailing slash is the interesting case**, and `013-auth-plan.md`
    §2.4 named it before the code existed: an exemption written as
    `path == "/health"` is *"a string comparison that will eventually be wrong
    about `/health/`"*.

    The middleware runs above routing, so FastAPI's own slash redirect never
    gets a chance to normalise it. `/healthz` and `/health-internal` are the
    other half: a prefix match would have made both open by accident.
    """
    assert is_open(path) is expected


# --- the Authorization header ------------------------------------------------


def test_a_well_formed_header_parses():
    header = "Basic " + base64.b64encode(b"analyst:s3cret-one").decode()
    assert parse_basic(header) == ("analyst", "s3cret-one")


def test_a_password_may_contain_a_colon():
    """RFC 7617 splits on the *first* colon only. A username may not contain
    one; a password may, and truncating it there would silently reject a
    perfectly good generated secret."""
    header = "Basic " + base64.b64encode(b"analyst:a:b:c").decode()
    assert parse_basic(header) == ("analyst", "a:b:c")


@pytest.mark.parametrize(
    "header,id_",
    [
        (None, "absent"),
        ("", "empty"),
        ("Basic", "scheme-with-nothing-after-it"),
        ("Basic ", "scheme-and-a-space"),
        ("Bearer abc123", "wrong-scheme"),
        ("Basic !!!not-base64!!!", "not-base64"),
        ("Basic " + base64.b64encode(b"no-colon-here").decode(), "no-colon"),
        ("Basic " + base64.b64encode(b"\xff\xfe").decode(), "not-utf8"),
        ("Basic YWJj YWJj", "two-values"),
    ],
)
def test_every_malformed_header_is_none_and_never_raises(header, id_):
    """**None, not an exception**, and that is the whole point of the function.

    The caller turns `None` into the same 401 as a wrong password. A header
    shape that raised would be a 500, and a 500 tells an attacker that
    *something about this input was special* — which is exactly the signal AC6
    exists to deny them.
    """
    assert parse_basic(header) is None


def test_the_scheme_is_matched_case_insensitively():
    """RFC 7235 says the scheme is case-insensitive, and some clients send
    `basic`. Refusing those would be a 401 that no amount of correct
    credentials could fix."""
    encoded = base64.b64encode(b"analyst:s3cret-one").decode()
    for scheme in ("Basic", "basic", "BASIC", "BaSiC"):
        assert parse_basic(f"{scheme} {encoded}") == ("analyst", "s3cret-one")


# --- the memo, and why it is keyed on the value ------------------------------


def test_the_parse_is_memoised_on_the_raw_value(monkeypatch):
    """Called per protected request, so it must not re-parse per request."""
    import api.http.auth as auth

    auth.reset_identities_cache()
    calls = []
    real = auth.json.loads

    def counting(raw):
        calls.append(raw)
        return real(raw)

    monkeypatch.setattr(auth.json, "loads", counting)

    first = auth.current_identities(GOOD)
    second = auth.current_identities(GOOD)

    assert first == second
    assert len(calls) == 1, f"parsed {len(calls)} times for one configuration"


def test_a_rotated_configuration_is_noticed_without_a_restart():
    """**The property the memo is keyed on a value to get.**

    Memoising on a boolean "already loaded" flag — the obvious implementation —
    means a rotated secret keeps working until somebody restarts the process,
    and a secret that needs a restart to rotate is a secret that does not get
    rotated. Keyed on the raw string, the new value is simply a different key.
    """
    from api.http.auth import current_identities, reset_identities_cache

    reset_identities_cache()
    assert current_identities('{"analyst": "old"}') == {"analyst": "old"}
    assert current_identities('{"analyst": "new"}') == {"analyst": "new"}


def test_a_broken_configuration_is_not_memoised():
    """A deployment fixed by correcting the environment recovers on the next
    request. Caching the failure would mean the fix needed a restart, which is
    the same trap as the one above wearing the opposite costume."""
    from api.http.auth import current_identities, reset_identities_cache

    reset_identities_cache()
    for _ in range(3):
        with pytest.raises(AuthConfigurationError):
            current_identities("not json")

    assert current_identities(GOOD) == {"analyst": "s3cret-one", "ops": "s3cret-two"}


def test_reset_forces_a_reparse(monkeypatch):
    import api.http.auth as auth

    auth.reset_identities_cache()
    calls = []
    real = auth.json.loads
    monkeypatch.setattr(auth.json, "loads", lambda raw: (calls.append(raw), real(raw))[1])

    auth.current_identities(GOOD)
    auth.reset_identities_cache()
    auth.current_identities(GOOD)

    assert len(calls) == 2
