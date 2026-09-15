"""Measure which tests actually reach Postgres, and which only look like they do.

A pytest plugin, activated on demand:

    python -m pytest -p tools.measure_db_access \
        --db-access-out=specs/014-test-isolation-census.json \
        --junitxml=.pytest_cache/census-junit.xml -q

**A plugin rather than a conftest fixture**, because it has to measure the tree
as it is. Anything living in `tests/conftest.py` runs on every invocation and
becomes part of what it is measuring; this is inert unless `-p` asks for it.

**Pass `--junitxml` explicitly.** `pytest.ini` prepends one, and a census run
would otherwise overwrite the report the run under measurement wrote.

---

Two instruments, because "does this test need a database?" is two questions.

**Did traffic happen?** (`before_cursor_execute`) A statement reached a cursor.
This is ground truth and it is the only thing that cannot be argued with.

**Was the dependency declared?** (the fixture closure) The test, or its module,
*asked* for `configured_database` — by parameter or by `usefixtures`.

The gap between them is the finding. Before Iteration 11 T4, `tests/conftest.py`
made `configured_database` `autouse=True`, so every test inherited a database
whether it declared one or not; this instrument existed to say which tests that
was doing real work for, and measured 65 across 12 files. After T4 the gap is
closed and the plugin's job changes: it is what
`tests/test_isolation.py::test_the_marked_set_equals_the_closure_set` uses to
prove the gap stays closed.

---

Two mechanical decisions worth stating, because the obvious alternative to each
is wrong in a way that fails silently.

**The listener attaches to the `Engine` class, not to `get_engine()`'s
instance.** `get_engine` is `lru_cache`d, and `tests/conftest.py`,
`tests/test_execution.py`, `tests/test_schema_tool.py` and `tests/test_tools.py`
all call `get_engine.cache_clear()` mid-session. Each clear means the next
`get_engine()` returns a *different* `Engine` object carrying no listener, so an
instance-level attachment goes quietly blind partway through the run and reports
zeros for everything after. SQLAlchemy's class-level registration covers every
instance, including ones constructed later, which also catches `conftest`'s
throwaway probe engine.

`tests/test_request_round_trips.py::counting_round_trips` is the working
precedent this generalises; it attaches per-instance because it lives inside one
test and never sees a `cache_clear()`.

**Counting `create_engine` calls would measure the wrong thing.** Not only
because of the cache: `create_engine` does not connect. SQLAlchemy's pool is
lazy, and `create_engine` against a dead host succeeds. A call count measures
*intent to be able to connect*, which is a different question from *did traffic
happen*.
"""

from __future__ import annotations

import json
import pathlib
import platform
import sys
from datetime import datetime, timezone

import pytest
import sqlalchemy
from sqlalchemy.engine import Engine

#: The fixture whose reach defines a declared database dependency.
DATABASE_FIXTURE = "configured_database"

#: The marker Iteration 11 introduces. Nothing carries it when the baseline
#: census runs, which is the point: the first census measures the tree before
#: the partition exists, so `marked` is uniformly false and every disagreement
#: it reports is about the closure alone.
NEEDS_DB_MARKER = "needs_db"

#: Statements kept per test, for reading the census by eye. Three is enough to
#: recognise a pattern (`BEGIN` / `SET TRANSACTION READ ONLY` / the query) and
#: small enough that the artifact stays reviewable in a diff.
SAMPLES_PER_TEST = 3

#: Longest sample statement retained, in characters.
SAMPLE_WIDTH = 140

#: Traffic that arrives outside any test: import time, or session teardown after
#: the final item. Not a placeholder -- anything landing here is a finding.
OUTSIDE_ANY_TEST = "<outside any test>"

#: Nodeids that must resolve to a closure containing `configured_database`, one
#: per distinct code path through `requested_fixture_closure`.
#:
#: This walk reads `item._fixtureinfo`, a private attribute. If pytest changes
#: its shape the walk returns empty sets, every disagreement disappears, and the
#: census reports a perfectly clean suite -- the loudest possible way for an
#: instrument to be wrong. These make that impossible to miss.
#:
#: The three cover: a direct parameter, a transitive reach through another
#: fixture, and a module-level `usefixtures` marker. `initialnames` would pass
#: the first two and is still wrong, because it folds autouse names in and would
#: therefore report the entire suite as declaring a database.
CLOSURE_LANDMARKS = {
    "tests/test_validator_gates.py": "transitive, via the module's `engine` fixture",
    "tests/test_expert_tier.py": "module-level `pytestmark = usefixtures(...)`",
    "tests/test_tools.py": "direct parameter on the test function",
}

#: An autouse fixture in `tests/conftest.py` that **no test requests by name**
#: (verified: zero references outside its own definition).
#:
#: It is the positive control for the walk's one hard requirement — that autouse
#: fixtures are excluded. Before the partition, that property could be inferred
#: from `configured_database` itself: `initialnames` would report every item as
#: declaring a database, and a count told you so. After the partition
#: `configured_database` is no longer autouse and that signal is gone, so
#: swapping `argnames` for `initialnames` would once again fold autouse in and
#: nothing would notice.
#:
#: This name would appear in every closure under `initialnames` and appears in
#: none under `argnames`, which makes the distinction observable no matter what
#: else changes.
AUTOUSE_SENTINEL = "isolated_spend_ledger"


def requested_fixture_closure(item: pytest.Item) -> set[str]:
    """Fixtures this item reaches by *asking*, transitively. Autouse excluded.

    `FuncFixtureInfo.initialnames` is the attribute that looks right and is not:
    it includes autouse fixtures, and while `configured_database` is autouse that
    makes the answer "yes" for all 1,348 items. The pair that excludes autouse is
    `argnames` (the test function's own parameters) plus the arguments of any
    `usefixtures` marker, walked transitively.
    """
    info = getattr(item, "_fixtureinfo", None)
    if info is None:  # doctest items and other non-Function nodes
        return set()

    pending = list(info.argnames)
    for marker in item.iter_markers("usefixtures"):
        pending.extend(marker.args)

    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for fixturedef in info.name2fixturedefs.get(name, ()):
            pending.extend(fixturedef.argnames)
    return seen


def verdict_for(*, traffic: int, closure: bool) -> str:
    """Name the four combinations of measured traffic and declared dependency.

    `traffic_without_closure` is the one the iteration exists to eliminate: the
    test reaches Postgres and says nothing about it, so it works only because
    something ambient -- an autouse fixture, or a DSN left in the environment --
    supplied a database it never asked for.

    `closure_without_traffic` is not a defect. A test that monkeypatches over a
    real engine to simulate an unreachable host declares the dependency and then
    deliberately makes no traffic.
    """
    if traffic and closure:
        return "declared"
    if traffic:
        return "traffic_without_closure"
    if closure:
        return "closure_without_traffic"
    return "hermetic"


class _Census:
    """Accumulates per-test statement counts, keyed by nodeid and phase."""

    def __init__(self) -> None:
        self.nodeid: str | None = None
        self.phase: str | None = None
        self.statements: dict[str, dict[str, int]] = {}
        self.samples: dict[str, list[str]] = {}
        self.closure: dict[str, bool] = {}
        self.marked: dict[str, bool] = {}
        self.fixturenames: dict[str, bool] = {}
        self.autouse_leaks: list[str] = []
        self.collected: list[str] = []

    def record(self, statement: str) -> None:
        nodeid = self.nodeid or OUTSIDE_ANY_TEST
        phase = self.phase or "call"
        bucket = self.statements.setdefault(
            nodeid, {"setup": 0, "call": 0, "teardown": 0}
        )
        bucket[phase] += 1
        samples = self.samples.setdefault(nodeid, [])
        if len(samples) < SAMPLES_PER_TEST:
            samples.append(" ".join(str(statement).split())[:SAMPLE_WIDTH])

    def traffic(self, nodeid: str) -> int:
        return sum(self.statements.get(nodeid, {}).values())


_CENSUS = _Census()


def _record(conn, cursor, statement, parameters, context, executemany) -> None:
    _CENSUS.record(statement)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--db-access-out",
        action="store",
        default=".pytest_cache/db_access.json",
        help="Where to write the database-access census (JSON).",
    )


def pytest_configure(config: pytest.Config) -> None:
    sqlalchemy.event.listen(Engine, "before_cursor_execute", _record)


def pytest_unconfigure(config: pytest.Config) -> None:
    try:
        sqlalchemy.event.remove(Engine, "before_cursor_execute", _record)
    except Exception:  # pragma: no cover - teardown must not mask a real failure
        pass


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Record the declared dependency for every collected item.

    Deliberately does not *modify* anything -- the hook is the only place with
    the full item list and their fixture info, and the census must cover tests
    that are later skipped or deselected as well as ones that run.
    """
    for item in items:
        closure = requested_fixture_closure(item)
        _CENSUS.collected.append(item.nodeid)
        _CENSUS.closure[item.nodeid] = DATABASE_FIXTURE in closure
        _CENSUS.marked[item.nodeid] = (
            item.get_closest_marker(NEEDS_DB_MARKER) is not None
        )
        _CENSUS.fixturenames[item.nodeid] = DATABASE_FIXTURE in getattr(
            item, "fixturenames", ()
        )
        if AUTOUSE_SENTINEL in closure:
            _CENSUS.autouse_leaks.append(item.nodeid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    _CENSUS.nodeid = item.nodeid
    yield
    _CENSUS.nodeid = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item: pytest.Item):
    _CENSUS.phase = "setup"
    yield
    _CENSUS.phase = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    _CENSUS.phase = "call"
    yield
    _CENSUS.phase = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None):
    _CENSUS.phase = "teardown"
    yield
    _CENSUS.phase = None


def _landmark_report() -> dict[str, dict[str, object]]:
    """Whether each landmark file produced at least one declared closure.

    Reported per file rather than per nodeid so that renaming a single test does
    not silently disarm the guard -- the file has to lose its database
    declaration entirely before a landmark goes false, and that is exactly the
    event worth noticing.
    """
    report: dict[str, dict[str, object]] = {}
    for path, why in CLOSURE_LANDMARKS.items():
        hits = sum(
            1
            for nodeid, declared in _CENSUS.closure.items()
            if declared and nodeid.startswith(path)
        )
        report[path] = {"why": why, "declared_items": hits, "found": hits > 0}
    return report


def _build_report() -> dict[str, object]:
    tests: dict[str, dict[str, object]] = {}
    by_file: dict[str, dict[str, int]] = {}
    totals = {
        "items": 0,
        "traffic": 0,
        "closure": 0,
        "marked": 0,
        "hermetic": 0,
        "declared": 0,
        "traffic_without_closure": 0,
        "closure_without_traffic": 0,
        "statements": 0,
    }

    for nodeid in _CENSUS.collected:
        statements = _CENSUS.statements.get(
            nodeid, {"setup": 0, "call": 0, "teardown": 0}
        )
        traffic = sum(statements.values())
        closure = _CENSUS.closure.get(nodeid, False)
        marked = _CENSUS.marked.get(nodeid, False)
        verdict = verdict_for(traffic=traffic, closure=closure)

        entry: dict[str, object] = {
            "statements": statements,
            "closure": closure,
            "marked": marked,
            "verdict": verdict,
        }
        samples = _CENSUS.samples.get(nodeid)
        if samples:
            entry["samples"] = samples
        tests[nodeid] = entry

        path = nodeid.split("::", 1)[0]
        rollup = by_file.setdefault(
            path,
            {
                "items": 0,
                "traffic": 0,
                "closure": 0,
                "marked": 0,
                "hermetic": 0,
                "statements": 0,
            },
        )
        rollup["items"] += 1
        rollup["statements"] += traffic
        rollup["traffic"] += 1 if traffic else 0
        rollup["closure"] += 1 if closure else 0
        rollup["marked"] += 1 if marked else 0
        rollup["hermetic"] += 1 if verdict == "hermetic" else 0

        totals["items"] += 1
        totals["statements"] += traffic
        totals["traffic"] += 1 if traffic else 0
        totals["closure"] += 1 if closure else 0
        totals["marked"] += 1 if marked else 0
        totals[verdict] += 1

    # Traffic attributed to no test at all: import-time statements, or session
    # teardown running after the last item. Reported separately rather than
    # folded into a test's count, because charging it to a test is the specific
    # misreading this plugin's phase split exists to prevent.
    outside = _CENSUS.statements.get(OUTSIDE_ANY_TEST)

    walk_agrees = all(
        _CENSUS.closure[nodeid] == _CENSUS.fixturenames[nodeid]
        for nodeid in _CENSUS.collected
    )

    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "pytest": pytest.__version__,
        "sqlalchemy": sqlalchemy.__version__,
        "argv": sys.argv[1:],
        # True once `configured_database` stops being autouse: the private walk
        # and pytest's public `item.fixturenames` then agree, and the permanent
        # guard T5 installs can use the public one and leave this module's
        # private-API walk behind in tools/.
        "closure_walk_agrees_with_fixturenames": walk_agrees,
        "closure_landmarks": _landmark_report(),
        # Must be 0. Non-zero means the walk is folding autouse fixtures in,
        # which makes every item look like it declared whatever conftest
        # supplies and turns the whole census into noise.
        "autouse_sentinel_leaks": len(_CENSUS.autouse_leaks),
        "totals": totals,
        "outside_any_test": outside,
        "by_file": dict(sorted(by_file.items())),
        "tests": tests,
    }


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    destination = pathlib.Path(session.config.getoption("--db-access-out"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = _build_report()
    # newline="\n" because the repository is LF throughout and Python would
    # otherwise translate on Windows, rewriting the whole artifact on every run.
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, indent=2, sort_keys=False)
        handle.write("\n")
    session.config._db_access_report = report


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    report = getattr(config, "_db_access_report", None)
    if report is None:
        return

    totals = report["totals"]
    write = terminalreporter.write_line
    write("")
    write("database access census")
    write(f"  {'file':<44} {'items':>6} {'traffic':>8} {'closure':>8} {'hermetic':>9}")
    for path, row in report["by_file"].items():
        write(
            f"  {path:<44} {row['items']:>6} {row['traffic']:>8} "
            f"{row['closure']:>8} {row['hermetic']:>9}"
        )
    write(
        f"  {'TOTAL':<44} {totals['items']:>6} {totals['traffic']:>8} "
        f"{totals['closure']:>8} {totals['hermetic']:>9}"
    )
    write(
        f"  verdicts: declared={totals['declared']} "
        f"traffic_without_closure={totals['traffic_without_closure']} "
        f"closure_without_traffic={totals['closure_without_traffic']} "
        f"hermetic={totals['hermetic']}"
    )
    landmarks = report["closure_landmarks"]
    missing = [path for path, row in landmarks.items() if not row["found"]]
    if missing:
        write(f"  CLOSURE WALK IS VACUOUS -- landmarks not found: {missing}")
    outside = report.get("outside_any_test")
    if outside:
        write(f"  statements outside any test: {outside}")
    write(f"  written to {config.getoption('--db-access-out')}")
