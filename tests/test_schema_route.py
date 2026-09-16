"""018-ui-redesign.md T5 — `GET /schema`, proven complete and safe.

Two properties matter beyond "it returns tables": the route reaches the
database only through the same path `/ask` already does (AC5 — asserted
generically for the whole module by
`tests/test_ask_endpoint.py::test_ac4_the_only_database_call_in_the_module_is_execute_sql`,
which a direct `get_schema()` import here would trip), and a schema-
introspection failure renders as a legible message rather than a stack trace
(AC7).
"""

from __future__ import annotations

import pytest

from api.http.auth import OPEN_PATHS


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_schema_returns_the_real_catalog(authed_client):
    """AC4: table name, kind, and each column's name/type, for a real
    Chinook relation -- and nothing beyond that (§7 Q-C's narrow contract,
    checked here against the live route as well as the unit test in
    `test_serialization.py`)."""
    body = authed_client.get("/schema").json()

    tables = {table["name"]: table for table in body["tables"]}
    assert "album" in tables
    album = tables["album"]
    assert album["kind"] == "table"

    column_names = {column["name"] for column in album["columns"]}
    assert {"album_id", "title", "artist_id"} <= column_names

    assert set(album.keys()) == {"name", "kind", "columns"}
    assert set(album["columns"][0].keys()) == {"name", "type"}


def test_schema_is_not_exempt_from_the_auth_gate():
    """Nothing about adding this route also adds it to `auth.OPEN_PATHS`."""
    assert "/schema" not in OPEN_PATHS


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_an_unreadable_schema_is_a_legible_failure_not_a_stack_trace(
    authed_client, monkeypatch
):
    """AC7. Same failure `/health` reports as `database.connected: false`,
    phrased for this route's own shape rather than reusing that one.

    Patches `_deployed_fingerprints` -- the same seam `/ask` already depends
    on for exactly this failure mode -- rather than trying to break Postgres
    itself.
    """
    monkeypatch.setattr("api.main._deployed_fingerprints", lambda target=None: None)

    response = authed_client.get("/schema")

    assert response.status_code == 503
    body = response.json()
    assert body["error"]
    assert "Traceback" not in body["error"]
