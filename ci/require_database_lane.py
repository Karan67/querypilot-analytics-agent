#!/usr/bin/env python3
"""Fail a pipeline whose database tests all quietly stopped being database tests.

**The hole Iteration 11 opened, and the belt that closes it.**

`ci/require_executed_tests.py` defends "the database died and nobody noticed" by
counting tests that actually executed. That worked because an unreachable
database used to skip **everything**: `011-ship.md` §2.1 measured 1,109 skipped
and exit 0, and a floor of 1,000 catches it.

The partition changed what a dead database costs. `configured_database` is opt-in
now, so only the `needs_db` lane skips and the hermetic lane — 923 tests —
executes and passes. That is the entire point of B-15, and it means the total is
no longer evidence about the database at all:

    markers all dropped  ->  database lane = 0,  total executed = 923,
                             floor of 1,000 ... still fails, today.

Today. The hermetic lane grows with every unit test anybody writes, and the day
it passes 1,000 a suite with zero database coverage becomes a green build. **A
total cannot detect an empty lane.** This counts the lane.

Usage:

    python -m pytest --collect-only -q -p tools.measure_db_access
        --db-access-out=lane.json
    python ci/require_database_lane.py lane.json [floor]

**Reads the census, not a junit report.** Two reasons. A `--collect-only` run
writes a junit report containing zero testcases — measured, `read_counts` returns
`(0, 0)` — so a floor read from one would fire on every build for the wrong
reason. And running the lane a second time just to count it would re-execute ~460
database tests the suite already ran. The census is structured, it is produced by
collection alone, and it records the marker directly.

**Collect the whole suite, not `-m needs_db`.** The census counts markers among
the items it saw, so collecting only the marked lane would make `marked` equal
`collected` and the count would say nothing about what was left out.

Exit 0 when at least ``floor`` tests carry `needs_db`; exit 1 otherwise, and on a
missing or unparseable census — a run that produced no census is not evidence of
a run.
"""

from __future__ import annotations

import json
import pathlib
import sys

#: A round number well below the 460 measured at Iteration 11 T4.
#:
#: Deliberately not the real count. A floor equal to the current total fails on
#: the first test anybody adds, which teaches people to edit the floor rather
#: than read it — and a guard routinely edited to make a build pass has stopped
#: being a guard. This one only moves when the database lane loses a fifth of
#: itself, which is worth stopping for.
#:
#: `tests/test_ci_guards.py::test_the_database_lane_has_not_silently_emptied`
#: checks the real count against this, so the two cannot drift apart unnoticed.
DEFAULT_DATABASE_LANE_FLOOR = 375


def read_lane(census_path: pathlib.Path) -> tuple[int, int]:
    """Return ``(marked, collected)`` from a census written by the plugin."""
    census = json.loads(census_path.read_text(encoding="utf-8"))
    totals = census["totals"]
    return totals["marked"], totals["items"]


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: require_database_lane.py <census.json> [floor]", file=sys.stderr)
        return 1

    census_path = pathlib.Path(argv[0])
    floor = int(argv[1]) if len(argv) > 1 else DEFAULT_DATABASE_LANE_FLOOR

    try:
        marked, collected = read_lane(census_path)
    except (OSError, ValueError, KeyError) as exc:
        print(f"could not read {census_path}: {exc}", file=sys.stderr)
        return 1

    if collected < 1000:
        print(
            f"the census collected only {collected} tests, so its marker count "
            "is not evidence about the suite.",
            file=sys.stderr,
        )
        return 1

    if marked < floor:
        print(
            f"only {marked} of {collected} tests carry `needs_db`, below the "
            f"floor of {floor}. The markers have been lost, and the hermetic "
            "lane will keep the build green with no database coverage at all.",
            file=sys.stderr,
        )
        return 1

    print(f"database lane: {marked} of {collected} collected (floor {floor})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
