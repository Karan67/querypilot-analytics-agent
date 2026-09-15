"""Whose word to take about how a request arrived (Iteration 12 T6, AC10).

Behind a TLS terminator the application never sees TLS. The connection it
accepts is plain HTTP from the proxy, and the only evidence that the *client*
used HTTPS is `X-Forwarded-Proto: https` -- a header, which is to say a string
the client can also send.

That is the whole problem this module exists for. AC9 emits
`Strict-Transport-Security` only for a request that arrived over HTTPS, and a
header anybody may set is not evidence of anything. Trust it unconditionally
and any client can claim HTTPS, which makes AC9 decorative. Trust it never and
a correctly deployed service behind Caddy believes every request is insecure,
never emits HSTS, and would redirect itself into a loop if T7's redirect lived
here rather than in the proxy.

So the header is believed **only when the peer that sent it is a proxy we
configured**. `QUERYPILOT_TRUSTED_PROXIES` is a comma-separated list of CIDR
networks; the default covers loopback and the RFC-1918 range Docker allocates
bridge networks from, which is what makes `docker compose --profile tls up`
work with no configuration while a container exposed directly to the internet
still refuses to believe a stranger.

**The failure modes are asymmetric and neither announces itself.** Too
permissive and HSTS becomes a lie a client can tell on its own behalf. Too
strict and HTTPS requests look like HTTP forever. `/health` reports the
resolved trust configuration for exactly this reason: it is not otherwise
observable from outside.

This module decides; it does not act. `api/main.py` holds the registration, on
the terms `api/agent/tools.py` states about itself.
"""

from __future__ import annotations

import ipaddress
import os
from functools import lru_cache

#: Comma-separated CIDRs whose `X-Forwarded-*` headers are believed.
TRUSTED_PROXIES_ENV = "QUERYPILOT_TRUSTED_PROXIES"

#: Loopback, plus the range Docker allocates bridge networks from.
#:
#: `172.16.0.0/12` is deliberately the whole RFC-1918 block Docker draws from
#: rather than a single network, because the compose network's subnet is
#: assigned at `up` time and naming it here would break the moment a developer
#: already had three other stacks running. It is private address space: a
#: request reaching the container from it has crossed no untrusted network.
#:
#: It does **not** include `10.0.0.0/8` or `192.168.0.0/16`. Those are also
#: private, and on a corporate LAN or a home network they are where the other
#: machines live -- trusting them would let any host on the office wifi claim
#: HTTPS. Docker does not allocate from them by default.
DEFAULT_TRUSTED_PROXIES = "127.0.0.0/8,::1/128,172.16.0.0/12"

FORWARDED_PROTO_HEADER = "x-forwarded-proto"
FORWARDED_FOR_HEADER = "x-forwarded-for"

_VALID_SCHEMES = frozenset({"http", "https"})


class TrustConfigurationError(ValueError):
    """`QUERYPILOT_TRUSTED_PROXIES` names something that is not a network."""


@lru_cache(maxsize=8)
def _parse(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            networks.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError as exc:
            raise TrustConfigurationError(
                f"{TRUSTED_PROXIES_ENV} entry {chunk!r} is not a CIDR network: {exc}"
            ) from exc
    return tuple(networks)


def trusted_networks(raw: str | None = None):
    """The configured networks, memoised by their raw string.

    Memoised the way `api/http/auth.py` memoises the credential map: keyed on
    the environment's value, so changing the variable takes effect without a
    restart and reading it costs nothing per request.

    An empty or unset value means the default. An explicitly empty list --
    `QUERYPILOT_TRUSTED_PROXIES=" "` -- also means the default rather than
    "trust nobody", because a deployment that meant to disable forwarding would
    have no proxy in front of it and would never see the headers anyway.
    """
    if raw is None:
        raw = os.environ.get(TRUSTED_PROXIES_ENV, "")
    return _parse(raw.strip() or DEFAULT_TRUSTED_PROXIES)


def is_trusted(client_host: str | None, raw: str | None = None) -> bool:
    """Whether the immediate peer is a proxy whose headers we believe."""
    if not client_host:
        return False
    try:
        address = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    return any(address in network for network in trusted_networks(raw))


def resolve_scheme(
    scheme: str, client_host: str | None, forwarded_proto: str | None, raw: str | None = None
) -> str:
    """The scheme the *client* used, which is not always the one we accepted.

    Returns the connection's own scheme unless a trusted proxy said otherwise.
    A malformed or unknown value is ignored rather than propagated: `https, http`
    from a chain of proxies takes the first entry, and anything that is not
    `http` or `https` is discarded, because a scheme we do not recognise is not
    a reason to change our mind about TLS.
    """
    if not forwarded_proto or not is_trusted(client_host, raw):
        return scheme
    candidate = forwarded_proto.split(",")[0].strip().lower()
    return candidate if candidate in _VALID_SCHEMES else scheme


def resolve_client(
    client_host: str | None, forwarded_for: str | None, raw: str | None = None
) -> str | None:
    """The address the client connected from, if a trusted proxy told us.

    Takes the **left-most** entry, which is the original client. Everything to
    its right is a proxy chain, and everything in the header is client-supplied
    once it has passed through a proxy that appends rather than replaces -- so
    this value is good enough to log and to rate-limit on, and is never good
    enough to authorise with.
    """
    if not forwarded_for or not is_trusted(client_host, raw):
        return client_host
    candidate = forwarded_for.split(",")[0].strip()
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return client_host
    return candidate


def status(raw: str | None = None) -> dict:
    """What `/health` reports about the trust boundary.

    Networks, never headers and never addresses. The point is that a deployment
    can be asked whether it is configured to believe its proxy, because getting
    this wrong is silent in both directions.
    """
    try:
        networks = trusted_networks(raw)
    except TrustConfigurationError as exc:
        return {"configured": False, "networks": [], "error": str(exc)}
    return {
        "configured": True,
        "networks": [str(network) for network in networks],
        "error": "",
    }
