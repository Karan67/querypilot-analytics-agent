"""Iteration 8 T7 — the shipping surface (`011-ship.md` AC10-AC12).

Three claims that had never been checked by anything:

**The `api` service reports its own health** (AC12). `011-ship.md` §2.7 found a
healthcheck on `db` and none on `api`, so `docker compose ps` showed the API as
`Up` whether or not it could serve a request. Iteration 0's lesson was that a
container reporting healthy while broken is worse than one reporting nothing,
and the API had been the version of that this project shipped.

**The README's numbers are traceable** (AC10). Every figure in it must come from
`EVALS.md` or a spec §2, with its caveat attached. The tests here do not check
prose; they check that specific claims still match the files they came from,
which is the part that rots.

**`docker compose up` works from an empty volume** (AC11). That is verified by
running it, not by a test — `down -v` then `up` is in the T7 report. What is
asserted here is the configuration that boot depends on, because the boot itself
happens once and the configuration is what a later change would break.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
README_PATH = REPO_ROOT / "README.md"
EVALS_PATH = REPO_ROOT / "EVALS.md"


def compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def service(name: str) -> dict:
    return compose()["services"][name]


# --- AC12: the api service reports its own health ---------------------------


def test_ac12_the_api_service_has_a_healthcheck():
    """§2.7's finding, closed.

    Asserted on the parsed YAML rather than on the text, because
    `docker-compose.yml` carries a long comment explaining what the healthcheck
    is for — and a textual search for "healthcheck" would match that comment in
    a file that had none.
    """
    api = service("api")
    assert "healthcheck" in api, (
        "the api service has no healthcheck, so `docker compose ps` reports it "
        "Up whether or not it can serve a request (AC12)"
    )

    test = api["healthcheck"]["test"]
    assert test, "the healthcheck has no command"


def test_ac12_the_healthcheck_asks_the_endpoint_that_proves_the_chain():
    """A port probe is not a health check.

    `/health` proves the API started, holds the read-only credential, can
    authenticate, and that Chinook is loaded. A TCP check on 8000 reports
    healthy for a process that accepts connections and cannot answer — which is
    the exact failure §2.7 describes, one layer down.
    """
    command = " ".join(str(part) for part in service("api")["healthcheck"]["test"])

    assert "/health" in command, (
        f"the healthcheck must probe /health, not just the port: {command!r}"
    )
    assert "8000" in command


def test_ac12_the_healthcheck_needs_nothing_the_image_does_not_have():
    """No curl, and no new runtime dependency to run a liveness probe.

    `python:3.12.13-slim` ships no curl — verified by running
    `command -v curl` in the container, which answers `no`. A healthcheck
    written with curl would fail permanently *and* look correct in review, and
    installing curl would put a package in the shipping image to support a
    probe.
    """
    command = " ".join(str(part) for part in service("api")["healthcheck"]["test"])

    assert "curl" not in command, "curl is not in the runtime image"
    assert "wget" not in command, "wget is not in the runtime image either"
    assert "python" in command


def test_ac12_the_probe_does_not_resolve_localhost():
    """`127.0.0.1`, not `localhost`.

    The probe runs inside the container. On a dual-stack resolver `localhost`
    can resolve to `::1` while uvicorn is bound to `0.0.0.0`, which is IPv4
    only — so the container would report unhealthy while being entirely fine.
    Pinning the literal address removes a failure mode that depends on the
    host's resolver.
    """
    command = " ".join(str(part) for part in service("api")["healthcheck"]["test"])

    assert "127.0.0.1" in command
    assert "localhost" not in command


def test_ac12_a_failing_health_endpoint_makes_the_container_unhealthy():
    """The status code does the work, and that is worth asserting.

    `/health` returns **503** when the database is unreachable, and
    `urllib.request.urlopen` raises `HTTPError` on a non-2xx — so the probe
    exits non-zero with no parsing, no grep of the body, and no second copy of
    the health logic to drift from the first.

    A probe that read the body and looked for `"ok"` would be the text-matching
    antipattern `003` argued against, in the one place where a typed alternative
    is already available.
    """
    command = " ".join(str(part) for part in service("api")["healthcheck"]["test"])

    assert "urlopen" in command, (
        "the probe must let a non-2xx raise; a check that ignores the status "
        "code would report healthy on /health's own 503"
    )
    for banned in ("status", '"ok"', "read()", "json"):
        assert banned not in command, (
            f"the probe inspects the body with {banned!r}; the 503 is the "
            f"signal and re-deriving it here is a second copy of /health"
        )


def test_the_db_healthcheck_was_not_disturbed():
    """The one that already existed, still doing its job.

    Its `-h 127.0.0.1` is load-bearing: during init the postgres entrypoint
    runs a temporary server on the unix socket only, so a socket-based check
    reports healthy while Chinook is still loading — and `depends_on:
    service_healthy` would then start the API against a half-loaded database.
    """
    command = " ".join(str(part) for part in service("db")["healthcheck"]["test"])

    assert "pg_isready" in command
    assert "-h 127.0.0.1" in command, (
        "a socket-based check reports healthy before init finishes"
    )


# --- AC11: the configuration a clean boot depends on ------------------------


def test_ac11_the_api_waits_for_a_healthy_database():
    """`service_healthy`, not `service_started`.

    On an empty volume the database spends up to a minute loading Chinook. An
    API that started as soon as the container existed would take requests
    against a database with no tables, which is `011-ship.md` §2.1's
    green-and-empty shape in the request path.
    """
    depends = service("api")["depends_on"]
    assert depends["db"]["condition"] == "service_healthy"


def test_ac11_the_init_scripts_and_seed_are_mounted_read_only():
    """The boot reads them; nothing should be able to write them.

    `db/init/` runs in filename order on an empty volume and its order is
    load-bearing — 01 loads Chinook, 02 creates views, 03 grants SELECT on what
    01 and 02 made. A writable mount would let a container mutate the thing
    that defines how every future clean boot behaves.
    """
    volumes = service("db")["volumes"]
    mounts = {entry.split(":")[1]: entry for entry in volumes if entry.count(":") >= 2}

    init = mounts.get("/docker-entrypoint-initdb.d")
    assert init and init.endswith(":ro"), f"init scripts are not read-only: {init}"

    seed = mounts.get("/seed")
    assert seed and seed.endswith(":ro"), f"the seed is not read-only: {seed}"


def test_ac11_both_volumes_are_named_so_down_keeps_them():
    """`down` keeps the data; only `down -v` discards it.

    Both stores are named volumes rather than bind mounts. For `pgdata` that is
    Iteration 0's arrangement; for `querypilot_data` it was Iteration 7's D-4,
    on the reasoning that a bind mount would put a `.db` file in the working
    tree, which is how a gitignored file becomes a committed one.
    """
    declared = compose()["volumes"]
    assert set(declared) == {"pgdata", "querypilot_data"}

    api_volumes = service("api")["volumes"]
    assert any(entry.startswith("querypilot_data:") for entry in api_volumes)


def test_the_history_path_agrees_with_the_mount():
    """Two statements of one path, checked against each other.

    `QUERYPILOT_HISTORY_PATH` is set explicitly in the environment *and* the
    volume is mounted at a directory, and the comment in the compose file says
    why: "the path and the mount below have to agree and one of them being
    implicit is how they stop agreeing." This is that comment as a test.
    """
    api = service("api")
    configured = pathlib.PurePosixPath(api["environment"]["QUERYPILOT_HISTORY_PATH"])

    mount_points = [entry.split(":")[1] for entry in api["volumes"]]
    assert str(configured.parent) in mount_points, (
        f"history is written to {configured} but nothing is mounted at "
        f"{configured.parent}; the file would live in the container's writable "
        f"layer and vanish on the next `up`"
    )


# --- AC10: the README's numbers are traceable -------------------------------


def test_ac10_the_readme_keeps_the_held_out_result_with_its_caveat():
    """The one number most likely to be flattened into a headline.

    `009` AC13 kept an accuracy claim off the answer page; the same pressure
    applies to a README, which is where "100%" would live without the sentence
    that makes it honest. The phrase is required in the README, and the same
    phrase must still be in `EVALS.md` — so this cannot pass on a README that
    quotes a caveat the record has dropped.

    **Whitespace is collapsed first, and the first version of this test failed
    without it.** The phrase appears in `EVALS.md` wrapped across a line break,
    so a literal substring search reported it missing from a file that says it
    twice. That is the third time this repository has hit that exact trap — the
    AC13 rendered-copy test hit it in Iteration 7 — which is reason enough to
    normalise before matching rather than to trust the line width of prose.
    """
    def flat(text: str) -> str:
        return " ".join(text.split())

    phrase = "between 90% and 100%, measured once at 100%"
    assert phrase in flat(README_PATH.read_text(encoding="utf-8")), (
        f"the README no longer states the result as {phrase!r}"
    )
    assert phrase in flat(EVALS_PATH.read_text(encoding="utf-8")), (
        f"the README quotes a caveat {phrase!r} that EVALS.md does not contain"
    )


def test_ac10_the_readme_names_the_iteration_the_handoff_calls_latest():
    """A README naming the wrong iteration is the cheapest way to mislead.

    **It was naming Iteration 7 while Iteration 8 was six tasks in**, which is
    what this test found. Derived from `HANDOFF.md`'s own state table rather
    than hardcoded, so the two cannot drift.

    Keyed on the **highest-numbered row** rather than on an "In progress"
    marker, and the first version was keyed on the marker. That broke the moment
    Iteration 8 closed: with nothing in progress there was no marker to find,
    and a test asserting "exactly one" then failed on a repository that was
    simply between iterations. The latest row exists in both states, so this
    holds while an iteration runs and after it closes.
    """
    handoff = (REPO_ROOT / "HANDOFF.md").read_text(encoding="utf-8")

    numbered = [
        int(match.group(1))
        for line in handoff.splitlines()
        if line.startswith("|")
        for match in [re.match(r"\|\s*\*{0,2}(\d+)\s+\w", line)]
        if match
    ]
    assert numbered, "could not read the iteration table in HANDOFF.md"
    current = [max(numbered)]

    # **Scoped to the "Current state" line, and the first version was not.**
    # Searching the whole README for "Iteration 8" passed while the headline
    # said Iteration 7, because the metrics table cites "Iteration 8 T5"
    # further down. An absence assertion over a long document finds the string
    # somewhere and proves nothing about the sentence that matters.
    readme_lines = README_PATH.read_text(encoding="utf-8").splitlines()
    state_lines = [line for line in readme_lines if "Current state" in line]
    assert len(state_lines) == 1, (
        f"expected one 'Current state' line in the README, found {len(state_lines)}"
    )

    assert f"Iteration {current[0]}" in state_lines[0], (
        f"HANDOFF says Iteration {current[0]} is current; the README says "
        f"{state_lines[0].strip()!r}"
    )


def test_ac10_every_test_count_in_the_docs_agrees_with_the_others():
    """Three files quote the suite size; a stale one is a small lie that spreads.

    Not the *live* count — that would make every added test a documentation
    change, which is how a number people have to update becomes a number people
    stop updating. This asserts only that the files agree with each other.
    """
    quoted = {}
    for name in ("README.md", "CLAUDE.md", "HANDOFF.md"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        found = set(re.findall(r"\b1,(\d{3})\s+tests\b", text))
        if found:
            quoted[name] = found

    assert quoted, "no file quotes a test count; this test is vacuous"

    everything = set().union(*quoted.values())
    assert len(everything) == 1, (
        f"the docs quote different suite sizes: "
        + ", ".join(f"{k} says {sorted(v)}" for k, v in quoted.items())
    )


@pytest.mark.parametrize(
    "claim,source",
    [
        # Each of these appears in the README and must still be in the file it
        # came from. AC10 is traceability, so the test is the trace.
        ("querypilot_ro", "specs/000-project.md"),
        ("200,000", "specs/000-project.md"),
    ],
)
def test_ac10_readme_claims_trace_to_a_source(claim, source):
    readme = README_PATH.read_text(encoding="utf-8")
    if claim not in readme:
        pytest.skip(f"the README no longer mentions {claim!r}")

    origin = (REPO_ROOT / source).read_text(encoding="utf-8")
    assert claim in origin, (
        f"the README states {claim!r} and {source} no longer does; a number "
        f"without a source is the estimate AC10 exists to keep out"
    )
