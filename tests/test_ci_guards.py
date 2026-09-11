"""The pipeline's own guards (Iteration 8 T4, `011-ship.md` AC1-AC5).

**This file exists because AC2 is the criterion most likely to be satisfied by
a comment.** The plan said so in advance (section 8): *"Until that has been
seen, the criterion is unmet."* A workflow file asserts nothing about itself,
and "we set a flag so CI fails on a missing database" is a sentence, not a
mechanism.

Three groups, in increasing order of how much they prove:

1. **The floor script**, driven with synthetic reports - including the exact
   counts `011-ship.md` section 2.1 measured (1,109 collected, 1,109 skipped,
   exit 0).
2. **The workflow's structure**, read from the *parsed* YAML. Comments are
   discarded by the parser, which is this repository's standing remedy for its
   single most repeated bug: a test asserting a string is absent that matches
   the comment promising to keep it absent. It has appeared five times in five
   costumes - twice a Python structural test matched its own docstring, then an
   AC13 test matched the HTML comment explaining AC13, then an `innerHTML` test
   matched the JS comment promising not to use `innerHTML`, then an AC13 test
   matched rendered page copy. This file's absence assertion - that no `secrets`
   context is referenced - walks parsed values, so the long comment in ci.yml
   explaining the no-secret rule is invisible to it by construction. The
   vacuity guard below is what proves the walk is not simply finding nothing.
3. **The require-database gate, end to end**, by running pytest in a subprocess
   against a dead DSN with and without the flag. That is T4's mandated mutation
   turned into a permanent test rather than something someone once watched
   happen in a terminal.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
FLOOR_SCRIPT_PATH = REPO_ROOT / "ci" / "require_executed_tests.py"
DOCKERFILE_PATH = REPO_ROOT / "api" / "Dockerfile"

#: A DSN nothing can be listening on. Port 1 is privileged and unassigned.
#:
#: **It does not fail fast, and that was measured rather than assumed.** The
#: expectation when this was written was an immediate refusal; what actually
#: comes back is ``psycopg.errors.ConnectionTimeout`` after the full
#: ``PROBE_CONNECT_TIMEOUT_SECONDS``, because this platform drops the packet
#: instead of rejecting it - the same behaviour that fixture's timeout was
#: added for. So each of the two subprocess runs below costs about 3.3s, and
#: this file runs in ~9s rather than the ~2s a refusal would have given.
#:
#: That is worth paying and worth knowing: it also means the two runs exercise
#: the timeout path, not just the refusal path.
#:
#: This does not violate the "no test hardcodes a DSN" rule. That rule exists so
#: CI can retarget the suite with one variable; this is a fixed non-address
#: whose whole purpose is to be unreachable, and pointing it at
#: ``TEST_DATABASE_URL`` would defeat the test.
DEAD_DSN = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/nowhere"

#: One real database-dependent test, used as the subject of the subprocess runs.
#: A single test rather than a whole file because the runs cost real seconds,
#: and *some* test must be selected: the fixture under test is session-scoped
#: and autouse, so a ``-k`` expression matching nothing would never reach it and
#: pytest would exit 5 for a reason unrelated to the claim.
GATE_SUBJECT = "tests/test_schema_tool.py::test_returns_every_base_table"


# ---------------------------------------------------------------------------
# 1. The floor script
# ---------------------------------------------------------------------------


def junit(tests: int, skipped: int, *, wrapped: bool = True) -> str:
    """A minimal junit report carrying the two counts the guard reads."""
    suite = (
        f'<testsuite name="pytest" tests="{tests}" errors="0" failures="0" '
        f'skipped="{skipped}"></testsuite>'
    )
    return f"<testsuites>{suite}</testsuites>" if wrapped else suite


def run_floor_guard(tmp_path, report: str | None, floor: int = 1000) -> int:
    """Invoke the guard's ``main`` and return its exit code.

    ``None`` writes no file at all, which is the missing-report case.
    """
    from ci.require_executed_tests import main

    path = tmp_path / "junit.xml"
    if report is not None:
        path.write_text(report, encoding="utf-8")
    return main([str(path), str(floor)])


def test_a_run_that_executed_the_suite_clears_the_floor(tmp_path):
    assert run_floor_guard(tmp_path, junit(tests=1108, skipped=3)) == 0


def test_the_measured_empty_run_is_a_failure(tmp_path):
    """The exact scenario `011-ship.md` section 2.1 measured.

    1,109 collected, 1,109 skipped, and pytest exiting 0. Before this guard
    that was a green build; the numbers are copied from the measurement rather
    than invented so that the test reads as the regression it is.
    """
    assert run_floor_guard(tmp_path, junit(tests=1109, skipped=1109)) == 1


def test_a_partially_skipped_run_below_the_floor_is_a_failure(tmp_path):
    """Not only the all-skipped case.

    A pipeline that lost the database halfway, or one where a marker
    accidentally deselected most of the suite, reports a mixture. The guard
    counts what executed, so a mixture is caught on the same arithmetic.
    """
    assert run_floor_guard(tmp_path, junit(tests=1108, skipped=200)) == 1


def test_the_floor_is_a_floor_and_not_a_gap(tmp_path):
    """Exactly at the floor passes; one below fails.

    The boundary is asserted because an off-by-one here is invisible: both
    versions pass every real run and differ only on the day the suite shrinks
    to precisely the floor.
    """
    assert run_floor_guard(tmp_path, junit(tests=1000, skipped=0), floor=1000) == 0
    assert run_floor_guard(tmp_path, junit(tests=1000, skipped=1), floor=1000) == 1


def test_an_error_is_not_a_skip(tmp_path):
    """A run the require-database gate failed still clears the floor, and that
    is correct.

    Worth asserting because it draws the line between the two belts. When the
    flag fires, every test *errors* - it does not skip - so this guard sees 1,108
    executed and passes. The build is already red from pytest's own exit code by
    then. The guard is not a second opinion on the same signal; it is there for
    the case where the flag is *gone*, and then the report says 1,108 skipped
    and this is the only thing left that notices.
    """
    report = (
        '<testsuites><testsuite name="pytest" tests="1108" errors="1108" '
        'failures="0" skipped="0"></testsuite></testsuites>'
    )
    assert run_floor_guard(tmp_path, report) == 0


def test_a_missing_report_is_a_failure(tmp_path):
    """A run that produced no report is not evidence of a run.

    This is the failure mode of the guard itself: pytest crashing during
    collection writes no XML, and a guard that treated an absent file as
    "nothing to check" would wave that through - failing open on exactly the
    catastrophe it was installed for.
    """
    assert run_floor_guard(tmp_path, None) == 1


def test_an_unparseable_report_is_a_failure(tmp_path):
    assert run_floor_guard(tmp_path, "<testsuites><testsuite tests=") == 1


def test_a_report_that_is_not_a_pytest_report_is_a_failure(tmp_path):
    """Well-formed XML with no testsuite in it.

    Reads as pedantic and is not: ``iter("testsuite")`` returning nothing would
    leave the counts at zero, and zero executed against a floor of 1,000 would
    fail anyway. But it would fail with an arithmetic complaint about a suite
    that never ran, instead of saying the file is the wrong kind of file.
    """
    assert run_floor_guard(tmp_path, "<coverage><packages /></coverage>") == 1


def test_both_junit_layouts_are_read(tmp_path):
    """pytest 8 wraps its testsuite in ``<testsuites>``; older versions did not.

    Handling only the wrapper would read the counts off an element that has
    none - zeroes, failing every build. Handling only the bare form would find
    nothing under the wrapper, which fails safe here but would not if the guard
    ever grew a "no counts means fine" branch.
    """
    assert run_floor_guard(tmp_path, junit(1108, 3, wrapped=True)) == 0
    assert run_floor_guard(tmp_path, junit(1108, 3, wrapped=False)) == 0


def test_the_default_floor_matches_what_the_workflow_asks_for():
    """The script's default and the workflow's argument must not disagree.

    They are two statements of one number, and D-1 fixed it at 1,000. If the
    workflow's floor were raised and the default left behind, anyone running the
    guard by hand would be checking a different rule from the one the gate
    enforces.
    """
    from ci.require_executed_tests import DEFAULT_FLOOR

    floor_arguments = [
        int(token)
        for step in workflow_steps()
        if "require_executed_tests.py" in str(step.get("run", ""))
        for token in str(step["run"]).split()
        if token.isdigit()
    ]
    assert floor_arguments, "no step passes a floor to require_executed_tests.py"
    assert all(floor == DEFAULT_FLOOR for floor in floor_arguments), (
        f"the workflow asks for {floor_arguments} and the script defaults to "
        f"{DEFAULT_FLOOR}"
    )


# ---------------------------------------------------------------------------
# 2. The workflow's structure, read from parsed YAML
# ---------------------------------------------------------------------------


def workflow() -> dict:
    """The parsed workflow.

    Parsed, never read as text, for the reason in this module's docstring: the
    file's comments explain at length what it must not contain, and a textual
    search would find those explanations and pass.
    """
    assert WORKFLOW_PATH.exists(), f"no workflow at {WORKFLOW_PATH} (AC1)"
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def workflow_steps() -> list[dict]:
    return workflow()["jobs"]["suite"]["steps"]


def workflow_strings() -> list[str]:
    """Every string anywhere in the parsed workflow, keys included."""

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield from walk(key)
                yield from walk(value)
        elif isinstance(node, list):
            for item in node:
                yield from walk(item)
        elif isinstance(node, str):
            yield node

    return list(walk(workflow()))


def test_the_string_walk_is_not_vacuous():
    """The guard on the guard.

    Every absence assertion below is only as good as this walk. If
    ``workflow_strings()`` returned an empty list - a renamed job key, a parser
    that hands back something unexpected - each of those assertions would pass
    triumphantly while checking nothing. That is the exact shape of the
    over-matching-regex failure the AC13 tests hit, so the walk is required to
    find landmarks it must contain.
    """
    haystack = "\n".join(workflow_strings())
    for landmark in (
        "docker compose up",
        "actions/checkout@v4",
        "QUERYPILOT_TESTS_REQUIRE_DATABASE",
        "require_executed_tests.py",
        "pytest",
    ):
        assert landmark in haystack, f"the string walk lost {landmark!r}"


def test_the_gate_runs_on_pushes_and_pull_requests():
    """AC1.

    Note the key: YAML 1.1 reads a bare ``on`` as the boolean ``True``, so the
    trigger block arrives under ``True`` and not under ``"on"``. Looking it up
    by the string would find nothing and, without this comment, look like the
    workflow was missing its triggers.
    """
    triggers = workflow().get(True, workflow().get("on"))
    assert triggers is not None, "the workflow declares no triggers"
    assert "push" in triggers
    assert "pull_request" in triggers


def test_the_gate_runs_the_suite_against_a_real_stack():
    """AC1: a real Postgres with Chinook loaded, brought up the product's way."""
    runs = "\n".join(str(step.get("run", "")) for step in workflow_steps())
    assert "docker compose up" in runs, (
        "the gate must bring up db/init/ the way the README does, not a "
        "services: block that mounts none of it"
    )
    assert "fetch_chinook" in runs, "nothing loads Chinook"
    assert "-m pytest" in runs, "the gate does not run the suite"


def test_the_gate_fails_rather_than_skips_without_a_database():
    """AC2, belt one: the flag is set, and set to the value conftest accepts."""
    from tests.conftest import REQUIRE_DATABASE_ENV

    job_env = workflow()["jobs"]["suite"].get("env", {})
    assert job_env.get(REQUIRE_DATABASE_ENV) == "1", (
        f"{REQUIRE_DATABASE_ENV} must be '1' in the job environment; "
        f"conftest compares against exactly that string"
    )


def test_the_gate_asserts_a_floor_on_tests_actually_executed():
    """AC2, belt two: the floor guard runs, unconditionally.

    ``if: always()`` matters more than it looks. Without it a real test failure
    stops the job before the floor is read, and the one run where you most want
    to know how many tests executed is the run that never checks.
    """
    floor_steps = [
        step
        for step in workflow_steps()
        if "require_executed_tests.py" in str(step.get("run", ""))
    ]
    assert len(floor_steps) == 1, "exactly one step should assert the floor"
    assert floor_steps[0].get("if") == "always()", (
        "the floor must be checked even when the suite failed"
    )

    referenced = REPO_ROOT / "ci" / "require_executed_tests.py"
    assert referenced.exists(), (
        f"the workflow runs {referenced}, which is not in the tree"
    )

    junit_flag = [
        step for step in workflow_steps() if "--junitxml" in str(step.get("run", ""))
    ]
    assert junit_flag, (
        "the floor is read from a junit report and nothing asks pytest to "
        "write one"
    )


def test_the_gate_needs_no_provider_credential():
    """AC3, and the property that keeps the gate forkable.

    The absence assertion this module's docstring is about. Walked over parsed
    values, so ci.yml's own paragraph explaining why no secret may appear is not
    itself a match.
    """
    for value in workflow_strings():
        assert "secrets." not in value, (
            f"the merge gate must need no secret so forks run clean; found "
            f"{value.strip()[:80]!r}"
        )


def test_the_gate_excludes_the_live_provider_tests_by_construction():
    """AC3: excluded deliberately, not by a key happening to be absent.

    Leaving them to skip would work today and be silent tomorrow: a repository
    that later gained a GROQ_API_KEY secret for a release job would start
    spending tokens on every push, and nothing would say so.
    """
    runs = "\n".join(str(step.get("run", "")) for step in workflow_steps())
    assert "--ignore=tests/test_llm_live.py" in runs, (
        "tests/test_llm_live.py must be excluded explicitly (resolved Q-B)"
    )


def test_the_gate_pins_the_interpreter_the_image_ships():
    """AC4, checked by derivation rather than by repeating the number.

    `011-ship.md` section 2.5 is the whole reason: the host ran 3.14.6, the
    image 3.12, and nothing pinned either. Two statements of one version drift;
    this asserts they are the same statement.
    """
    setup_steps = [
        step
        for step in workflow_steps()
        if str(step.get("uses", "")).startswith("actions/setup-python")
    ]
    assert len(setup_steps) == 1, "exactly one step should pin the interpreter"
    pinned = str(setup_steps[0]["with"]["python-version"])

    from_lines = [
        line
        for line in DOCKERFILE_PATH.read_text(encoding="utf-8").splitlines()
        if line.startswith("FROM ")
    ]
    assert len(from_lines) == 1, "expected a single FROM in api/Dockerfile"
    image = from_lines[0].split()[1]
    image_version = image.removeprefix("python:").removesuffix("-slim")

    assert pinned == image_version, (
        f"CI pins Python {pinned} and the image ships {image_version}; "
        f"a skew surfaces as a confusing assertion failure rather than as the "
        f"version problem it is"
    )
    assert image_version.count(".") == 2, (
        f"the image tag {image!r} floats its patch version, so CI cannot pin "
        f"to it meaningfully"
    )


def test_scripts_the_pipeline_invokes_directly_are_executable():
    """**The first thing CI actually found, turned into a guard.**

    Run 1 of this workflow died at ``./db/fetch_chinook.sh: Permission denied``,
    exit 126. The script has a shebang, the README tells a developer to invoke it
    exactly that way, and its mode in the index was ``100644`` -- because
    ``core.filemode`` is ``false`` on the one machine this project has been
    developed on, so git has never recorded an executable bit for anything.
    Eight iterations of a green suite could not see it, and neither could a
    reviewer: the mode is not in the diff.

    That is precisely the failure the plan predicted in its section 8 -- *"an
    assumption about paths, line endings or a running Docker daemon that has
    been true for eight iterations because one machine made it true"* -- and a
    fresh Linux clone would have hit it too.

    Derived rather than listed: the invocations come out of the workflow, so
    adding ``./db/something.sh`` to a step brings it under the rule
    automatically. The mode is read from the **index**, not from the filesystem,
    because on Windows ``os.access(..., X_OK)`` answers a question about NTFS
    that has nothing to do with what a runner will see.

    ``db/init/*.sh`` are deliberately not covered. Nothing invokes them
    directly: Postgres's entrypoint sources a non-executable ``.sh`` and
    executes an executable one, so both modes work there, and changing them
    would change how init runs to fix a problem it does not have.
    """
    directly_invoked = sorted(
        {
            token.lstrip("./")
            for step in workflow_steps()
            for token in str(step.get("run", "")).split()
            if token.startswith("./")
        }
    )
    assert directly_invoked, (
        "no ./ invocation found in the workflow; if that is now true this test "
        "is vacuous and should be removed rather than left passing"
    )

    listed = subprocess.run(
        ["git", "ls-files", "-s", "--", *directly_invoked],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert listed.returncode == 0, f"git ls-files failed: {listed.stderr}"

    modes = {}
    for line in listed.stdout.splitlines():
        metadata, _, path = line.partition("\t")
        modes[path] = metadata.split()[0]

    for path in directly_invoked:
        assert path in modes, f"the workflow runs ./{path}, which git does not track"
        assert modes[path] == "100755", (
            f"./{path} is mode {modes[path]} in the index, so a runner gets "
            f"'Permission denied' (exit 126). Fix with: "
            f"git update-index --chmod=+x {path}"
        )


def test_the_gate_installs_from_the_lock():
    """AC5: identical resolution on every run.

    A lockfile nothing installs from is decoration. The negative half matters
    as much as the positive one - installing the *ranged* file here would look
    correct in review and quietly restore per-run resolution.
    """
    runs = "\n".join(str(step.get("run", "")) for step in workflow_steps())
    assert "api/requirements-dev.lock" in runs, "the gate does not use the lock"
    assert "-r api/requirements-dev.txt" not in runs, (
        "the gate installs the ranged file, which defeats the lock"
    )
    assert (REPO_ROOT / "api" / "requirements-dev.lock").exists()


# ---------------------------------------------------------------------------
# 3. The require-database gate, end to end
# ---------------------------------------------------------------------------


def test_only_the_exact_value_one_requires_a_database(monkeypatch):
    """``QUERYPILOT_TESTS_REQUIRE_DATABASE=0`` must not mean *required*.

    Added because the conftest docstring claimed this and nothing checked it.
    Loosening ``== "1"`` to a truthiness test passes every other test in this
    file - the CI arm sets ``"1"`` and the developer arm unsets the variable, so
    neither distinguishes the two implementations. The case that separates them
    is somebody switching the gate off the obvious way, and discovering during
    an incident that ``"0"`` turned it on.

    Unset and explicitly disabled must behave identically, so both are asserted.
    """
    from tests.conftest import REQUIRE_DATABASE_ENV, _database_is_required

    monkeypatch.setenv(REQUIRE_DATABASE_ENV, "1")
    assert _database_is_required() is True
    monkeypatch.setenv(REQUIRE_DATABASE_ENV, " 1 ")
    assert _database_is_required() is True, "a stray space should not disable it"

    for off in ("0", "", "true", "yes", "no", "off"):
        monkeypatch.setenv(REQUIRE_DATABASE_ENV, off)
        assert _database_is_required() is False, (
            f"{off!r} must not enable the gate; only '1' does"
        )

    monkeypatch.delenv(REQUIRE_DATABASE_ENV, raising=False)
    assert _database_is_required() is False


def run_pytest_against_a_dead_database(*, require: bool) -> subprocess.CompletedProcess:
    """Run one real database-dependent test against a DSN nothing answers.

    A subprocess because the fixture under test is session-scoped: it has
    already run for *this* session, and there is no way to make it run again
    in-process without reaching inside pytest. The cost is about a second per
    call, paid twice, and it buys the only assertion that actually exercises
    the branch CI depends on.
    """
    from tests.conftest import REQUIRE_DATABASE_ENV

    env = dict(os.environ)
    env["TEST_DATABASE_URL"] = DEAD_DSN
    if require:
        env[REQUIRE_DATABASE_ENV] = "1"
    else:
        # Popped rather than left alone: this arm asserts the *developer's*
        # experience, and it must assert it while running inside CI too, where
        # the variable is set for the whole job.
        env.pop(REQUIRE_DATABASE_ENV, None)

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            GATE_SUBJECT,
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_a_developer_with_the_stack_down_gets_a_skip():
    """The behaviour that must not change, asserted alongside the one that does.

    Half of this mutation is proving the gate fires. The other half is proving
    it fires *only* when asked - a gate that failed for everyone would be
    "fixed" within a day by deleting it, and then CI would be green and empty
    again with nobody the wiser.
    """
    completed = run_pytest_against_a_dead_database(require=False)

    assert completed.returncode == 0, (
        f"an unreachable database should skip for a developer, not fail:\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
    assert "skipped" in completed.stdout, completed.stdout
    assert "docker compose up -d" in completed.stdout, (
        "the skip reason must say what to do about it"
    )


def test_ci_gets_a_red_build_instead_of_an_empty_one():
    """**T4's mutation, as a test.**

    The plan required breaking the DSN in the workflow and watching the build go
    red. That was done once and is recorded in the T4 report - but a mutation
    someone watched happen in a terminal protects nothing afterwards. This runs
    it on every suite: dead DSN, flag set, and the run must fail.

    The exit code is the claim. The message is asserted too, because the whole
    point of failing rather than skipping is that somebody reads why - and
    "No database at the configured DSN" without the variable's name sends them
    looking for a broken test instead of a missing service.
    """
    from tests.conftest import REQUIRE_DATABASE_ENV

    completed = run_pytest_against_a_dead_database(require=True)
    combined = completed.stdout + completed.stderr

    assert completed.returncode != 0, (
        f"with {REQUIRE_DATABASE_ENV}=1 an unreachable database must fail the "
        f"run; this is exactly the green-and-empty build AC2 forbids:\n"
        f"{combined}"
    )
    assert REQUIRE_DATABASE_ENV in combined, (
        f"the failure must name the variable that caused it:\n{combined}"
    )
    assert " 1 skipped" not in completed.stdout, (
        f"the run skipped rather than failed:\n{completed.stdout}"
    )


# ---------------------------------------------------------------------------
# 4. Assumptions CI falsified, kept falsified
# ---------------------------------------------------------------------------


def undoers_in(source: str, filename: str) -> list[str]:
    """Names of tests in ``source`` that call ``undo()`` on the shared fixture.

    Split out from the repository scan so the *rule* can be tested on cases the
    repository does not contain. That is not tidiness: a mutation removing the
    name check below left the repository scan green, because nothing here
    currently takes `monkeypatch` and undoes a different object.
    """
    import ast

    found = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.FunctionDef):
            continue
        if "monkeypatch" not in {argument.arg for argument in node.args.args}:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "undo"
                and isinstance(inner.func.value, ast.Name)
                and inner.func.value.id == "monkeypatch"
            ):
                found.append(f"{filename}::{node.name}:{inner.lineno}")
    return found


def scan_for_shared_monkeypatch_undo() -> tuple[list[str], int]:
    """Run :func:`undoers_in` over the whole suite. Returns (offenders, seen)."""
    import ast

    offenders = []
    inspected = 0
    for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        offenders.extend(undoers_in(source, path.name))
        for node in ast.walk(ast.parse(source, filename=path.name)):
            if isinstance(node, ast.FunctionDef) and "monkeypatch" in {
                argument.arg for argument in node.args.args
            }:
                inspected += 1
    return offenders, inspected


def test_the_undo_scan_tells_a_private_monkeypatch_from_the_shared_one():
    """The discrimination the repository scan cannot demonstrate.

    Two snippets that differ only in *which object* is undone. The shared
    fixture's `undo()` reverts conftest's autouse isolation and must be flagged;
    a locally constructed `pytest.MonkeyPatch()` owns only its own patches and
    must not be. Both spellings contain the text ``.undo()``, which is why the
    scan reads the AST.
    """
    shared = (
        "def test_x(monkeypatch, tmp_path):\n"
        "    monkeypatch.setattr(mod, 'a', 1)\n"
        "    monkeypatch.undo()\n"
    )
    private = (
        "def test_y(monkeypatch, tmp_path):\n"
        "    private = pytest.MonkeyPatch()\n"
        "    private.setattr(mod, 'a', 1)\n"
        "    private.undo()\n"
    )

    assert undoers_in(shared, "shared.py"), "the shared fixture's undo() was missed"
    assert undoers_in(private, "private.py") == [], (
        "a privately constructed MonkeyPatch owns only its own patches and must "
        "not be reported"
    )

    # And the parameter is what makes it shared: the same call on a name that
    # never came from the fixture is somebody else's object.
    unrelated = "def test_z(tmp_path):\n    monkeypatch.undo()\n"
    assert undoers_in(unrelated, "unrelated.py") == []


def test_no_test_undoes_the_shared_monkeypatch():
    """``monkeypatch.undo()`` reverts the autouse isolation fixtures too.

    **This is the seventh instance of `HANDOFF` section 6's oldest trap** --
    *shared state a test can reach will eventually be written by one* -- and the
    first where the isolation was in place and a test switched it off. All six
    isolation fixtures in `conftest.py` are autouse precisely so no test has to
    remember them. `monkeypatch` is a single function-scoped instance shared with
    every fixture that requested it, so one `undo()` in a test body reverts
    `DEFAULT_PATH`, the spend ledger, the answer cache and the rest along with
    whatever the test meant to revert.

    It cost a real database at `C:\\data\\querypilot.db` on the development
    machine, written on every full run for an iteration, invisible because the
    write succeeded. CI surfaced it in one line: a Linux runner cannot create
    `/data`.

    Asserted against the parsed AST, not the file text, for the reason this
    module's docstring gives -- and here specifically because the correct
    alternative is *also* a call to `undo()`:
    `tests/test_multi_pass_recording.py` builds its own `pytest.MonkeyPatch()`
    and undoes that, which touches nothing anybody else owns. A textual search
    for "undo" cannot tell the two apart. The AST can: it looks for `undo()` on
    a name that arrived as the test's own `monkeypatch` parameter.

    The discrimination is proved by
    `test_the_undo_scan_tells_a_private_monkeypatch_from_the_shared_one` rather
    than by this scan. It has to be: **dropping the name check entirely left
    this test green**, because no test in the repository currently both takes
    `monkeypatch` and undoes something else, so the repository contains no case
    that separates the two implementations.
    """
    offenders, inspected = scan_for_shared_monkeypatch_undo()

    # The vacuity guard. This test passes when it finds nothing, so a walk that
    # inspects nothing passes loudest of all.
    assert inspected > 100, (
        f"only {inspected} tests take a monkeypatch parameter; the walk is "
        f"probably broken rather than the suite clean"
    )

    assert not offenders, (
        "monkeypatch.undo() reverts conftest's autouse isolation fixtures as "
        "well, so these tests can write the real history store, spend ledger or "
        "EVALS.md: "
        + ", ".join(offenders)
        + ". Restore the one attribute with a second setattr, or use a "
        "separate pytest.MonkeyPatch() instance."
    )


def test_the_unusable_provider_cannot_be_used(provider_that_must_not_be_called):
    """The stub's second job, which nothing else exercises.

    `provider_that_must_not_be_called` exists first to remove the API-key
    dependency CI found, and second to make "this run spends nothing" a
    guarantee rather than an observation. The second half survived a mutation:
    replacing the `raise` with a canned response left all seven tests green,
    because in each of them the run aborts pre-flight or `run_evaluation` is
    itself replaced, so `complete` is never reached.

    That makes the `raise` defence-in-depth for the day one of those guards
    breaks -- and an untested defence is the thing this project mutation-tests
    to avoid. So the contract is asserted directly instead of being inferred
    from seven tests that never touch it.
    """
    from api.llm.factory import get_provider

    assert get_provider() is provider_that_must_not_be_called, (
        "the fixture must be what run_evals.main() receives"
    )

    try:
        provider_that_must_not_be_called.complete("system", "user")
    except AssertionError as exc:
        assert "spends nothing" in str(exc)
    else:
        raise AssertionError(
            "the stub answered a request; a test asserting nothing is spent "
            "would then pass while the run made calls"
        )


def test_the_gate_runs_with_no_provider_key():
    """AC3, and the guard that keeps the last failure findable.

    CI has always been keyless -- `.env.example` ships `GROQ_API_KEY=` empty --
    but only incidentally, and *incidentally* is what let seven tests depend on
    a developer's key for eight iterations. Stating it in the job environment
    makes the pipeline the standing check that no hermetic test needs a
    credential, and stops a future edit to `.env.example` from quietly masking
    the next one.
    """
    job_env = workflow()["jobs"]["suite"].get("env", {})
    assert "GROQ_API_KEY" in job_env, (
        "the gate should pin GROQ_API_KEY empty rather than rely on "
        ".env.example happening to leave it blank"
    )
    assert job_env["GROQ_API_KEY"] == "", (
        f"the gate must spend no tokens; found {job_env['GROQ_API_KEY']!r}"
    )


# --- Iteration 9 T5 (B-13): a local run keeps its evidence -------------------


def test_local_runs_write_a_junit_report_by_default():
    """**The mechanism B-13 needed, asserted so it cannot be quietly dropped.**

    B-13's entry says *"what to do when it next happens: read the assertion
    message"*, and the message has never been read. Both observed occurrences
    were seen through `-q`, which truncates to `AssertionError: refer...`, and a
    re-run passes and takes the evidence with it. A third occurrence during
    Iteration 9 was lost the same way.

    The junit report carries the whole message under `-q`. This asserts that a
    plain `pytest` writes one, so the evidence exists before anyone knows they
    want it.

    **Read from the parsed config, not by grepping the file.** The long comment
    in `pytest.ini` explaining why the flag is there would satisfy a substring
    search on its own, which is this repository's single most repeated bug --
    five instances in five costumes, per `HANDOFF.md` section 6.
    """
    import configparser
    import pathlib
    import shlex

    parser = configparser.ConfigParser()
    parser.read_string(pathlib.Path("pytest.ini").read_text(encoding="utf-8"))
    addopts = shlex.split(parser.get("pytest", "addopts", fallback=""))

    junit = [opt for opt in addopts if opt.startswith("--junitxml")]
    assert junit, (
        f"pytest.ini's addopts no longer writes a junit report, so a local "
        f"failure is only as legible as the terminal it scrolled past (B-13). "
        f"addopts is {addopts!r}"
    )
    assert len(junit) == 1, f"more than one --junitxml in addopts: {junit}"


def test_the_ci_workflow_overrides_the_local_junit_path():
    """The two report paths must not collide, and CI's must win.

    `ci/require_executed_tests.py` reads the path CI passes. `addopts` is
    prepended to the command line, so a `--junitxml` on the command line takes
    precedence -- verified by running pytest both ways during T5, not inferred
    from the documentation.

    If CI ever stopped passing its own path, it would silently start reading
    whatever `pytest.ini` wrote, which is inside the workspace and not uploaded
    as an artifact. This fails first.
    """
    import pathlib

    workflow = pathlib.Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "--junitxml=" in workflow or '--junitxml="$JUNIT_REPORT"' in workflow, (
        "the workflow no longer passes its own --junitxml, so the floor check "
        "would read pytest.ini's local report instead of CI's"
    )
