"""TLS termination through Caddy (Iteration 12 T7, AC13 AC14).

**Not hermetic, and not `needs_db` either.** These drive a real HTTPS request
through a real reverse proxy, so they need `docker compose --profile tls up -d`
and nothing else will do -- a mocked TLS handshake would assert that the mock
works. That is why AC13 is worded as *exercised, not asserted*: the previous
state of this project was a README paragraph saying TLS was a deployment
concern, which was true and tested nothing.

They carry their own marker, `tls`, registered in `pytest.ini`. Like the
database lane they **skip** when the endpoint is unreachable, because a
developer who has not started the profile should not see failures for a thing
they did not ask for -- and like the database lane that skip is dangerous in a
pipeline, so `QUERYPILOT_TESTS_REQUIRE_TLS=1` turns it back into a failure. CI
does not run this profile today; the variable is what a pipeline that wants to
would set.

Certificates are not verified, and that is deliberate rather than lazy. The
profile issues from Caddy's *internal* CA precisely so it works offline, which
means no verification path exists without extracting the root out of the
container and teaching the client about it. What is being proved here is that a
TLS handshake completes, that the request reaches the application through it,
and that the application can tell -- and the last of those is asserted by the
presence of HSTS, which only appears when `api/http/forwarded.py` believed a
trusted proxy's `X-Forwarded-Proto`. That chain is the end-to-end property; a
verified certificate would add nothing to it.
"""

from __future__ import annotations

import os
import ssl
import urllib.error
import urllib.request

import pytest

from api.http import headers

pytestmark = pytest.mark.tls

HTTPS_PORT = os.environ.get("TLS_HTTPS_PORT", "8443")
HTTP_PORT = os.environ.get("TLS_HTTP_PORT", "8080")
HTTPS_BASE = f"https://localhost:{HTTPS_PORT}"
HTTP_BASE = f"http://localhost:{HTTP_PORT}"

REQUIRE_ENV = "QUERYPILOT_TESTS_REQUIRE_TLS"


def _unverified() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _get(url: str, *, redirect: bool = True):
    """Fetch, returning the response or the redirect, never following blindly."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    handlers = [urllib.request.HTTPSHandler(context=_unverified())]
    if not redirect:
        handlers.append(_NoRedirect())
    opener = urllib.request.build_opener(*handlers)
    try:
        return opener.open(url, timeout=10)
    except urllib.error.HTTPError as exc:
        return exc


@pytest.fixture(scope="module", autouse=True)
def terminator_is_up():
    """Skip the lane when the profile is not running, unless told not to.

    The same shape as `configured_database`: right for a developer, lethal for
    a pipeline that would otherwise exit 0 having verified nothing.
    """
    try:
        _get(f"{HTTPS_BASE}/health")
    except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
        message = (
            f"the TLS profile is not reachable at {HTTPS_BASE} ({exc}). "
            f"Start it with `docker compose --profile tls up -d`."
        )
        if os.environ.get(REQUIRE_ENV) == "1":
            pytest.fail(message)
        pytest.skip(message)


# --- AC13: a real HTTPS request, all the way through ------------------------


def test_the_handshake_completes_and_the_application_answers() -> None:
    response = _get(f"{HTTPS_BASE}/health")
    assert response.status == 200


def test_the_connection_is_actually_tls() -> None:
    """Vacuity guard for everything above: prove the socket was encrypted.

    Without this, a Caddyfile that served plain HTTP on 8443 would satisfy
    every other assertion in this file.
    """
    import socket

    with socket.create_connection(("localhost", int(HTTPS_PORT)), timeout=10) as raw:
        with _unverified().wrap_socket(raw, server_hostname="localhost") as tls:
            assert tls.version().startswith("TLS")
            assert tls.getpeercert(binary_form=True)


def test_the_application_knows_the_request_arrived_over_tls() -> None:
    """The end-to-end property, and the reason this file is worth its cost.

    HSTS appears only when `api/http/forwarded.py` believed Caddy's
    `X-Forwarded-Proto: https` -- which happens only because Caddy's address
    falls inside the compose network that the default trusted set covers. One
    header proves the proxy set it, the trust boundary accepted it, and the
    header middleware acted on it.
    """
    response = _get(f"{HTTPS_BASE}/health")
    assert response.headers.get(headers.HSTS_HEADER) == headers.STRICT_TRANSPORT_SECURITY


def test_the_security_headers_survive_the_proxy() -> None:
    """A reverse proxy that rewrote or dropped them would be silent about it."""
    response = _get(f"{HTTPS_BASE}/health")
    for name in headers.SECURITY_HEADERS:
        assert name in response.headers, f"{name} did not survive the proxy"


def test_the_gate_still_refuses_an_anonymous_caller_over_tls() -> None:
    """TLS is transport, not authorisation. Iteration 10's perimeter stands."""
    response = _get(f"{HTTPS_BASE}/")
    assert response.status == 401


def test_the_proxy_does_not_introduce_its_own_server_banner() -> None:
    """AC11 through the whole chain, not just at uvicorn."""
    response = _get(f"{HTTPS_BASE}/health")
    assert "server" not in {name.lower() for name in response.headers}


# --- AC14: plain HTTP is redirected, not served -----------------------------


def test_plain_http_is_redirected_rather_than_served() -> None:
    response = _get(f"{HTTP_BASE}/health", redirect=False)
    assert response.status in {301, 302, 307, 308}


def test_the_redirect_preserves_the_host_and_the_path() -> None:
    """AC14. The port is not asserted: Caddy names 443 because that is what it
    listens on inside its namespace, and the profile publishes it elsewhere on
    the host precisely so it does not need a privileged port."""
    from urllib.parse import urlparse

    response = _get(f"{HTTP_BASE}/health", redirect=False)
    location = urlparse(response.headers["Location"])
    assert location.scheme == "https"
    assert location.hostname == "localhost"
    assert location.path == "/health"


def test_the_redirect_does_not_serve_the_payload() -> None:
    """A redirect that also returned the body would have served the request
    over plain HTTP while looking like it had not."""
    response = _get(f"{HTTP_BASE}/health", redirect=False)
    assert not response.read().strip()
