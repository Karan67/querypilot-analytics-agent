"""Security response headers (Iteration 12 T5, AC7 AC8 AC11).

Hermetic. Everything runs against the app through `TestClient`, or reads
`api/web/` and `api/Dockerfile` as files.

Two properties are worth naming up front, because they are what the tests are
shaped around.

**The 401 carries the headers too.** That is AC7's real content. A headers
middleware registered *inside* the authentication gate never runs for a
request the gate refuses, and the refusal is exactly the response an
unauthenticated browser renders. `tests/test_security_headers.py::
test_the_refusal_carries_the_full_policy` is the assertion; the ordering that
satisfies it is in `api/main.py`, where this middleware is declared after
`require_credentials` so that Starlette wraps the gate in it.

**The policy answers to the page.** AC8. A CSP the application violates is
worse than none, because the first bug report loosens it permanently. So the
tests below derive the page's requirements from `api/web/` and assert the
policy covers them -- and assert the page has no inline script or style, which
is the fact `script-src 'self'` rests on.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from api.http import headers

WEB = pathlib.Path(__file__).resolve().parent.parent / "api" / "web"
DOCKERFILE = pathlib.Path(__file__).resolve().parent.parent / "api" / "Dockerfile"

HTML_PAGES = sorted(WEB.glob("*.html"))
SCRIPTS = sorted(WEB.glob("*.js"))


def directives() -> dict[str, str]:
    """The CSP parsed into `{name: value}`, rather than matched as a string."""
    parsed = {}
    for chunk in headers.CONTENT_SECURITY_POLICY.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, value = chunk.partition(" ")
        parsed[name] = value.strip()
    return parsed


# --- AC7: every response, including the ones that refuse ---------------------


def test_an_authenticated_response_carries_every_header(authed_client) -> None:
    """Asserted against `/`, not `/health`.

    `/health` reads the database to report readiness, which would put these
    tests in the `needs_db` lane and out of Iteration 11's hermetic 923. The
    headers are applied by middleware that never looks at the route, so any
    route proves the property and the cheapest one is the page.
    """
    response = authed_client.get("/")
    for name in headers.SECURITY_HEADERS:
        assert name in response.headers, f"{name} missing from a 200"


def test_the_refusal_carries_the_full_policy(anonymous_client) -> None:
    """AC7's real content, and the reason the middleware wraps the gate.

    A 401 is what an unauthenticated browser actually renders. Headers applied
    inside the authentication middleware would never reach it.
    """
    response = anonymous_client.get("/")
    assert response.status_code == 401
    for name in headers.SECURITY_HEADERS:
        assert name in response.headers, f"{name} missing from the 401"


def test_the_refusal_still_carries_its_challenge(anonymous_client) -> None:
    """Vacuity guard for the test above: the 401 must still be a real 401.

    A middleware that replaced the response rather than decorating it would
    satisfy every header assertion and break authentication.
    """
    response = anonymous_client.get("/")
    assert response.headers.get("WWW-Authenticate") == 'Basic realm="QueryPilot"'
    assert response.json() == {"detail": "Unauthorized"}


def test_an_unknown_route_is_still_indistinguishable(anonymous_client) -> None:
    """§2 measured that an anonymous caller cannot enumerate routes. AC12.

    The headers must not reintroduce a difference between a real route and an
    absent one.
    """
    real = anonymous_client.get("/")
    absent = anonymous_client.get("/nope")
    assert real.status_code == absent.status_code == 401
    for name in headers.SECURITY_HEADERS:
        assert real.headers.get(name) == absent.headers.get(name)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
    ],
)
def test_the_simple_headers_have_the_values_that_make_them_work(
    authed_client, name: str, expected: str
) -> None:
    """Presence is not the property. `X-Frame-Options: ALLOWALL` is present."""
    assert authed_client.get("/").headers[name] == expected


def test_hsts_is_not_emitted_yet() -> None:
    """T6's job, and deliberately not T5's.

    Emitting HSTS unconditionally would pin a developer's browser to HTTPS for
    a laptop that only serves HTTP, which is a way to make localhost
    unreachable with no obvious cause.
    """
    assert "Strict-Transport-Security" not in headers.SECURITY_HEADERS


# --- AC8: the policy is the one this page can actually run under -------------


def test_the_policy_denies_everything_it_does_not_name() -> None:
    assert directives()["default-src"] == "'none'"


@pytest.mark.parametrize("directive", ["script-src", "style-src", "connect-src"])
def test_the_policy_never_allows_inline_or_eval(directive: str) -> None:
    """The loosening that a CSP dies of."""
    value = directives()[directive]
    assert "unsafe-inline" not in value
    assert "unsafe-eval" not in value


def test_there_are_pages_and_scripts_to_check() -> None:
    """Vacuity guard: every assertion below iterates `api/web/`."""
    assert len(HTML_PAGES) >= 2, f"found {len(HTML_PAGES)} html pages"
    assert len(SCRIPTS) >= 2, f"found {len(SCRIPTS)} scripts"


@pytest.mark.parametrize("page", HTML_PAGES, ids=lambda p: p.name)
def test_no_page_carries_an_inline_script(page: pathlib.Path) -> None:
    """`script-src 'self'` blocks an inline `<script>` with no attribute.

    Matched against the tag rather than the word: a `<script src=...>` is
    fine and is what both pages use.
    """
    markup = page.read_text(encoding="utf-8")
    for match in re.finditer(r"<script\b([^>]*)>", markup, re.IGNORECASE):
        assert "src=" in match.group(1).lower(), (
            f"{page.name} has an inline <script>, which this CSP blocks"
        )


@pytest.mark.parametrize("page", HTML_PAGES, ids=lambda p: p.name)
def test_no_page_carries_an_inline_style_attribute(page: pathlib.Path) -> None:
    markup = page.read_text(encoding="utf-8")
    assert not re.search(r"\sstyle\s*=", markup, re.IGNORECASE), (
        f"{page.name} has an inline style attribute, which style-src 'self' blocks"
    )
    for match in re.finditer(r"<style\b", markup, re.IGNORECASE):
        raise AssertionError(f"{page.name} has an inline <style> block: {match}")


def test_the_stylesheet_pulls_nothing_from_anywhere() -> None:
    """`url()` and `@import` would need directives this policy does not grant."""
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    assert "@import" not in css
    assert "url(" not in css


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_every_fetch_target_is_same_origin(script: pathlib.Path) -> None:
    """`connect-src 'self'` is only correct while this stays true."""
    source = script.read_text(encoding="utf-8")
    for target in re.findall(r"fetch\(\s*[\"'`]([^\"'`]+)", source):
        assert target.startswith("/"), (
            f"{script.name} fetches {target!r}, which connect-src 'self' blocks"
        )


def test_the_page_assets_are_served_from_the_origin_the_policy_allows(
    authed_client,
) -> None:
    """End to end: the page loads, and so do the two things it asks for."""
    assert authed_client.get("/").status_code == 200
    assert authed_client.get("/static/styles.css").status_code == 200
    assert authed_client.get("/static/app.js").status_code == 200


# --- AC11: the server stops introducing itself -------------------------------


def test_the_server_banner_is_suppressed_at_the_server() -> None:
    """Read from the Dockerfile's instructions, not its prose.

    The comment above the CMD explains `--no-server-header` at length, so a
    text search would match the explanation in a file that had dropped the
    flag.
    """
    from tests.test_container_image import directives

    commands = directives("CMD")
    assert len(commands) == 1, (
        f"expected exactly one CMD instruction, found {commands}; the "
        f"HEALTHCHECK's own CMD lives on a continuation line and an earlier "
        f"version of this test matched that instead"
    )
    assert "--no-server-header" in commands[0]


def test_the_application_itself_never_adds_a_server_header(authed_client) -> None:
    """The flag covers uvicorn; this covers the app.

    `TestClient` does not run uvicorn, so a `server` header here would have to
    have come from the application, which is the half the flag cannot fix.
    """
    assert "server" not in {k.lower() for k in authed_client.get("/").headers}


# --- the module is a policy, not a mechanism ---------------------------------


def test_apply_leaves_a_deliberate_value_alone() -> None:
    existing = {"X-Frame-Options": "SAMEORIGIN"}
    headers.apply(existing)
    assert existing["X-Frame-Options"] == "SAMEORIGIN"
    assert existing["Referrer-Policy"] == "no-referrer"


def test_apply_can_be_told_to_overwrite() -> None:
    existing = {"X-Frame-Options": "SAMEORIGIN"}
    headers.apply(existing, overwrite=True)
    assert existing["X-Frame-Options"] == "DENY"
