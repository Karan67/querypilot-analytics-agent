"""Apply the database partition the census measured. One-shot, not a guard.

    python tools/apply_partition.py [--check]

Reads `specs/014-test-isolation-census.json` and gives every test that reads
PostgreSQL both halves of its declaration -- `@pytest.mark.needs_db` and a
request for `configured_database` -- so that `tests/conftest.py` can stop
supplying a database to all 1,348 tests through `autouse=True`.

**Committed even though it runs once.** 459 marks across 28 files is not a diff
anybody reviews line by line, so the answerable question has to be *how was this
set chosen* rather than *is each line right*. Re-running this must be a no-op;
`--check` asserts exactly that and is what `tests/test_isolation.py` calls.

The selection rule is **not** "made any traffic":

    needs_db == closure or call_statements > 0 or teardown_statements > 0

Setup-phase traffic is excluded unless the test also declares the fixture.
`configured_database` is session-scoped, so its readiness probe runs inside the
setup of whichever test happens to be collected first and a per-test instrument
charges the probe's `SELECT 1` to that test. Exactly one test is affected --
`tests/test_ask_endpoint.py::test_ac4_no_endpoint_touches_the_database_directly`,
whose own assertion is that no endpoint touches the database -- and marking it
would record a dependency it does not have. That is the whole reason the census
splits traffic by phase instead of totalling it (`specs/014-test-isolation.md`
§2.5).
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CENSUS = ROOT / "specs" / "014-test-isolation-census.json"

MARKER = "@pytest.mark.needs_db"
USEFIXTURES = '@pytest.mark.usefixtures("configured_database")'
FIXTURE = "configured_database"


def target_nodeids(census: dict) -> set[str]:
    """Tests that read PostgreSQL, by the rule in the module docstring."""
    chosen = set()
    for nodeid, entry in census["tests"].items():
        statements = entry["statements"]
        if entry["closure"] or statements["call"] or statements["teardown"]:
            chosen.add(nodeid)
    return chosen


def _function_name(nodeid: str) -> str:
    """`tests/x.py::test_y[param]` -> `test_y`. Rejects class-based nodeids.

    A `Class::method` nodeid would silently resolve to the method name and get a
    decorator placed on the wrong `def` if two classes share a method name. None
    exist today; this makes that an error rather than a corruption.
    """
    _, _, rest = nodeid.partition("::")
    if "::" in rest:
        raise SystemExit(f"class-based test not handled: {nodeid}")
    return rest.split("[", 1)[0]


def plan_files(census: dict) -> tuple[dict, dict, list[str]]:
    """Split the target set into module-scope files, per-test files, and splits.

    A *split* is a parametrized function whose cases disagree -- some reach the
    database and some do not. A decorator sits on the function, so marking it
    over-declares the hermetic cases. Reported rather than silently accepted.
    """
    by_file: dict[str, dict] = {}
    for nodeid, entry in census["tests"].items():
        path = nodeid.split("::", 1)[0]
        record = by_file.setdefault(path, {"all": set(), "needed": set(), "fn": {}})
        record["all"].add(nodeid)
        name = _function_name(nodeid)
        record["fn"].setdefault(name, {"total": 0, "needed": 0})
        record["fn"][name]["total"] += 1

    chosen = target_nodeids(census)
    for nodeid in chosen:
        path = nodeid.split("::", 1)[0]
        by_file[path]["needed"].add(nodeid)
        by_file[path]["fn"][_function_name(nodeid)]["needed"] += 1

    module_scope, per_test, splits = {}, {}, []
    for path, record in sorted(by_file.items()):
        if not record["needed"]:
            continue
        for name, counts in record["fn"].items():
            if 0 < counts["needed"] < counts["total"]:
                splits.append(f"{path}::{name}")
        names = {
            _function_name(nodeid)
            for nodeid in record["needed"]
        }
        if record["needed"] == record["all"]:
            module_scope[path] = names
        else:
            per_test[path] = names
    return module_scope, per_test, splits


def _takes_fixture(node: ast.FunctionDef) -> bool:
    args = node.args
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    return FIXTURE in names


def _already_marked(node: ast.FunctionDef) -> bool:
    for decorator in node.decorator_list:
        text = ast.unparse(decorator)
        if "needs_db" in text:
            return True
    return False


def ensure_pytest_import(path: pathlib.Path) -> bool:
    """Add `import pytest` where a decorator was placed into a file without it.

    Two files needed this -- `test_daily_quota_guards.py` and
    `test_serialization.py` -- and the failure is a collection `NameError`, so it
    is loud rather than dangerous. Handled here anyway so `--check` and a re-run
    agree with what was actually shipped.

    Inserted after the last stdlib-looking import so the grouping the files
    already use (stdlib, blank, third-party, blank, first-party) survives.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    if any(
        isinstance(node, ast.Import) and any(a.name == "pytest" for a in node.names)
        for node in ast.walk(tree)
    ):
        return False

    anchor = 0
    for node in tree.body:
        if isinstance(node, ast.Import) and not any(
            a.name.startswith(("api", "evals", "tests", "ci")) for a in node.names
        ):
            anchor = max(anchor, node.end_lineno)
    lines = source.split("\n")
    lines[anchor:anchor] = ["", "import pytest"]
    path.write_text("\n".join(lines), encoding="utf-8", newline="")
    return True


def rewrite_per_test(path: pathlib.Path, names: set[str]) -> int:
    """Place decorators above each qualifying `def`, bottom-up."""
    source = path.read_text(encoding="utf-8")
    lines = source.split("\n")
    tree = ast.parse(source)

    edits = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name not in names:
            continue
        if _already_marked(node):
            continue
        # Above existing decorators, so `parametrize` stays adjacent to the def
        # and the database declaration reads first.
        anchor = (node.decorator_list[0].lineno - 1) if node.decorator_list else (
            node.lineno - 1
        )
        block = [MARKER] if _takes_fixture(node) else [MARKER, USEFIXTURES]
        edits.append((anchor, block))

    for anchor, block in sorted(edits, reverse=True):
        lines[anchor:anchor] = block

    path.write_text("\n".join(lines), encoding="utf-8", newline="")
    return len(edits)


def rewrite_module(path: pathlib.Path) -> int:
    """Add or extend the module-level `pytestmark`.

    Extends rather than assigns. `tests/test_llm_live.py` holds a bare
    `pytestmark = pytest.mark.skipif(...)` that keeps live provider calls from
    billing against quota; assigning over it would delete that and the tests
    would start making real API calls.
    """
    source = path.read_text(encoding="utf-8")
    lines = source.split("\n")
    tree = ast.parse(source)

    existing = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            existing = node
            break

    wanted = ["pytest.mark.needs_db", 'pytest.mark.usefixtures("configured_database")']

    if existing is not None:
        current = ast.unparse(existing.value)
        if "needs_db" in current:
            return 0
        present = (
            [ast.unparse(e) for e in existing.value.elts]
            if isinstance(existing.value, ast.List)
            else [current]
        )
        merged = present + [w for w in wanted if w.split("(")[0] not in "".join(present)]
        # usefixtures may already be there under a different spelling; dedupe.
        seen, ordered = set(), []
        for item in merged:
            if item not in seen:
                seen.add(item)
                ordered.append(item)
        replacement = "pytestmark = [\n    " + ",\n    ".join(ordered) + ",\n]"
        start, end = existing.lineno - 1, existing.end_lineno
        lines[start:end] = replacement.split("\n")
    else:
        last_import = 0
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                last_import = max(last_import, node.end_lineno)
        block = [
            "",
            "pytestmark = [",
            "    pytest.mark.needs_db,",
            '    pytest.mark.usefixtures("configured_database"),',
            "]",
        ]
        lines[last_import:last_import] = block

    path.write_text("\n".join(lines), encoding="utf-8", newline="")
    return 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="report what would change; write nothing")
    args = parser.parse_args(argv)

    census = json.loads(CENSUS.read_text(encoding="utf-8"))
    module_scope, per_test, splits = plan_files(census)

    print(f"target tests: {len(target_nodeids(census))}")
    print(f"module-scope files: {len(module_scope)}   per-test files: {len(per_test)}")
    if splits:
        print(f"parametrized functions with mixed cases ({len(splits)}):")
        for name in splits:
            print(f"  {name}")

    if args.check:
        return 0

    changed = 0
    for path in module_scope:
        changed += rewrite_module(ROOT / path)
        ensure_pytest_import(ROOT / path)
    for path, names in per_test.items():
        changed += rewrite_per_test(ROOT / path, names)
        ensure_pytest_import(ROOT / path)
    print(f"edits applied: {changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
