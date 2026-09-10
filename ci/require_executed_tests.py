#!/usr/bin/env python3
"""Fail a pipeline that reported success without running the tests.

**AC2's second belt.** The first is `tests/conftest.py`'s
``QUERYPILOT_TESTS_REQUIRE_DATABASE`` gate, which turns an unreachable database
from a skip into a failure. That flag fixes the root cause and it is *one line
somebody can delete* — in a workflow file, during an unrelated change, with a
green build to reassure them. This script is the smoke alarm behind it: it does
not care *why* nothing ran, only that something did.

The number it defends against is measured, not imagined (`011-ship.md` §2.1):

    $ TEST_DATABASE_URL=<unreachable> pytest -q
    1109 skipped, 6 warnings in 5.26s
    $ echo $?
    0

Read from ``--junitxml`` rather than from stdout (resolved D-1). Parsing
``"1109 skipped"`` out of terminal text means a test-summary format change
silently disables the guard, and a guard that fails open is worse than none.

Usage:

    python ci/require_executed_tests.py <report.xml> [floor]

Exit 0 when at least ``floor`` tests actually executed; exit 1 otherwise, and
on a missing or unparseable report — **a run that produced no report is not
evidence of a run.**
"""

from __future__ import annotations

import pathlib
import sys
import xml.etree.ElementTree as ElementTree

#: Resolved D-1: a round number well below the real count (1,138 measured at
#: Iteration 8 T4, of which CI executes all but the three live provider
#: tests), not the count itself.
#:
#: A floor equal to the current total fails on the first test anybody adds,
#: which teaches people to raise the floor without reading why it exists —
#: and a guard that is routinely edited to make a build pass has stopped being
#: a guard. This one only ever moves when the suite loses a hundred tests,
#: which is a thing worth stopping for.
DEFAULT_FLOOR = 1000


class ReportProblem(Exception):
    """The report cannot be trusted to say how many tests ran."""


def read_counts(report_path: pathlib.Path) -> tuple[int, int]:
    """Return ``(tests, skipped)`` summed over every testsuite in the report.

    Summed rather than read off the root, because pytest 8 wraps its single
    ``<testsuite>`` in a ``<testsuites>`` element and older versions did not.
    ``iter`` covers both: it yields the root itself when the root is the
    testsuite. Getting that wrong in the *lenient* direction would read zeroes
    off the wrapper and fail every build; in the strict direction it would read
    nothing and pass every build. Neither is acceptable, so both shapes are
    handled explicitly.
    """
    if not report_path.exists():
        raise ReportProblem(
            f"no report at {report_path}. pytest did not get far enough to "
            f"write one, which is not evidence that the suite ran."
        )

    try:
        root = ElementTree.parse(report_path).getroot()
    except ElementTree.ParseError as exc:
        raise ReportProblem(f"{report_path} is not parseable XML: {exc}") from exc

    suites = list(root.iter("testsuite"))
    if not suites:
        raise ReportProblem(
            f"{report_path} contains no <testsuite> element; found root "
            f"<{root.tag}>. This is not a pytest junit report."
        )

    tests = 0
    skipped = 0
    for suite in suites:
        try:
            tests += int(suite.get("tests", "0"))
            skipped += int(suite.get("skipped", "0"))
        except ValueError as exc:
            raise ReportProblem(
                f"{report_path} has a non-numeric count attribute: {exc}"
            ) from exc

    return tests, skipped


def main(argv: list[str]) -> int:
    if not argv:
        print(
            f"usage: {sys.argv[0]} <junit-report.xml> [floor]",
            file=sys.stderr,
        )
        return 2

    report_path = pathlib.Path(argv[0])
    floor = int(argv[1]) if len(argv) > 1 else DEFAULT_FLOOR

    try:
        tests, skipped = read_counts(report_path)
    except ReportProblem as problem:
        print(f"FAIL: {problem}", file=sys.stderr)
        return 1

    executed = tests - skipped
    print(f"collected={tests} skipped={skipped} executed={executed} floor={floor}")

    if executed < floor:
        print(
            f"FAIL: only {executed} of {tests} tests actually executed, "
            f"below the floor of {floor}. A pipeline that skips the suite must "
            f"not report success -- see 011-ship.md AC2. The usual cause is an "
            f"unreachable database, in which case "
            f"QUERYPILOT_TESTS_REQUIRE_DATABASE=1 should have failed the run "
            f"before this did; check whether it is still set.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    sys.exit(main(sys.argv[1:]))
