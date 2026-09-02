"""Tests for the agent's tool registry — `api/agent/tools.py`.

Task T8. The registry is deliberately thin (decision D-2), so these tests check
the two things that can actually break: that the wrapper is genuinely a
pass-through, and that the registry points at the wrapper rather than at
something else that merely looks like it.

The convention being protected is from `specs/000-project.md` §5 — `tools.py`
holds the surface, domain modules hold the logic.
"""

from __future__ import annotations

import inspect as pyinspect

import pytest

from api.agent import tools
from api.db import introspection


def test_registry_exposes_exactly_the_tools_built_so_far():
    """Iteration 1 is complete: all four tools from the roadmap exist."""
    assert set(tools.TOOLS) == {
        "get_schema",
        "sample_rows",
        "validate_sql",
        "execute_sql",
    }


def test_validate_sql_wrapper_delegates_unchanged():
    """The wrapper must not summarise or reshape the reason. The agent retries
    against that text, so flattening it here would remove the detail the retry
    depends on."""
    from api.safety import validator

    for sql in [
        "SELECT 1",
        "DELETE FROM track",
        "WITH d AS (DELETE FROM t RETURNING 1 AS x) SELECT x FROM d",
        "",
    ]:
        assert tools.validate_sql(sql) == validator.validate_sql(sql)


def test_validate_sql_registry_entry_is_the_wrapper():
    assert tools.TOOLS["validate_sql"] is tools.validate_sql


def test_registry_entry_is_the_wrapper_itself():
    """Identity, not equality: a registry pointing at a different callable that
    happens to behave the same today is a bug waiting for Iteration 4."""
    assert tools.TOOLS["get_schema"] is tools.get_schema


def test_every_registry_value_is_callable():
    non_callable = {name: v for name, v in tools.TOOLS.items() if not callable(v)}
    assert not non_callable


def test_wrapper_returns_what_the_implementation_returns():
    """The wrapper adds nothing and subtracts nothing. `Schema` is frozen and
    compares structurally, so this is a real equality check rather than an
    identity coincidence."""
    assert tools.get_schema() == introspection.get_schema()


def test_wrapper_returns_a_schema_instance():
    assert isinstance(tools.get_schema(), introspection.Schema)


def test_ac14_wrapper_takes_no_parameters():
    """The no-injection-surface property has to survive the indirection. A
    wrapper that grew a `schema_name` argument would forfeit it while the
    implementation still looked correct."""
    assert not pyinspect.signature(tools.get_schema).parameters


def test_wrapper_propagates_introspection_errors(monkeypatch):
    """Errors must not be flattened on the way through. From Iteration 4 the
    agent reacts to this exception, and a wrapper that swallowed it into a
    generic failure would remove the reason it needs."""
    from api.db import engine as engine_module

    engine_module.get_engine.cache_clear()
    monkeypatch.setenv(
        engine_module.DATABASE_URL_ENV,
        "postgresql+psycopg://nobody:nobody@querypilot-no-such-host:5432/nothing",
    )
    try:
        with pytest.raises(introspection.SchemaIntrospectionError):
            tools.get_schema()
    finally:
        engine_module.get_engine.cache_clear()


def test_execute_sql_wrapper_delegates_unchanged():
    from api.db import execution

    for sql in ["SELECT 1", "DROP TABLE track", ""]:
        assert tools.execute_sql(sql) == execution.execute_sql(sql)


def test_execute_sql_registry_entry_is_the_wrapper():
    assert tools.TOOLS["execute_sql"] is tools.execute_sql


def test_execute_sql_validates_through_the_registry(configured_database):
    """The registry entry must carry the safety layer with it. A tool reachable
    by name that skipped Gate 2 would be the worst possible regression."""
    result = tools.TOOLS["execute_sql"]("DROP TABLE track")
    assert result.ok is False
    assert result.category == "rejected"


def test_sample_rows_wrapper_delegates_unchanged(configured_database):
    from api.db import sampling

    for name in ("track", "invoice_totals", "nope"):
        assert tools.sample_rows(name) == sampling.sample_rows(name)


def test_sample_rows_registry_entry_is_the_wrapper():
    assert tools.TOOLS["sample_rows"] is tools.sample_rows


def test_sample_rows_through_the_registry_enforces_the_allowlist(configured_database):
    """The registry entry must carry the allowlist with it. A tool reachable by
    name that could sample `pg_stat_activity` would undo the only gate."""
    result = tools.TOOLS["sample_rows"]("pg_stat_activity")
    assert result.ok is False
    assert result.category == "unknown_relation"


def test_tools_module_holds_no_implementation():
    """Guards the §5 convention structurally: the registry delegates, it does
    not introspect. If SQLAlchemy is ever imported here, the split has eroded.
    """
    source = pyinspect.getsource(tools)
    assert "sqlalchemy" not in source.lower()
    assert "sqlglot" not in source.lower()
    assert "inspect(" not in source
