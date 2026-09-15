"""Security response headers (Iteration 12 T5, AC7 AC8 AC11).

Before this module the complete set of headers on a QueryPilot response was
`date`, `server`, `content-length` and `content-type`. That was measured, not
assumed, and it is what §2 of `specs/015-production-deployment.md` records. No
`X-Content-Type-Options`, no `X-Frame-Options`, no `Content-Security-Policy`,
no `Referrer-Policy`.

On a laptop that costs nothing. On the public URL B-11 contemplates it is the
difference between a browser that refuses to run injected script and one that
happily does.

**The policy answers to the page, not the other way round.** The rule this
module is built on is that a CSP the application violates is worse than no CSP,
because the first bug report loosens it and the loosened version is what ships
forever. So the policy below was derived from what `api/web/` actually does,
measured: two HTML pages that load exactly one stylesheet and one script each,
both from `/static/`, with no inline `<script>`, no inline `style=` attribute,
no `@import`, no `url()` in the stylesheet, and four `fetch()` calls all to
same-origin paths. Every one of those facts is asserted by a test, so a future
page that needs `unsafe-inline` fails the test rather than quietly widening the
policy.

`default-src 'none'` is the reason the rest can be short: every fetch
destination that is not named below is denied, including ones invented after
this was written.

**Where this sits in the stack matters.** `api/main.py` registers this
*after* the authentication gate, which in Starlette means it wraps it -- the
last middleware registered is the outermost. That ordering is load-bearing:
AC7 requires the `401` to carry these headers too, and a middleware registered
inside the gate never runs for a request the gate refuses.
"""

from __future__ import annotations

from typing import Mapping

# Same-origin everything, and nothing else. See the module docstring for the
# measurement each directive rests on.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'none'",
        # `api/web/*.html` loads one script each, both from /static/.
        "script-src 'self'",
        # One stylesheet, no @import, no url().
        "style-src 'self'",
        # The charts are SVG built through the DOM, not <img>; data: is here
        # for a favicon a browser may request and nothing else.
        "img-src 'self' data:",
        # The four fetch() calls: /ask, /quota, /feedback, /history/data.
        "connect-src 'self'",
        "font-src 'self'",
        # Nothing legitimately embeds this page, and nothing it loads should be
        # able to retarget a relative URL.
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    )
)

# Capabilities this application has no use for. Denying them costs nothing and
# removes them from anything that manages to run in the page's context.
PERMISSIONS_POLICY = ", ".join(
    ("geolocation=()", "microphone=()", "camera=()", "payment=()", "usb=()")
)

SECURITY_HEADERS: Mapping[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    # The results table renders values the database supplied. `nosniff` is what
    # stops a browser deciding a JSON response is really HTML.
    "X-Content-Type-Options": "nosniff",
    # Belt and braces with frame-ancestors, for anything that predates CSP.
    "X-Frame-Options": "DENY",
    # A question is in the URL of nothing, but the referrer would still leak
    # the deployment's hostname to anywhere a user navigates next.
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": PERMISSIONS_POLICY,
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

#: Emitted only for a request that actually arrived over HTTPS (T6, AC9).
#:
#: **Never unconditionally.** HSTS tells a browser to refuse plain HTTP to this
#: host for a year, and it is cached by host, not by port. Sent once from a
#: laptop on `http://localhost:8000`, it makes every other project that ever
#: serves `http://localhost` unreachable in that browser, with no error that
#: names the cause and no way to clear it but a buried settings page. The
#: failure is remote in time from the change that caused it, which is the worst
#: kind this project has met.
#:
#: One year, subdomains included, and no `preload`. Preload is a one-way door
#: -- removal takes months and a request to a browser vendor -- and it is not a
#: decision a deployment that has not chosen a domain yet is entitled to make.
#: That is Q-M, and it is deferred.
STRICT_TRANSPORT_SECURITY = "max-age=31536000; includeSubDomains"

HSTS_HEADER = "Strict-Transport-Security"


def apply(headers, *, overwrite: bool = False, secure: bool = False) -> None:
    """Add the security headers to a response's header mapping, in place.

    `secure` says the request reached the client over HTTPS -- which behind a
    terminating proxy is not the same as the scheme this process accepted, and
    is resolved by `api/http/forwarded.py` from a header that is only believed
    when a configured proxy sent it.

    `overwrite` is False by default so that a route which has deliberately set
    a stricter value for itself keeps it. Nothing does that today; the default
    exists so that adding such a route later does not silently lose its
    intent.
    """
    emit = dict(SECURITY_HEADERS)
    if secure:
        emit[HSTS_HEADER] = STRICT_TRANSPORT_SECURITY
    for name, value in emit.items():
        if overwrite or name not in headers:
            headers[name] = value
