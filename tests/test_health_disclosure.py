"""`/health`'s disclosure reduction (Iteration 12 T10, AC24).

Hermetic. This file is deliberately narrow: it owns the *shape* of the
anonymous-versus-authenticated split, and nothing about the database or
history logic those bodies wrap, which stay covered where they already were
(`tests/test_ask_recording.py`, `tests/test_auth.py`).

The property this reverses is `013-auth.md`'s D-4, which kept `/health`
unchanged specifically so an anonymous operator could see *why*
authentication was misconfigured. T10 requires a credential for that now, and
the one case where this genuinely costs something -- `QUERYPILOT_USERS` itself
being unset or broken, where no credential can ever authenticate -- is named
directly in `tests/test_auth.py::
test_an_unconfigured_deployment_no_longer_says_why_anonymously` rather than
hidden.
"""

from __future__ import annotations

import json

import pytest

from api.http import auth


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_an_anonymous_healthy_caller_sees_only_status(anonymous_client) -> None:
    response = anonymous_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_an_authenticated_caller_still_sees_the_database_role_and_name(
    authed_client,
) -> None:
    """AC24's exact wording: role name, database name and table count require
    a credential. This is the positive proof that a credential still unlocks
    them, so the test above cannot be passing because the fields vanished
    everywhere rather than being gated."""
    payload = authed_client.get("/health").json()
    assert payload["database"]["user"] == "querypilot_ro"
    assert payload["database"]["database"] == "chinook"
    assert isinstance(payload["database"]["public_tables"], int)


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_an_authenticated_caller_sees_every_diagnostic_block(authed_client) -> None:
    """Nothing else quietly moved behind the credential wrong -- history,
    proxy, secrets and spend are all still where T6-T9 put them."""
    payload = authed_client.get("/health").json()
    for key in ("database", "history", "auth", "proxy", "secrets", "spend"):
        assert key in payload, f"{key} missing from the authenticated body"


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_a_wrong_secret_is_still_anonymous_as_far_as_health_is_concerned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller who gets the password wrong learns nothing more than one who
    sent no header at all -- `/health` does not distinguish "tried and
    failed" from "did not try", on the same principle `_unauthorized()` states
    for the 401 itself."""
    from fastapi.testclient import TestClient

    from api.main import app

    monkeypatch.setenv(auth.USERS_ENV, json.dumps({"analyst": "the-real-secret"}))
    client = TestClient(app)
    client.auth = ("analyst", "wrong-guess")

    response = client.get("/health")
    assert response.json() == {"status": "ok"}


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_an_unknown_identity_is_also_anonymous(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from api.main import app

    monkeypatch.setenv(auth.USERS_ENV, json.dumps({"analyst": "the-real-secret"}))
    client = TestClient(app)
    client.auth = ("somebody-else", "anything")

    response = client.get("/health")
    assert response.json() == {"status": "ok"}


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_a_malformed_authorization_header_is_treated_as_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not Basic at all, or not base64, or no colon in the decoded value --
    `parse_basic` already returns `None` for all three; this proves the health
    handler treats that `None` the same as no header, rather than raising."""
    from fastapi.testclient import TestClient

    from api.main import app

    monkeypatch.setenv(auth.USERS_ENV, json.dumps({"analyst": "the-real-secret"}))
    client = TestClient(app)

    response = client.get("/health", headers={"Authorization": "Bearer not-basic-at-all"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_identify_caller_never_raises_on_a_broken_credential_map() -> None:
    """The combinator's whole reason to exist: `/health` gets no 401 to fall
    back on, so `current_identities` raising must become `None`, not an
    uncaught exception that turns a healthcheck into a 500."""
    assert auth.identify_caller("Basic YW55dGhpbmc6YXQgYWxs", None) is None
    assert auth.identify_caller("Basic YW55dGhpbmc6YXQgYWxs", "not json at all") is None


def test_identify_caller_returns_the_name_on_a_real_match() -> None:
    """The positive case, isolated from HTTP -- proves the combinator itself
    is correct, independent of how `/health` calls it."""
    import base64

    header = "Basic " + base64.b64encode(b"analyst:the-real-secret").decode()
    identities = json.dumps({"analyst": "the-real-secret"})
    assert auth.identify_caller(header, identities) == "analyst"
