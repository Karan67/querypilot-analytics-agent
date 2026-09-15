"""The container's own liveness probe (Iteration 12 T3, AC4 and AC6).

Before Iteration 12 the only healthcheck this system had was declared in
`docker-compose.yml`, and it hardcoded `http://127.0.0.1:8000/health`. That was
two problems wearing one coat. A platform that never reads a compose file --
which is every platform the charter names -- ran the container with no probe at
all. And the port appeared in three places that had no way of disagreeing out
loud: the Dockerfile's `CMD`, the compose healthcheck, and `EXPOSE`.

This script is the single place the probe resolves a port, and it resolves the
same `$PORT` the server binds, so the two cannot drift.

**It does not read the response body, deliberately.** `011-ship.md` AC12 settled
that: `/health` answers 503 when the database is unreachable, `urlopen` raises
on a non-2xx, and so the status code already carries the whole signal. A probe
that parsed the body for `"ok"` would be a second copy of the health logic,
free to drift from the first, and it would be the text-matching antipattern
`003` argued against in the one place a typed alternative exists. The first
draft of this file did parse the body; `tests/test_shipping.py` caught it.

That restraint is also what lets T10 land. Reducing the anonymous `/health`
body to `{"status": "ok"}` changes nothing here, because nothing here looks
inside it.

It presents no credential, which is why `/health` has to stay outside the
authentication perimeter (`api/http/auth.py::OPEN_PATHS`).

Importing this module does nothing. That matters: `tests/test_error_mapping.py`
walks every module under `api/` with `pkgutil.walk_packages` and imports each
one, so a module that acted at import time would run inside the test suite.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

DEFAULT_PORT = "8000"
TIMEOUT_SECONDS = 5.0


def probe_url() -> str:
    """Build the health URL from the same `$PORT` the server binds.

    `127.0.0.1` rather than `localhost`: the probe runs inside the container,
    and on a dual-stack resolver `localhost` can resolve to `::1` while uvicorn
    is bound to `0.0.0.0`, which is IPv4 only. The container would then report
    unhealthy while being entirely fine.
    """
    port = os.environ.get("PORT", "").strip() or DEFAULT_PORT
    return f"http://127.0.0.1:{port}/health"


def check() -> tuple[bool, str]:
    """Return whether the service is live, and a reason when it is not.

    The reason is for a human reading `docker inspect`'s health log. It names
    the status and the URL, never a response body -- the body carries
    configuration detail and this string is written to disk on every failure.
    """
    url = probe_url()
    try:
        urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS).close()
    except urllib.error.HTTPError as exc:
        return False, f"{url} answered {exc.code}"
    except urllib.error.URLError as exc:
        return False, f"{url} unreachable: {exc.reason}"
    except (TimeoutError, OSError) as exc:
        return False, f"{url} unreachable: {exc}"
    return True, ""


def main() -> int:
    live, reason = check()
    if live:
        return 0
    print(reason, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
