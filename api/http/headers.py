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

# `Strict-Transport-Security` is deliberately absent from the mapping above.
# It belongs to T6, which emits it only for a request that actually arrived
# over HTTPS -- pinning a laptop on plain HTTP to HTTPS for a year is a way to
# make a developer machine unreachable with no obvious cause.


def apply(headers, *, overwrite: bool = False) -> None:
    """Add the security headers to a response's header mapping, in place.

    `overwrite` is False by default so that a route which has deliberately set
    a stricter value for itself keeps it. Nothing does that today; the default
    exists so that adding such a route later does not silently lose its
    intent.
    """
    for name, value in SECURITY_HEADERS.items():
        if overwrite or name not in headers:
            headers[name] = value
