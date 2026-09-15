"""Forwarded headers and conditional HSTS (Iteration 12 T6, AC9 AC10 AC12).

Hermetic. No proxy is started; the header is what a proxy sends, and a header
is exactly what a test can send too -- which is the whole point of the module
under test, and the reason these tests are worth having. A client can send
`X-Forwarded-Proto: https` just as easily as Caddy can.

The asymmetry to keep in mind while reading: **both misconfigurations are
silent.** Believe the header from anyone and any client can claim HTTPS, which
makes HSTS a lie the client tells on its own behalf. Believe it from nobody and
a correctly deployed service behind a terminator never emits HSTS at all.
Neither failure raises anything, so both directions are asserted here.
"""

from __future__ import annotations

import pytest

from api.http import forwarded, headers

TRUSTED = "10.9.0.0/16"
INSIDE = "10.9.0.7"
OUTSIDE = "203.0.113.5"


# --- who is believed ---------------------------------------------------------


def test_the_default_covers_dockers_bridge_range() -> None:
    """`docker compose --profile tls up` has to work with no configuration."""
    assert forwarded.is_trusted("172.18.0.4", None)
    assert forwarded.is_trusted("127.0.0.1", None)


def test_the_default_does_not_cover_the_office_lan() -> None:
    """`10.0.0.0/8` and `192.168.0.0/16` are private *and* populated.

    On a corporate LAN or a home network they are where the other machines
    live, so trusting them would let any host on the wifi claim HTTPS. Docker
    does not allocate bridge networks from them by default.
    """
    assert not forwarded.is_trusted("10.1.2.3", None)
    assert not forwarded.is_trusted("192.168.1.50", None)


def test_a_public_address_is_never_trusted_by_default() -> None:
    assert not forwarded.is_trusted("203.0.113.5", None)


def test_an_absent_or_unparseable_peer_is_not_trusted() -> None:
    """A unix-socket connection has no client address at all."""
    assert not forwarded.is_trusted(None, None)
    assert not forwarded.is_trusted("", None)
    assert not forwarded.is_trusted("not-an-address", None)


def test_the_trusted_set_is_configuration() -> None:
    assert forwarded.is_trusted(INSIDE, TRUSTED)
    assert not forwarded.is_trusted(OUTSIDE, TRUSTED)


def test_a_blank_setting_means_the_default_not_trust_nobody() -> None:
    assert forwarded.is_trusted("172.18.0.4", "   ")


def test_a_malformed_network_is_refused_by_name() -> None:
    with pytest.raises(forwarded.TrustConfigurationError) as excinfo:
        forwarded.trusted_networks("172.16.0.0/12,banana")
    assert "banana" in str(excinfo.value)


# --- AC10: the scheme is only rewritten on a trusted peer's word -------------


def test_a_trusted_proxy_can_say_the_client_used_https() -> None:
    assert forwarded.resolve_scheme("http", INSIDE, "https", TRUSTED) == "https"


def test_a_stranger_cannot_claim_https() -> None:
    """The header is a string anybody may send. This is AC10's whole content."""
    assert forwarded.resolve_scheme("http", OUTSIDE, "https", TRUSTED) == "http"


def test_a_trusted_proxy_can_also_say_the_client_used_plain_http() -> None:
    """Trust is not a one-way ratchet toward `https`.

    A terminator that also serves plain HTTP reports which one this request
    used, and a proxy saying `http` over a connection we happened to accept as
    HTTPS is telling us the client was not secure.
    """
    assert forwarded.resolve_scheme("https", INSIDE, "http", TRUSTED) == "http"
    assert forwarded.resolve_scheme("http", INSIDE, "http", TRUSTED) == "http"


def test_a_proxy_chain_takes_the_original_scheme() -> None:
    assert forwarded.resolve_scheme("http", INSIDE, "https, http", TRUSTED) == "https"


def test_an_unrecognised_scheme_changes_nothing() -> None:
    """A scheme we do not understand is not a reason to change our mind."""
    assert forwarded.resolve_scheme("http", INSIDE, "gopher", TRUSTED) == "http"
    assert forwarded.resolve_scheme("http", INSIDE, "", TRUSTED) == "http"


def test_no_header_changes_nothing() -> None:
    assert forwarded.resolve_scheme("http", INSIDE, None, TRUSTED) == "http"


# --- AC10: the client address, on the same terms ----------------------------


def test_a_trusted_proxy_can_name_the_original_client() -> None:
    assert forwarded.resolve_client(INSIDE, "198.51.100.9", TRUSTED) == "198.51.100.9"


def test_a_stranger_cannot_rewrite_its_own_address() -> None:
    assert forwarded.resolve_client(OUTSIDE, "198.51.100.9", TRUSTED) == OUTSIDE


def test_the_left_most_entry_wins() -> None:
    value = "198.51.100.9, 172.18.0.4"
    assert forwarded.resolve_client(INSIDE, value, TRUSTED) == "198.51.100.9"


def test_a_junk_forwarded_for_falls_back_to_the_peer() -> None:
    assert forwarded.resolve_client(INSIDE, "not-an-address", TRUSTED) == INSIDE


# --- AC9: HSTS follows the resolved scheme ----------------------------------


def test_hsts_is_absent_on_plain_http() -> None:
    """A laptop serving http://localhost must never pin itself.

    HSTS is cached by host and not by port, so one accidental emission makes
    every other project that ever serves `http://localhost` unreachable in that
    browser, with no error naming the cause.
    """
    emitted: dict[str, str] = {}
    headers.apply(emitted, secure=False)
    assert headers.HSTS_HEADER not in emitted


def test_hsts_is_present_once_the_request_is_known_to_be_https() -> None:
    emitted: dict[str, str] = {}
    headers.apply(emitted, secure=True)
    assert emitted[headers.HSTS_HEADER] == headers.STRICT_TRANSPORT_SECURITY


def test_the_policy_is_a_year_with_subdomains_and_no_preload() -> None:
    """`preload` is a one-way door: removal takes months and a request to a
    browser vendor, and it is not a decision a deployment without a chosen
    domain is entitled to make. That is Q-M, deferred."""
    policy = headers.STRICT_TRANSPORT_SECURITY
    assert "max-age=31536000" in policy
    assert "includeSubDomains" in policy
    assert "preload" not in policy


# --- end to end, through the app --------------------------------------------


def test_a_plain_request_gets_no_hsts(authed_client) -> None:
    assert headers.HSTS_HEADER not in authed_client.get("/").headers


def test_an_untrusted_client_claiming_https_gets_no_hsts(authed_client) -> None:
    """`TestClient` connects from `testclient`, which is not an IP address at
    all and is therefore never inside a trusted network."""
    response = authed_client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert headers.HSTS_HEADER not in response.headers


PROXY_PEER = ("192.0.2.10", 5000)


def _client_behind_a_proxy(monkeypatch, *, credentials):
    """A `TestClient` whose connection appears to come from a real address.

    The peer address is fixed on the constructor, not per request -- the
    default `testclient` is not an IP at all, which is why every request from
    the shared fixtures is correctly distrusted.
    """
    import json

    from fastapi.testclient import TestClient

    from api.http.auth import USERS_ENV
    from api.main import app
    from tests.conftest import TEST_SECRET, TEST_USER

    monkeypatch.setenv(USERS_ENV, json.dumps({TEST_USER: TEST_SECRET}))
    monkeypatch.setenv(forwarded.TRUSTED_PROXIES_ENV, "192.0.2.0/24")
    client = TestClient(app, client=PROXY_PEER)
    if credentials:
        client.auth = (TEST_USER, TEST_SECRET)
    return client


def test_a_trusted_proxy_claiming_https_gets_hsts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive half. Without it, a middleware that never believes anyone
    would pass every negative test above."""
    client = _client_behind_a_proxy(monkeypatch, credentials=True)
    response = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert response.status_code == 200
    assert response.headers[headers.HSTS_HEADER] == headers.STRICT_TRANSPORT_SECURITY


def test_the_same_proxy_without_the_header_gets_no_hsts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trust is necessary and not sufficient: a trusted proxy that forwarded a
    plain HTTP request must not produce HSTS either."""
    client = _client_behind_a_proxy(monkeypatch, credentials=True)
    assert headers.HSTS_HEADER not in client.get("/").headers


def test_the_refusal_also_carries_hsts_when_secure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7 and AC9 together: the 401 is a response like any other."""
    client = _client_behind_a_proxy(monkeypatch, credentials=False)
    response = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert response.status_code == 401
    assert headers.HSTS_HEADER in response.headers


def test_an_unknown_route_is_still_indistinguishable(anonymous_client) -> None:
    """AC12 survives T6: the proxy middleware runs before routing, so it must
    not introduce a difference between a real path and an absent one."""
    real = anonymous_client.get("/", headers={"X-Forwarded-Proto": "https"})
    absent = anonymous_client.get("/nope", headers={"X-Forwarded-Proto": "https"})
    assert real.status_code == absent.status_code == 401
    assert real.headers.get(headers.HSTS_HEADER) == absent.headers.get(
        headers.HSTS_HEADER
    )


def test_a_broken_trust_configuration_does_not_take_the_service_down(
    authed_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conservative answer: serve the request on the connection's own
    evidence, which means no HSTS, rather than returning a 500."""
    monkeypatch.setenv(forwarded.TRUSTED_PROXIES_ENV, "not-a-network")
    response = authed_client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert response.status_code == 200
    assert headers.HSTS_HEADER not in response.headers


# --- the trust boundary is observable ---------------------------------------


def test_the_status_reports_networks_and_never_addresses() -> None:
    report = forwarded.status(TRUSTED)
    assert report["configured"] is True
    assert report["networks"] == [TRUSTED]
    assert report["error"] == ""


def test_the_status_names_a_broken_configuration() -> None:
    report = forwarded.status("banana")
    assert report["configured"] is False
    assert "banana" in report["error"]
