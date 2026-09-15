"""`api/healthcheck.py` -- the container's own liveness probe (Iteration 12 T3).

Hermetic. The probe talks HTTP, but every test here substitutes the transport,
so none of this needs Docker, a database, or a listening socket. That is
deliberate: the probe is the thing that decides whether a deployment is
reported healthy, and a test for it that only runs when the stack is already up
would be testing the case that needs no testing.

The body-reading tests that were here in the first draft are gone, along with
the body reading. `011-ship.md` AC12 settled that the status code carries the
signal and that a probe parsing `/health`'s body would be a second copy of the
health logic. `tests/test_shipping.py` still enforces it.
"""

from __future__ import annotations

import urllib.error

import pytest

from api import healthcheck


class _Response:
    """The subset of `http.client.HTTPResponse` that `check()` actually uses."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _answering(recorder: list[str] | None = None):
    def _urlopen(url: str, timeout: float = 0.0) -> _Response:
        if recorder is not None:
            recorder.append(url)
        return _Response()

    return _urlopen


def _raising(exc: Exception):
    def _urlopen(url: str, timeout: float = 0.0):
        raise exc

    return _urlopen


# --- the port, which is the whole point of the script existing ---------------


def test_the_probe_defaults_to_8000(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PORT", raising=False)
    assert healthcheck.probe_url() == "http://127.0.0.1:8000/health"


def test_the_probe_follows_the_port_the_server_was_told_to_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC6. The Dockerfile's CMD binds `${PORT:-8000}`; this has to agree.

    A probe that kept checking 8000 while uvicorn moved would report a healthy
    service unreachable, which is the failure mode that makes a platform kill a
    container that is working.
    """
    monkeypatch.setenv("PORT", "10000")
    assert healthcheck.probe_url() == "http://127.0.0.1:10000/health"


def test_a_blank_port_falls_back_rather_than_building_a_broken_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PORT=` in an env file is an empty string, not an absent variable."""
    monkeypatch.setenv("PORT", "   ")
    assert healthcheck.probe_url() == "http://127.0.0.1:8000/health"


def test_the_probe_uses_the_loopback_address_and_not_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a dual-stack resolver `localhost` can pick ::1 while uvicorn is on
    0.0.0.0, which fails while everything is in fact fine."""
    monkeypatch.delenv("PORT", raising=False)
    assert "127.0.0.1" in healthcheck.probe_url()
    assert "localhost" not in healthcheck.probe_url()


def test_the_probe_asks_the_endpoint_that_proves_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TCP check would report healthy for a process that cannot answer."""
    monkeypatch.delenv("PORT", raising=False)
    assert healthcheck.probe_url().endswith("/health")


# --- what counts as alive: the status code, and nothing else -----------------


def test_a_2xx_is_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", _answering())
    live, reason = healthcheck.check()
    assert live is True
    assert reason == ""


def test_a_503_is_not_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/health` answers 503 when the database is unreachable.

    `urlopen` raises `HTTPError` on a non-2xx, so the status code does the work
    with no parsing and no second copy of the health logic.
    """
    monkeypatch.setattr(
        healthcheck.urllib.request,
        "urlopen",
        _raising(urllib.error.HTTPError("u", 503, "no", {}, None)),  # type: ignore[arg-type]
    )
    live, reason = healthcheck.check()
    assert live is False
    assert "503" in reason


def test_a_401_is_not_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """If `/health` ever fell inside the auth perimeter, the probe must fail
    loudly rather than report a container that no longer answers anyone."""
    monkeypatch.setattr(
        healthcheck.urllib.request,
        "urlopen",
        _raising(urllib.error.HTTPError("u", 401, "no", {}, None)),  # type: ignore[arg-type]
    )
    assert healthcheck.check()[0] is False


def test_a_closed_port_is_not_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        healthcheck.urllib.request,
        "urlopen",
        _raising(urllib.error.URLError("connection refused")),
    )
    assert healthcheck.check()[0] is False


def test_a_timeout_is_not_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        healthcheck.urllib.request, "urlopen", _raising(TimeoutError("too slow"))
    )
    assert healthcheck.check()[0] is False


def test_the_response_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe runs every 15s for the life of the container; a leaked socket
    each time is a slow failure that looks like something else entirely."""
    response = _Response()
    monkeypatch.setattr(
        healthcheck.urllib.request, "urlopen", lambda url, timeout=0.0: response
    )
    healthcheck.check()
    assert response.closed is True


# --- the exit code, which is the only thing Docker reads ---------------------


def test_main_exits_zero_when_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthcheck, "check", lambda: (True, ""))
    assert healthcheck.main() == 0


def test_main_exits_one_when_not_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthcheck, "check", lambda: (False, "nope"))
    assert healthcheck.main() == 1


# --- the module must stay inert at import time -------------------------------


def test_importing_the_module_performs_no_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tests/test_error_mapping.py` imports every module under `api/`.

    A module that acted at import time would run a probe inside the suite, and
    on a developer machine with something else on 8000 it would do worse than
    that.
    """
    called: list[str] = []
    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", _answering(called))
    import importlib

    importlib.reload(healthcheck)
    assert called == []


def test_the_module_never_parses_the_response_body() -> None:
    """`011-ship.md` AC12, asserted at the new home of the probe.

    Read from the parsed AST, not the text: this module's own docstring
    explains at length that it does not parse the body, and a text search would
    match the explanation rather than the code. That mistake has been made four
    times in this repository.
    """
    import ast
    import pathlib

    source = pathlib.Path(healthcheck.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"read", "json"}, (
                f"the probe reads the response body via .{node.attr}(); the "
                "status code is the signal"
            )
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in getattr(node, "names", [])]
            assert "json" not in names, "the probe imports json to parse the body"
