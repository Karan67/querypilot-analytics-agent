"""The shape of the runtime image (Iteration 12 T2 and T3).

Hermetic. Everything here reads `api/Dockerfile`, `api/.dockerignore` and
`docker-compose.yml` as files; nothing builds or runs a container. The
properties that can only be observed on a *built* image -- that it actually
runs as uid 10001, that it actually contains no `__pycache__` -- are AC1 and
AC3 and belong to a Docker-dependent lane that does not exist yet.

**The comment-stripping discipline.** This repository has been bitten four
times by a test that asserted "this string must not appear" and then matched
the comment explaining why it must not appear. A Dockerfile is mostly comments
here, and the comments quote the very instructions under test -- `--no-index`,
`USER`, `8000` all appear in prose above the lines that use them. So every
absence assertion below runs against `instructions()`, never against the raw
text, and `test_the_comment_stripper_did_not_gut_the_dockerfile` exists to
prove the stripper did not simply return nothing, which would make every
absence assertion pass for free.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "api" / "Dockerfile"
DOCKERIGNORE = ROOT / "api" / ".dockerignore"
COMPOSE = ROOT / "docker-compose.yml"


def instructions() -> list[str]:
    """The Dockerfile's instruction lines, with comments and blanks removed.

    A Dockerfile comment is a line whose first non-space character is `#`.
    There is no trailing-comment form -- `RUN echo # hi` passes the `#` to the
    shell -- so stripping whole lines is exactly right here, and stripping any
    more would corrupt the instructions.

    Backslash continuations are joined, so one logical instruction is one
    element. The first version of this helper did not, and it read the
    `HEALTHCHECK` as its flags alone -- the probe command lives on the
    continuation line, so an assertion about the probe was silently examining
    `--interval=15s ...` instead. A helper that returns half an instruction
    makes every assertion about the other half meaningless.
    """
    kept: list[str] = []
    pending = ""
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#") or not line.strip():
            continue
        if pending:
            pending = f"{pending} {line.strip()}"
        else:
            pending = line
        if pending.rstrip().endswith("\\"):
            pending = pending.rstrip()[:-1].rstrip()
            continue
        kept.append(pending)
        pending = ""
    if pending:
        kept.append(pending)
    return kept


def instruction_text() -> str:
    return "\n".join(instructions())


def directives(name: str) -> list[str]:
    """Every instruction of one kind, e.g. every `FROM` or every `COPY`."""
    pattern = re.compile(rf"^\s*{name}\s+(.*)$", re.IGNORECASE)
    found = []
    for line in instructions():
        match = pattern.match(line)
        if match:
            found.append(match.group(1).strip())
    return found


# --- the guard that keeps every absence assertion below honest ---------------


def test_the_comment_stripper_did_not_gut_the_dockerfile() -> None:
    """If `instructions()` returned nothing, every test in this file would pass.

    The Dockerfile is deliberately comment-heavy, so the stripper removes most
    of it. What has to survive is the instructions themselves.
    """
    raw = DOCKERFILE.read_text(encoding="utf-8")
    kept = instruction_text()

    assert kept.strip(), "the stripper removed the entire Dockerfile"
    assert len(kept) < len(raw), "the stripper removed nothing at all"
    for required in ("FROM", "RUN", "COPY", "USER", "CMD", "HEALTHCHECK"):
        assert required in kept, f"the stripper ate the {required} instructions"


def test_the_instruction_reader_joins_continuations() -> None:
    """The other half of the stripper guard, and a real bug this caught.

    `HEALTHCHECK` puts its flags on one line and its probe command on the next.
    A reader that stops at the backslash returns the flags alone, and then an
    assertion about the probe command is quietly examining `--interval=15s`.
    Same for the `RUN` that creates the account: `groupadd` and `useradd` are
    one instruction across two lines.
    """
    probe = " ".join(directives("HEALTHCHECK"))
    assert "--interval" in probe and "CMD" in probe, "HEALTHCHECK was cut at the backslash"

    account = [r for r in directives("RUN") if "groupadd" in r]
    assert account, "no account-creating RUN found"
    assert "useradd" in account[0], "the RUN was cut at the backslash"


def test_the_stripper_really_does_remove_the_commentary() -> None:
    """The inverse guard: prose that exists only in comments must be gone.

    Without this, a stripper that returned the file unchanged would satisfy
    every other test here while proving nothing.
    """
    raw = DOCKERFILE.read_text(encoding="utf-8")
    kept = instruction_text()
    assert "decoration" in raw, "the Dockerfile lost the comment this test anchors on"
    assert "decoration" not in kept


# --- AC2: two stages, and the second one is not the first --------------------


def stage_names() -> list[str]:
    """The `AS <name>` alias of each stage, parsed rather than substring-matched.

    The first version of this compared with `"as runtime" in line.lower()`, and
    a mutation that renamed the stage to `runtimex` sailed through it -- the
    substring is still present. An assertion that a *prefix* of the wrong
    answer satisfies is not an assertion.
    """
    names = []
    for stage in directives("FROM"):
        match = re.search(r"\bAS\s+([A-Za-z0-9_.-]+)\s*$", stage, re.IGNORECASE)
        names.append(match.group(1).lower() if match else "")
    return names


def test_the_dockerfile_is_multi_stage() -> None:
    stages = directives("FROM")
    assert len(stages) == 2, f"expected a builder and a runtime stage, found {stages}"
    assert stage_names() == ["builder", "runtime"]


def test_the_runtime_stage_installs_from_the_builder_stages_wheels() -> None:
    runs = directives("RUN")
    assert any("from=builder" in r and "/wheels" in r for r in runs), (
        "the runtime stage does not take anything from the builder, which makes "
        "the builder theatre"
    )


def test_the_runtime_install_never_contacts_an_index() -> None:
    """`--no-index` is the load-bearing flag.

    Without it, `pip install --find-links` silently falls back to PyPI when a
    wheel is missing, the builder stage stops mattering, and the build stops
    being reproducible without anyone being told.
    """
    installs = [r for r in directives("RUN") if "pip install" in r]
    assert installs, "the runtime stage installs nothing"
    for line in installs:
        assert "--no-index" in line
        assert "--find-links" in line


def test_the_wheels_never_become_a_layer_at_all() -> None:
    """A COPY cannot be undone by a later `rm`, and measurement proved it.

    The first version copied /wheels into the runtime stage and deleted it in
    the same RUN. `/wheels` was then absent from the filesystem and still
    present in the image, which had grown 39MB. A bind mount is visible to one
    command and is committed to nothing.
    """
    text = instruction_text()
    assert "--mount=type=bind,from=builder" in text
    assert "COPY --from=builder" not in text, (
        "a COPY from the builder commits a layer that no later rm can reclaim"
    )


def test_no_pip_cache_is_kept() -> None:
    for line in directives("RUN"):
        if "pip " in line:
            assert "--no-cache-dir" in line, f"pip line keeps its cache: {line}"


# --- AC1: the process is not root --------------------------------------------


def test_the_image_declares_a_non_root_user() -> None:
    users = directives("USER")
    assert users, "no USER instruction: the container runs as root"
    assert users[-1].split(":")[0] not in {"root", "0"}


def test_the_user_is_the_fixed_numeric_uid_the_spec_chose() -> None:
    """Spec §7 Q-D resolved to a fixed numeric uid rather than a name.

    A name is a local convenience; the number is what a host's user namespace,
    a volume's ownership and `docker top` all actually see.
    """
    assert directives("USER")[-1] == "10001:10001"


def test_the_user_is_declared_after_the_application_is_copied() -> None:
    """Otherwise the COPY runs as 10001 and the source becomes process-writable.

    The application has no business writing its own source, and a non-root user
    that owns the code it executes gives back most of what non-root buys.
    """
    lines = instructions()
    user_at = max(i for i, line in enumerate(lines) if line.strip().upper().startswith("USER"))
    copy_app_at = max(
        i for i, line in enumerate(lines) if re.match(r"^\s*COPY\s+\.\s+", line, re.IGNORECASE)
    )
    assert copy_app_at < user_at


def test_the_data_directory_is_owned_by_that_uid() -> None:
    """The history store's volume mounts at /data.

    Docker propagates the image's ownership of a directory when it initialises
    an *empty* named volume, so creating /data with the right owner here is
    what makes a fresh deployment work. An already-existing volume keeps the
    ownership it was created with, which is T4's migration.
    """
    text = instruction_text()
    assert "/data" in text
    assert "chown 10001:10001 /data" in text


# --- AC4: the image carries its own probe ------------------------------------


def test_the_image_declares_its_own_healthcheck() -> None:
    """Before this, only `docker-compose.yml` had one.

    Every platform the charter names -- Render, Fly.io, Railway -- ignores a
    compose file, so the deployed container ran with no liveness probe at all.
    """
    assert directives("HEALTHCHECK"), "the image has no HEALTHCHECK"


def test_the_healthcheck_delegates_to_the_committed_script() -> None:
    probe = " ".join(directives("HEALTHCHECK"))
    assert "api.healthcheck" in probe


def test_the_probe_is_invoked_as_a_module_and_never_as_a_file_path() -> None:
    """A real failure, found by running the container rather than reading it.

    `python /app/api/healthcheck.py` puts /app/api on sys.path, where this
    project's own `api/http/` package shadows the standard library's `http`.
    `import urllib.request` then dies with `No module named 'http.client'`,
    and the container reports unhealthy while serving every request correctly
    -- the precise failure mode Iteration 0 called worse than no probe at all.

    `-m` resolves from WORKDIR /app, the same path `api.main:app` already
    depends on.
    """
    for probe in (
        " ".join(directives("HEALTHCHECK")),
        " ".join(_compose()["services"]["api"]["healthcheck"]["test"]),
    ):
        tokens = re.split(r"[\s,\[\]\"']+", probe)
        assert "-m" in tokens, f"probe is not a module invocation: {probe}"
        assert "healthcheck.py" not in probe, (
            "invoking the probe by file path puts api/ on sys.path, where "
            "api/http/ shadows the stdlib http module"
        )


# --- AC6: the port can move ---------------------------------------------------


def test_the_command_binds_the_injected_port() -> None:
    """Render, Fly.io and Railway inject `$PORT` and expect the process to bind
    it. Before AC6 the CMD hardcoded 8000 and the image could not comply."""
    command = " ".join(directives("CMD"))
    assert "PORT" in command
    assert "${PORT:-8000}" in command


def test_the_command_execs_so_that_uvicorn_is_pid_1() -> None:
    """Without `exec`, the shell stays PID 1 and a platform's SIGTERM never
    reaches uvicorn, so every deploy ends in a kill after the grace period."""
    command = " ".join(directives("CMD"))
    assert command.count("exec ") == 1


def test_eight_thousand_survives_only_as_the_fallback() -> None:
    """The literal port may appear in EXPOSE and as the CMD's default. Anywhere
    else is a second source of truth that cannot disagree out loud."""
    for line in instructions():
        if "8000" not in line:
            continue
        upper = line.strip().upper()
        assert upper.startswith("EXPOSE") or upper.startswith("CMD"), (
            f"8000 is hardcoded outside EXPOSE and CMD: {line}"
        )


# --- AC3's companion: the build context ---------------------------------------


def test_a_dockerignore_exists_beside_the_build_context() -> None:
    """The build context is `./api`, so the file has to live there.

    A `.dockerignore` at the repository root is read for a root context and
    ignored for this one, which is a silent no-op rather than an error.
    """
    assert DOCKERIGNORE.is_file()


@pytest.mark.parametrize(
    "pattern",
    ["**/__pycache__", "**/*.py[cod]", "**/.venv", "**/.git", "requirements-dev.txt"],
)
def test_the_dockerignore_excludes_what_the_census_found(pattern: str) -> None:
    """T1 measured 61 of 106 context files as host bytecode -- 57% of the bytes.

    Every pattern carries the `**/` prefix deliberately. Docker matches with
    `filepath.Match`, not gitignore semantics, so `__pycache__/` matches only
    the context root -- rebuilding with that form left 6 of 7 directories in
    the image.

    This is the weaker half of AC3. The real property is that the *built image*
    carries no bytecode under /app, which cannot be checked without a daemon.
    """
    lines = [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines, "the .dockerignore reader returned nothing"
    assert pattern in lines


# --- T3: the compose probe stopped being a second source of truth -------------


def _compose() -> dict:
    """Parsed as YAML, never grepped.

    `tests/test_ci_guards.py` established this: the compose file's comments
    discuss the very strings under test, so a text search matches the
    explanation rather than the configuration.
    """
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_the_compose_healthcheck_no_longer_hardcodes_a_port() -> None:
    probe = _compose()["services"]["api"]["healthcheck"]["test"]
    assert "8000" not in " ".join(probe)


def test_the_compose_healthcheck_runs_the_same_script_the_image_does() -> None:
    """One script, so the probe and the server cannot disagree about the port."""
    probe = " ".join(_compose()["services"]["api"]["healthcheck"]["test"])
    assert "api.healthcheck" in probe
    assert "api.healthcheck" in " ".join(directives("HEALTHCHECK"))


def test_the_compose_file_still_parses_and_declares_both_services() -> None:
    """Vacuity guard for the two tests above: if `_compose()` returned an empty
    mapping they would fail loudly, but if the api service lost its healthcheck
    entirely they would error rather than report the real problem."""
    services = _compose()["services"]
    assert {"api", "db"} <= set(services)
    assert "healthcheck" in services["api"]


# --- T4: the volume belongs to the account the API runs as -------------------


def test_a_one_shot_service_takes_ownership_of_the_data_volume() -> None:
    """The Dockerfile's `chown` only reaches a *fresh* named volume.

    Docker copies the image's ownership of a directory when it initialises an
    empty volume. A volume that already exists keeps the ownership it was
    created with, which for every deployment predating T2 is root -- measured,
    not assumed: the new image against the existing volume answered
    `touch: cannot touch '/data/probe': Permission denied`.
    """
    services = _compose()["services"]
    assert "data-init" in services, "nothing fixes the ownership of an existing volume"

    init = services["data-init"]
    entrypoint = " ".join(init["entrypoint"])
    assert "chown" in entrypoint
    assert "10001" in entrypoint
    assert any("/data" in str(v) for v in init["volumes"])


def test_the_one_shot_runs_privileged_and_the_api_does_not() -> None:
    """A chown needs root. Isolating it here is what lets the API image stay
    non-root without `gosu` and without a privilege-dropping entrypoint."""
    services = _compose()["services"]
    assert str(services["data-init"]["user"]) in {"0:0", "0", "root"}
    assert "user" not in services["api"], (
        "the api service overrides the image's USER, which would undo AC1"
    )


def test_the_api_waits_for_the_ownership_fix_to_finish() -> None:
    """`service_completed_successfully`, not `service_started`.

    The chown has to have *finished* before uvicorn opens the history store,
    and it has to have succeeded -- a failed chown that the API started anyway
    would surface as an unwritable history and a healthy-looking container.
    """
    depends = _compose()["services"]["api"]["depends_on"]
    assert depends["data-init"]["condition"] == "service_completed_successfully"


def test_the_one_shot_does_not_restart() -> None:
    """It exits 0 by design; `unless-stopped` would loop it forever."""
    assert str(_compose()["services"]["data-init"]["restart"]) == "no"


# --- T11: Postgres is not published on every interface -----------------------


def test_postgres_binds_loopback_by_default() -> None:
    """AC22. Measured before this line existed: `docker ps` reported
    `0.0.0.0:5432->5432/tcp`, reachable from any machine that could route to
    this host at all. Nothing in this project needs more than loopback -- the
    host test suite and the eval runner both connect to `localhost`."""
    port = _compose()["services"]["db"]["ports"][0]
    assert port.startswith("${POSTGRES_BIND_HOST:-127.0.0.1}:"), (
        f"the db port is not bound to a loopback default: {port!r}"
    )


def test_postgres_bind_host_is_configurable() -> None:
    """The escape hatch is real configuration, not a hand-edit of the file."""
    port = _compose()["services"]["db"]["ports"][0]
    assert "POSTGRES_BIND_HOST" in port


def test_the_api_port_is_unrestricted() -> None:
    """Only Postgres narrows. The API is meant to be publicly reachable --
    that is the whole point of a deployment -- so its port stays as it was."""
    port = _compose()["services"]["api"]["ports"][0]
    assert not port.startswith("127.0.0.1"), (
        "the API's own port narrowed too, which would make it unreachable "
        "from outside the host"
    )
