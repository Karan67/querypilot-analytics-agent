"""Prove the prohibition refuses, the instrument measures, and neither is vacuous.

Every test here is the red half of a mutation recorded in
`specs/014-test-isolation-plan.md` §6. Where the repository contains no case that
separates two implementations — the two `inconsistent` combinations, which the
equality guard will make impossible — the test drives a factored helper on
synthetic input instead of scanning the tree. This project has been bitten by a
scan that stayed green after its discriminating clause was deleted, because
nothing in the tree told the two versions apart.

Subprocess runs each pass their own `--junitxml`. `pytest.ini` prepends one, so a
child that does not override it rewrites the parent's report mid-session.
"""

from __future__ import annotations

import ast
import configparser
import json
import os
import pathlib
import shlex
import subprocess
import sys

import pytest

from tests.isolation import (
    DATABASE_FIXTURE,
    NEEDS_DB_MARKER,
    PROHIBITED_TARGETS,
    DatabaseAccessProhibitedError,
    isolation_decision,
    prohibited_engine_access,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Never connected to. `create_engine` is lazy, so this warms `get_engine`'s
#: cache without opening a socket, which is the whole point of the eviction
#: tests below. Not a real DSN and not read from the environment, so it does not
#: violate the no-hardcoded-DSN rule: nothing dials it.
UNDIALLED_DSN = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/never"


def _run_pytest(args: list[str], tmp_path: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-p", "no:cacheprovider",
         f"--junitxml={tmp_path / 'junit.xml'}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )


# --- The decision, on input the repository will never contain ----------------


@pytest.mark.parametrize(
    ("marked", "closure", "expected"),
    [
        (True, True, "allow"),
        (False, False, "prohibit"),
        (True, False, "inconsistent"),
        (False, True, "inconsistent"),
    ],
)
def test_isolation_decision_separates_all_four_combinations(marked, closure, expected):
    """The two `inconsistent` cases are why this is a pure function.

    Once the equality guard lands, no test in the repository is marked without
    the fixture or vice versa — so a branch handling those has no reachable input
    and cannot be shown to still work. Collapsing `inconsistent` into a silent
    `return`, or into `prohibit`, must fail here or it fails nowhere.
    """
    assert isolation_decision(marked=marked, closure=closure) == expected


def test_an_inconsistent_decision_is_not_quietly_treated_as_allowed():
    """Guards the specific collapse that would look harmless in review."""
    assert isolation_decision(marked=True, closure=False) != "allow"
    assert isolation_decision(marked=False, closure=True) != "prohibit"


def test_constructs_engine_steps_aside_without_claiming_a_database():
    """The third category, which the census structurally cannot find.

    A test that builds an Engine against a closed port issues no statements, so
    traffic-based measurement scores it hermetic — correctly, since it passes
    with the stack down. But it does call `create_engine`, so the prohibition
    would refuse it. `constructs_engine` says *the Engine is deliberate* and
    nothing more: no fixture, no skipping, still in the hermetic lane.
    """
    assert isolation_decision(
        marked=False, closure=False, constructs_engine=True
    ) == "allow"
    assert isolation_decision(marked=False, closure=False) == "prohibit"


def test_constructs_engine_cannot_wave_through_a_half_declaration():
    """It is checked last, and only for a test that declared nothing.

    Otherwise it becomes the escape hatch for the failure the `inconsistent`
    branch exists to name — a test marked `needs_db` with no fixture, running
    unprobed against whatever DSN is ambient — and one marker silently disables
    the other's guarantee.
    """
    for marked, closure in ((True, False), (False, True)):
        assert isolation_decision(
            marked=marked, closure=closure, constructs_engine=True
        ) == "inconsistent"


# --- The prohibition itself ---------------------------------------------------


def test_an_undeclared_test_cannot_build_an_engine():
    from api.db.engine import get_engine

    with prohibited_engine_access():
        with pytest.raises(DatabaseAccessProhibitedError):
            get_engine()


def test_the_refusal_names_the_marker_and_the_fixture():
    """A failure nobody can act on teaches people to delete the guard."""
    from api.db.engine import get_engine

    with prohibited_engine_access():
        with pytest.raises(DatabaseAccessProhibitedError) as caught:
            get_engine()

    message = str(caught.value)
    assert NEEDS_DB_MARKER in message
    assert DATABASE_FIXTURE in message


def test_the_sqlalchemy_belt_is_not_vacuous():
    """The second entry in PROHIBITED_TARGETS is a distinct binding, not a dup.

    `api/db/engine.py` did `from sqlalchemy import create_engine` at import, so
    patching one name leaves the other pointing at the real factory. Without this
    the belt is a line nobody exercises.
    """
    import sqlalchemy

    with prohibited_engine_access():
        with pytest.raises(DatabaseAccessProhibitedError):
            sqlalchemy.create_engine(UNDIALLED_DSN)


def test_the_prohibition_is_not_swallowed_by_the_introspection_handler(monkeypatch):
    """The base class, proved against the handler that would eat a RuntimeError.

    `api/db/introspection.py:342` catches `(SQLAlchemyError, RuntimeError)` and
    re-raises as `SchemaIntrospectionError`. Base this error on `RuntimeError` —
    the choice house style would make — and `get_schema()` reports a schema
    error, `test_ac17_unreachable_database_raises_schema_error` stays green, and
    the prohibition is invisible.

    The DSN is set explicitly so an unset variable cannot raise `RuntimeError`
    here for an unrelated reason and make this pass without the prohibition
    firing at all.
    """
    from api.db.engine import DATABASE_URL_ENV
    from api.db.introspection import SchemaIntrospectionError, get_schema

    monkeypatch.setenv(DATABASE_URL_ENV, UNDIALLED_DSN)

    with prohibited_engine_access():
        with pytest.raises(DatabaseAccessProhibitedError):
            get_schema()

    assert not issubclass(DatabaseAccessProhibitedError, SchemaIntrospectionError)


def test_the_error_escapes_a_blanket_except_clause():
    """`except Exception` must not be able to absorb it."""
    assert not issubclass(DatabaseAccessProhibitedError, Exception)
    assert issubclass(DatabaseAccessProhibitedError, BaseException)

    from api.db.engine import get_engine

    reached_handler = False
    with prohibited_engine_access():
        try:
            try:
                get_engine()
            except Exception:  # noqa: BLE001 - the point of the test
                reached_handler = True
        except DatabaseAccessProhibitedError:
            pass
    assert not reached_handler


def test_nothing_on_the_database_path_swallows_the_prohibition():
    """Keeps the premise of the base-class decision true as `api/db/` changes.

    Parsed, not grepped: every docstring in this iteration contains the string
    `except Exception`, and a text scan would match its own explanation. That
    exact bug has landed four times in this repository.
    """
    offenders = []
    for path in sorted((REPO_ROOT / "api" / "db").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            caught = node.type
            if caught is None:
                offenders.append(f"{path.name}:{node.lineno} bare except")
            elif isinstance(caught, ast.Name) and caught.id == "Exception":
                offenders.append(f"{path.name}:{node.lineno} except Exception")

    assert not offenders, (
        "a blanket handler in api/db/ would absorb DatabaseAccessProhibitedError "
        f"and report it as an ordinary failure: {offenders}"
    )


def test_the_scan_for_blanket_handlers_can_actually_see_one(tmp_path):
    """The absence assertion above is worthless if the walk finds nothing."""
    source = "try:\n    pass\nexcept Exception:\n    pass\n"
    handlers = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Name)
        and node.type.id == "Exception"
    ]
    assert len(handlers) == 1


# --- The lru_cache hazard -----------------------------------------------------


@pytest.mark.constructs_engine
def test_a_warm_engine_cache_does_not_survive_into_a_prohibited_block(monkeypatch):
    """The eviction, which nothing else in the suite can observe.

    `configured_database` is session-scoped, so the first test that needs a
    database leaves a live Engine memoised in `get_engine`'s `lru_cache`. Every
    unmarked test afterwards would get a **cache hit** — connecting without ever
    calling `create_engine`, so the patch never fires. Remove the setup eviction
    and `currsize` stays 1 and `get_engine()` hands back a working engine.

    Collection order hides this: the files that warm the cache sort after the
    ones that would notice. So the warm state is built here explicitly rather
    than inherited from whatever ran first.
    """
    from api.db.engine import DATABASE_URL_ENV, get_engine

    monkeypatch.setenv(DATABASE_URL_ENV, UNDIALLED_DSN)
    get_engine.cache_clear()
    get_engine()  # lazy: constructs a pool, opens no socket
    assert get_engine.cache_info().currsize == 1

    with prohibited_engine_access():
        assert get_engine.cache_info().currsize == 0, (
            "a memoised Engine outlived the eviction, so the prohibition is "
            "inert for every test after the first database test"
        )
        with pytest.raises(DatabaseAccessProhibitedError):
            get_engine()

    get_engine.cache_clear()


def test_the_patch_target_keeps_conftest_s_cache_clear_working():
    """Retargeting the patch to `get_engine` breaks `cache_clear` — loudly.

    `tests/conftest.py` and eight other sites call
    `get_engine.cache_clear()`. Replacing `get_engine` with a plain function
    removes it, which is the tripwire that stops the wrong seam being chosen
    quietly. Asserting the attribute survives keeps that tripwire armed.
    """
    from api.db.engine import get_engine

    assert ("api.db.engine", "create_engine") in PROHIBITED_TARGETS
    assert not any(attribute == "get_engine" for _, attribute in PROHIBITED_TARGETS)

    with prohibited_engine_access():
        assert callable(get_engine.cache_clear)
        assert get_engine.cache_info().currsize == 0


# --- The marker registration --------------------------------------------------


def test_the_marker_is_registered_in_pytest_ini():
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "pytest.ini", encoding="utf-8")
    markers = parser["pytest"]["markers"]
    assert NEEDS_DB_MARKER in markers


def test_strict_markers_is_in_force_not_merely_in_the_file(tmp_path):
    """Behavioural, because `--strict-markers` present but overridden is worse
    than absent: it reads as protection nobody has.

    A marker this repository will never register must turn collection red. Delete
    `--strict-markers` from `addopts` and pytest accepts it silently, the run
    exits 0, and this fails.
    """
    probe = tmp_path / "test_unregistered_marker_probe.py"
    probe.write_text(
        "import pytest\n\n\n"
        "@pytest.mark.definitely_not_a_registered_marker\n"
        "def test_probe():\n    pass\n",
        encoding="utf-8",
    )

    completed = _run_pytest(
        [str(probe), "--collect-only", "-q", "-c", "pytest.ini"], tmp_path
    )

    assert completed.returncode != 0, (
        "an unregistered marker was accepted, so --strict-markers is not in "
        f"force:\n{completed.stdout[-1500:]}"
    )
    assert "definitely_not_a_registered_marker" in completed.stdout + completed.stderr


def test_addopts_still_carries_strict_markers():
    """The weak half of the pair above, kept because it names the mechanism."""
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "pytest.ini", encoding="utf-8")
    assert "--strict-markers" in shlex.split(parser["pytest"]["addopts"])


# --- The instrument -----------------------------------------------------------


@pytest.mark.needs_db
@pytest.mark.usefixtures("configured_database")
def test_the_census_counts_statements_a_test_really_issued(tmp_path):
    """Mutations (a) and (b): the listener exists, and it survives a cache_clear.

    **Marked by hand, not by the census.** The traffic this asserts happens in a
    child process, where the parent's `before_cursor_execute` listener cannot see
    it, so the census scores this test hermetic. It is not: with the stack down
    the child records nothing and the assertion fails. The hermetic lane found
    that, which is the argument for running `-m "not needs_db"` in CI rather than
    trusting the partition — a census cannot measure a subprocess.

    `tests/test_tools.py` calls `get_engine.cache_clear()` twice partway through,
    so the engine object changes mid-file. A listener attached to an *instance*
    goes blind after that, and the tests below those call sites report zero. This
    asserts traffic for a test that runs after both of them.
    """
    census = tmp_path / "census.json"
    completed = _run_pytest(
        ["tests/test_tools.py", "-q", "-p", "tools.measure_db_access",
         f"--db-access-out={census}"],
        tmp_path,
    )
    assert completed.returncode == 0, completed.stdout[-2000:]

    data = json.loads(census.read_text(encoding="utf-8"))
    late = next(
        entry
        for nodeid, entry in data["tests"].items()
        if nodeid.endswith("::test_execute_sql_wrapper_delegates_unchanged")
    )
    assert sum(late["statements"].values()) > 0, (
        "no traffic recorded for a test that runs after two cache_clear() calls "
        "-- the listener is attached per-instance rather than to the Engine class"
    )
    assert data["totals"]["traffic"] > 0


def test_the_closure_walk_finds_every_way_a_test_declares_a_database(tmp_path):
    """Mutations (c) and (d): the walk is not empty, and it excludes autouse.

    Three landmark files, one per code path: a direct parameter, a transitive
    reach through another fixture, and a module-level `usefixtures`. If pytest
    changes `_fixtureinfo`'s shape the walk returns empty sets and the census
    reports a spotlessly declared suite — the loudest possible way to be wrong.

    The upper bound is the other half. `initialnames` folds autouse in, and while
    `configured_database` is autouse that would mark every collected item as
    declaring a database. A strict inequality catches it; a lower bound alone
    would not.
    """
    census = tmp_path / "closure.json"
    completed = _run_pytest(
        ["tests/test_tools.py", "tests/test_expert_tier.py",
         "tests/test_validator_gates.py", "--collect-only", "-q",
         "-p", "tools.measure_db_access", f"--db-access-out={census}"],
        tmp_path,
    )
    assert completed.returncode == 0, completed.stdout[-2000:]

    data = json.loads(census.read_text(encoding="utf-8"))
    for path, landmark in data["closure_landmarks"].items():
        assert landmark["found"], f"closure walk found nothing in {path}"

    totals = data["totals"]
    assert 0 < totals["closure"] < totals["items"], (
        "every collected item reports a declared database, which is what "
        "`initialnames` returns -- the walk is counting autouse fixtures"
    )


def test_the_census_verdicts_partition_every_test():
    """The four verdicts are exhaustive and disjoint, so none can be lost."""
    from tools.measure_db_access import verdict_for

    assert verdict_for(traffic=0, closure=False) == "hermetic"
    assert verdict_for(traffic=9, closure=True) == "declared"
    assert verdict_for(traffic=9, closure=False) == "traffic_without_closure"
    assert verdict_for(traffic=0, closure=True) == "closure_without_traffic"


def test_the_committed_census_backs_the_numbers_in_the_spec():
    """§2 is derived, not typed. If the artifact drifts, the spec is wrong."""
    census = json.loads(
        (REPO_ROOT / "specs" / "014-test-isolation-census.json").read_text(
            encoding="utf-8"
        )
    )
    totals = census["totals"]

    assert totals["items"] == 1348
    assert totals["hermetic"] == 888
    assert totals["traffic_without_closure"] == 65
    assert sum(
        totals[verdict]
        for verdict in (
            "hermetic",
            "declared",
            "traffic_without_closure",
            "closure_without_traffic",
        )
    ) == totals["items"]


# --- The partition (T4) -------------------------------------------------------


def _collect_census(tmp_path: pathlib.Path) -> dict:
    """Collect the whole suite under the plugin. No database required."""
    census = tmp_path / "collected.json"
    completed = _run_pytest(
        ["--collect-only", "-q", "-p", "tools.measure_db_access",
         f"--db-access-out={census}"],
        tmp_path,
    )
    assert completed.returncode == 0, completed.stdout[-2000:]
    return json.loads(census.read_text(encoding="utf-8"))


def test_the_closure_walk_still_excludes_autouse_fixtures(tmp_path):
    """The `argnames`-not-`initialnames` choice, kept observable after T4.

    Before the partition this was self-evident: `initialnames` folds autouse in,
    `configured_database` was autouse, so the wrong attribute reported all 1,348
    items as declaring a database and any count caught it. T4 removed that
    autouse and with it the signal — swapping the attribute back would now be
    invisible, because nothing else autouse reaches the database.

    `isolated_spend_ledger` restores the discrimination. It is autouse, no test
    requests it by name, so it belongs to **every** closure under `initialnames`
    and to **none** under `argnames`.
    """
    data = _collect_census(tmp_path)
    assert data["autouse_sentinel_leaks"] == 0, (
        "an autouse fixture no test requested appeared in the computed closure, "
        "so the walk is reporting inherited fixtures as declarations"
    )


def test_the_closure_walk_now_agrees_with_pytests_public_attribute(tmp_path):
    """Retires the private-API walk as a source of risk.

    While `configured_database` was autouse, `item.fixturenames` held it for
    every item and only the private walk could tell a declaration from an
    inheritance. With autouse gone the two must agree, and the guard above can
    rely on the public attribute.
    """
    data = _collect_census(tmp_path)
    assert data["closure_walk_agrees_with_fixturenames"] is True


def test_the_readiness_probe_does_not_mark_the_test_it_lands_on(tmp_path):
    """The one test the census over-counted, kept unmarked on purpose.

    `configured_database` is session-scoped, so its `SELECT 1` runs in the setup
    phase of whichever test is collected first and a per-test instrument charges
    it there. Marking on total traffic would give a database dependency to a test
    whose entire assertion is that no endpoint has one.
    """
    data = _collect_census(tmp_path)
    probe_victim = (
        "tests/test_ask_endpoint.py::"
        "test_ac4_no_endpoint_touches_the_database_directly"
    )
    assert probe_victim in data["tests"]
    assert not data["tests"][probe_victim]["marked"]
    assert not data["tests"][probe_victim]["closure"]


def test_the_auth_suite_runs_without_a_database(tmp_path):
    """B-15, executed rather than asserted.

    The charter opened B-15 because all 64 tests in `tests/test_auth.py` skipped
    with the stack down. Measurement corrected both halves of that: the file
    held 67 tests at the time, six of which really do need Postgres because
    `/health` goes through `execute_sql()` by design. So the deselection is
    part of the claim, not a way around it.

    **Re-measured at `018-ui-redesign.md` T5**, after `/schema` was added to
    `PROTECTED`: the file now holds 74 tests, 67 of them hermetic and 7
    needing Postgres. The one extra `needs_db` test is not this iteration's —
    it predates this change and the number here was simply never revisited
    against it, which is its own small instance of `HANDOFF.md` §6's "a
    measurement that survives only in conversation is not a measurement":
    read from the real run below rather than carried forward by memory.

    Restore `autouse=True` on `configured_database` and every one of the 67
    skips instead.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_auth.py", "-m", "not needs_db",
         "-q", "--no-header", "-p", "no:cacheprovider",
         f"--junitxml={tmp_path / 'junit.xml'}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ,
             "TEST_DATABASE_URL": UNDIALLED_DSN,
             "QUERYPILOT_TESTS_REQUIRE_DATABASE": ""},
    )

    assert completed.returncode == 0, (
        "the auth suite cannot run without a database it does not need — B-15 is "
        f"back:\n{completed.stdout[-1500:]}"
    )
    assert "skipped" not in completed.stdout, (
        f"something skipped rather than ran:\n{completed.stdout[-800:]}"
    )
    assert "67 passed" in completed.stdout, completed.stdout[-400:]


def test_the_partition_script_is_idempotent():
    """Re-deriving the partition from the census must change nothing.

    The guarantee that the 214 committed edits are what the census implies,
    rather than what a one-off run happened to produce. If this fails, either a
    test moved between lanes or someone hand-edited a marker.
    """
    completed = subprocess.run(
        [sys.executable, "tools/apply_partition.py", "--check"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "target tests: 459" in completed.stdout
    assert "parametrized functions with mixed cases" not in completed.stdout


def test_the_hermetic_lane_stays_below_the_ci_floor():
    """§2.7, as a standing check rather than a one-time reading.

    `ci/require_executed_tests.py` defends "the database died and someone deleted
    the flag". Today that means zero tests execute; after the partition it means
    the hermetic lane executes. The moment the hermetic count passes the floor,
    that guard is dead and a lost database is a green build again.
    """
    from ci.require_executed_tests import DEFAULT_FLOOR

    census = json.loads(
        (REPO_ROOT / "specs" / "014-test-isolation-census.json").read_text(
            encoding="utf-8"
        )
    )
    hermetic = census["totals"]["hermetic"]

    assert hermetic < DEFAULT_FLOOR, (
        f"{hermetic} tests would run with no database, clearing the floor of "
        f"{DEFAULT_FLOOR} -- ci/require_executed_tests.py no longer detects a "
        "pipeline that lost its database"
    )


def test_the_prohibition_is_active_without_being_requested():
    """The autouse itself, which nothing else in this file proves.

    This test requests no fixture, carries no marker, and never enters
    `prohibited_engine_access()`. The only thing that can make `get_engine()`
    raise here is `prohibit_database_access` being `autouse=True` — so drop the
    autouse and this is the test that notices.

    Every other prohibition test drives the context manager directly, which
    proves the mechanism works and says nothing about whether it is switched on.
    """
    from api.db.engine import get_engine

    with pytest.raises(DatabaseAccessProhibitedError):
        get_engine()
