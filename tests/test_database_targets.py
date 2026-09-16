"""Dynamic database switching — the registry, the gate, and the two live
targets it actually has to distinguish.

Three properties matter beyond "the endpoint returns 200": an unknown target
is refused before it can reach a connection (Invariant #1, and AC23's "never
raises" for `execute_sql`/`get_schema`); two registered targets are genuinely
independent connection pools, not one engine quietly reused (`api/db/
engine.py`'s whole reason for a per-target cache); and the glossary follows
the target rather than being one process-wide constant (Invariant #3).
"""

from __future__ import annotations

import pytest

from api.db.engine import UnknownDatabaseTargetError, get_database_url, get_engine
from api.db.execution import CATEGORY_CONNECTION_ERROR, execute_sql
from api.agent.glossary import current_glossary_terms
from api.targets import CATEGORY_UNKNOWN_DATABASE, DATABASE_TARGETS, DEFAULT_TARGET


# --- the registry itself: closed, and named exactly as documented -----------


def test_the_registry_has_exactly_the_two_documented_targets():
    """A floor and a ceiling, not just a floor: `DATABASE_TARGETS` growing
    silently is as much a surprise to a reader as it shrinking."""
    assert set(DATABASE_TARGETS) == {"chinook", "pagila"}


def test_chinook_reuses_the_pre_existing_single_target_variables():
    """Iteration 14 already wired `QUERYPILOT_DATABASE_URL` and
    `QUERYPILOT_GLOSSARY_FILE` for the one-database deployment. A deployment
    that upgrades and sets neither new variable must keep working unchanged,
    which only holds if chinook's registry entry points at those exact
    names rather than new ones."""
    entry = DATABASE_TARGETS["chinook"]
    assert entry.dsn_env == "QUERYPILOT_DATABASE_URL"
    assert entry.glossary_env == "QUERYPILOT_GLOSSARY_FILE"


def test_pagila_has_no_glossary_of_its_own():
    """Invariant #3, stated as data rather than only as a docstring claim."""
    assert DATABASE_TARGETS["pagila"].glossary_env is None


def test_default_target_is_chinook():
    assert DEFAULT_TARGET == "chinook"


# --- validation happens before any connection is attempted ------------------


def test_unknown_target_is_refused_by_get_database_url():
    with pytest.raises(UnknownDatabaseTargetError):
        get_database_url("not-a-real-database")


def test_unknown_target_is_refused_by_get_engine():
    """Same gate, reached the other way -- `get_engine` must not skip
    `get_database_url`'s check by building a DSN some other way."""
    with pytest.raises(UnknownDatabaseTargetError):
        get_engine("not-a-real-database")


def test_execute_sql_never_raises_on_an_unknown_target():
    """AC23, extended to cover a target rather than only a query. A caller
    that skipped the HTTP-boundary check must still get a categorised
    failure, not an exception the agent loop was never built to catch."""
    result = execute_sql("SELECT 1", target="nonexistent")

    assert result.ok is False
    assert result.category == CATEGORY_CONNECTION_ERROR


def test_a_malicious_target_string_never_reaches_a_connection():
    """Invariant #1's injection-safety claim, exercised directly rather than
    only argued in a docstring: nothing about the shape of this string makes
    it *more* refused than `'nonexistent'` above -- the gate is a set
    membership check, not a format check, so quote characters and SQL
    keywords carry no special danger and no special privilege either."""
    result = execute_sql("SELECT 1", target="'; drop table album; --")

    assert result.ok is False
    assert result.category == CATEGORY_CONNECTION_ERROR


def test_unknown_target_categorises_identically_to_an_unreachable_database():
    """From a caller's side these are the same claim -- 'this database could
    not be reached' -- so they must be the same category, per
    `api/db/execution.py`'s own reasoning for not inventing a second one."""
    result = execute_sql("SELECT 1", target="nonexistent")
    assert result.category == CATEGORY_CONNECTION_ERROR


# --- glossary coupling (Invariant #3) ----------------------------------------


def test_chinook_glossary_has_its_eight_measured_terms():
    terms = current_glossary_terms("chinook")
    assert len(terms) == 8
    assert "active customer" in terms


def test_pagila_glossary_is_empty():
    assert current_glossary_terms("pagila") == {}


def test_an_unknown_target_glossary_is_empty_not_an_error():
    """Fails open, matching `current_glossary_terms`'s own documented D-2
    convention for every other misconfiguration it tolerates."""
    assert current_glossary_terms("nonexistent") == {}


# --- live: two registered targets are genuinely independent pools -----------


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database", "configured_pagila")
def test_chinook_and_pagila_are_different_live_databases():
    """The measurement that actually proves switching works, not just that
    the plumbing compiles: the same query against the two targets returns
    two different `current_database()` values, from two different engines.

    Skips rather than fails when Pagila's opt-in profile is not up
    (`configured_pagila`) -- CI's default `docker compose up` never starts
    it, the same way `tests/test_tls_profile.py` skips rather than fails
    when the `tls` profile is not up.
    """
    chinook_engine = get_engine("chinook")
    pagila_engine = get_engine("pagila")
    assert chinook_engine is not pagila_engine

    chinook_result = execute_sql("SELECT current_database()", target="chinook")
    pagila_result = execute_sql("SELECT current_database()", target="pagila")

    assert chinook_result.ok and pagila_result.ok
    assert chinook_result.rows[0][0] == "chinook"
    assert pagila_result.rows[0][0] == "pagila"


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database", "configured_pagila")
def test_pagila_has_tables_chinook_does_not():
    """A cheap, real discriminator that the two schemas are not secretly the
    same database reached twice. Skips when Pagila's opt-in profile is not
    up, per `configured_pagila`."""
    result = execute_sql(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'film'",
        target="pagila",
    )
    assert result.ok
    assert result.rows[0][0] == 1


# --- the HTTP boundary: /databases, and the 400 both routes share -----------


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_databases_endpoint_lists_both_targets(authed_client):
    body = authed_client.get("/databases").json()

    names = {entry["name"] for entry in body["targets"]}
    assert names == {"chinook", "pagila"}
    assert body["default"] == "chinook"

    available = {entry["name"]: entry["available"] for entry in body["targets"]}
    assert available["chinook"] is True


def test_databases_endpoint_is_not_exempt_from_the_auth_gate():
    from api.http.auth import OPEN_PATHS

    assert "/databases" not in OPEN_PATHS


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_ask_refuses_an_unknown_database_with_400(authed_client):
    body_response = authed_client.post(
        "/ask", json={"question": "how many tracks?", "database": "nonexistent"}
    )
    body = body_response.json()

    assert body_response.status_code == 400
    assert body["ok"] is False
    assert body["category"] == CATEGORY_UNKNOWN_DATABASE
    assert body["sql"] == ""
    assert body["provider"] == ""
    assert body["model"] == ""


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_ask_refuses_an_unknown_database_before_spending_the_daily_reservation(
    authed_client, monkeypatch
):
    """The refusal has to happen before `history.reserve_question`, or a
    client probing target names could exhaust the daily ceiling for nobody's
    benefit."""
    calls = []
    from api.store import history

    real_reserve = history.reserve_question

    def _counting_reserve(identity):
        calls.append(identity)
        return real_reserve(identity)

    monkeypatch.setattr("api.main.history.reserve_question", _counting_reserve)

    authed_client.post(
        "/ask", json={"question": "how many tracks?", "database": "nonexistent"}
    )

    assert calls == [], "an unknown database must not consume a reservation"


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_schema_refuses_an_unknown_database_with_400(authed_client):
    response = authed_client.get("/schema", params={"database": "nonexistent"})
    assert response.status_code == 400


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database", "configured_pagila")
def test_schema_against_pagila_returns_pagila_tables(authed_client):
    """Skips when Pagila's opt-in profile is not up, per `configured_pagila`."""
    body = authed_client.get("/schema", params={"database": "pagila"}).json()
    names = {table["name"] for table in body["tables"]}
    assert "film" in names
    assert "album" not in names, "chinook's tables must not appear under pagila"
