"""Shared pytest fixtures.

**The database location is known only to this module.** Decision D-1 in
`specs/001-schema-tool-plan.md`: the DSN comes from ``TEST_DATABASE_URL`` and
nothing is hardcoded in test setup. A test that embeds ``localhost:5432``
directly is to be rejected in review — Iteration 8 CI must be able to point the
whole suite at a different server by setting one environment variable.

The suite runs against a live Chinook container rather than a mocked
``Inspector``. Mocking introspection would assert that the mock matches the code
and prove nothing about Postgres, which is the only thing the schema tool
actually talks to.
"""

from __future__ import annotations

import os
import pathlib

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

#: Local-development fallback only. Matches the committed `.env.example`, which
#: holds no real secret. CI overrides it via TEST_DATABASE_URL.
DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg://querypilot_ro:readonlylocaldev@localhost:5432/chinook"
)

#: Seconds the reachability probe waits before giving up and skipping.
#:
#: Not optional. A refused connection fails immediately, but a host that accepts
#: the packet and never answers — Docker Desktop stopped mid-session, a firewall
#: dropping rather than rejecting, a paused container — makes libpq wait
#: indefinitely by default, and the suite hangs instead of skipping. Observed
#: exactly that: a three-minute hang when the Docker daemon went away.
#:
#: libpq clamps anything below 2 to 2 seconds.
PROBE_CONNECT_TIMEOUT_SECONDS = 3

#: Set to ``1`` to make an unreachable database a **failure** instead of a skip,
#: **for the tests that declare one**.
#:
#: The qualifier is new at Iteration 11 T4 and it narrows what this flag does.
#: While `configured_database` was autouse, setting this failed the entire run.
#: Now it fails only the `needs_db` lane; the 888 hermetic tests run and pass
#: regardless. That is still a red build, which is all the flag was ever for --
#: but `ci.yml` and `ci/require_executed_tests.py` both quote the old, wider
#: claim and are corrected at T6.
#:
#: `011-ship.md` §2.1 measured the reason this exists. Pointed at a dead DSN the
#: suite reports ``1109 skipped`` and **exit code 0** — a green build that
#: verified nothing, which is the single most dangerous property this repository
#: could take into CI. It is not hypothetical or a misconfiguration: it is the
#: designed behaviour of the fixture below, and the design is right for a
#: developer with the stack down.
#:
#: So the choice is made by the caller rather than guessed. A developer gets one
#: line of explanation; CI sets this and gets a red build. The variable is read
#: here and nowhere else, for the same reason ``TEST_DATABASE_URL`` is.
REQUIRE_DATABASE_ENV = "QUERYPILOT_TESTS_REQUIRE_DATABASE"


def _database_is_required() -> bool:
    """Whether an unreachable database should fail the run rather than skip it.

    Exactly ``"1"``, not any truthy string. ``QUERYPILOT_TESTS_REQUIRE_DATABASE=0``
    meaning *required* is the kind of surprise that gets discovered during an
    incident, and an unset variable and an explicitly disabled one must behave
    identically.
    """
    return os.environ.get(REQUIRE_DATABASE_ENV, "").strip() == "1"


def _database_url() -> str:
    """Resolve the test DSN. Deliberately not named ``test_*`` — pytest would
    collect it as a test case."""
    return os.environ.get("TEST_DATABASE_URL") or DEFAULT_TEST_DATABASE_URL


@pytest.fixture(scope="session")
def configured_database() -> str:
    """Point the application's engine at the test database, or skip the test.

    **Opt-in since Iteration 11 T4 (B-15).** This was `autouse=True`, which gave
    all 1,348 tests a database and made every one of them skip when the stack was
    down. `specs/014-test-isolation.md` §2.1 measured what that cost: **888 of
    them never issue a statement.** A test reaches this fixture now by declaring
    a database two ways -- `@pytest.mark.needs_db` and a request for this fixture
    -- and `tests/isolation.py` refuses an Engine to any test that does neither.

    The marker does not apply the fixture and the fixture does not imply the
    marker. Both are written, and `tests/test_isolation.py` asserts the two sets
    are equal; injecting one from the other would make half that equality true by
    construction.

    ``get_schema()`` reads ``QUERYPILOT_DATABASE_URL`` through ``get_engine()``,
    which is ``lru_cache``d. Setting the variable and clearing that cache lets
    the tool under test run completely unmodified — no test-only parameter is
    threaded through the production call path, which keeps AC14 (no parameters)
    honest rather than merely asserted.

    If nothing is listening, every test skips with a reason that says what to do
    about it. An unreachable database is an environment problem, and reporting
    it as a wall of failures would bury the one line that matters.

    **Unless the caller says otherwise** (Iteration 8 T4). That skip is correct
    for a developer and catastrophic for a pipeline: `011-ship.md` §2.1 measured
    exit code 0 over 1,109 skipped tests. ``QUERYPILOT_TESTS_REQUIRE_DATABASE=1``
    inverts it, and CI sets that. The decision belongs to whoever started the
    run, so it is read from the environment instead of inferred from ``CI`` —
    a run on a developer's machine that happens to export ``CI`` should not
    change meaning.
    """
    from api.db import engine as engine_module

    url = _database_url()

    try:
        probe = create_engine(
            url,
            connect_args={"connect_timeout": PROBE_CONNECT_TIMEOUT_SECONDS},
        )
        with probe.connect() as conn:
            conn.execute(text("SELECT 1"))
        probe.dispose()
    except SQLAlchemyError as exc:
        detail = (
            f"No database at the configured DSN ({exc.__class__.__name__}). "
            f"Start it with 'docker compose up -d', or set TEST_DATABASE_URL. "
            f"Underlying error: {exc}"
        )
        if _database_is_required():
            pytest.fail(
                f"{REQUIRE_DATABASE_ENV}=1, so an unreachable database is a "
                f"failed run rather than a skipped one. {detail}",
                pytrace=False,
            )
        pytest.skip(detail)

    os.environ[engine_module.DATABASE_URL_ENV] = url
    engine_module.get_engine.cache_clear()
    yield url
    engine_module.get_engine.cache_clear()


@pytest.fixture(scope="session")
def schema(configured_database: str):
    """The introspected schema. Session-scoped: it is immutable, and one
    catalog read is enough for the whole suite."""
    from api.db.introspection import get_schema

    return get_schema()


@pytest.fixture(scope="session")
def relations(schema) -> dict:
    """Relations keyed by name, for readable lookups in assertions."""
    return {table.name: table for table in schema.tables}


@pytest.fixture(scope="session")
def track_columns(relations) -> dict:
    """Columns of `track` keyed by name. `track` is the workhorse fixture: it
    has a primary key, nullable and non-nullable columns, varchar with a
    length, numeric with a precision, and three foreign keys."""
    return {column.name: column for column in relations["track"].columns}


def _load_dotenv() -> None:
    """Make `.env` visible to host tests.

    Production never does this -- the container gets its environment from
    docker compose, which is the 12-factor arrangement. But host tests have no
    such injector, and the live LLM tests need GROQ_API_KEY. Test-only, and
    existing environment always wins so CI can override without touching files.
    """
    env_file = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


@pytest.fixture
def provider_that_must_not_be_called(monkeypatch):
    """Let `run_evals.main()` get past provider construction with no API key.

    **Opt-in, not autouse**, and that matters: an autouse version would hand a
    stub to `tests/test_llm_live.py`, whose whole purpose is to reach the real
    provider, and to every test that installs a scripted provider of its own.

    **Found by CI, on the second run this repository ever had** (Iteration 8
    T4). Seven tests of the eval runner's pre-flight guards failed with
    ``assert 2 == 1``. `main()` builds the provider *before* it projects the
    cost, so with no key it returns 2 from "Provider error" and never reaches
    the guard under test. On this project's one development machine `.env`
    supplies a real key, so the guards were reached and the tests passed for
    eight iterations.

    The clearest evidence of the misunderstanding is a docstring: the zero-budget
    test said *"no provider is configured in this test, and the run must fail
    before it would need one."* The second half is true and the first was not --
    a provider was configured, by the developer's environment, and the test
    would have been just as green if the guard had been deleted and the key
    removed.

    So the stub does two jobs. It removes the environment dependency, and
    `complete` raises: these tests assert that **nothing is spent**, and a
    provider that cannot be used without failing the test is a stronger
    statement of that than a provider that merely happens not to be called.
    """

    class _Unusable:
        #: Read by the runner's reporting. Named so it is obvious in output
        #: that no real model was involved.
        model = "unusable-stub"

        def complete(self, system: str, user: str) -> str:
            raise AssertionError(
                "the provider was called, but this test asserts the run spends "
                "nothing and aborts before it would need a provider"
            )

    stub = _Unusable()
    monkeypatch.setattr("api.llm.factory.get_provider", lambda: stub)
    return stub


@pytest.fixture(autouse=True)
def isolated_spend_ledger(tmp_path, monkeypatch):
    """Never let a test write the real daily-spend ledger.

    **Autouse, and structural on purpose.** The B-5 tests isolate the ledger
    explicitly, but two tests elsewhere drive `main()` end to end and reach the
    ledger write without knowing it exists -- and they wrote 6,800 tokens and 40
    requests of fake spend into the real file on the first full run after B-5
    landed. Gitignored, so it would never have shown up in review; and read at
    the next real run's pre-flight, where it would have silently moved the
    daily guard by the size of a small benchmark.

    This is the `EVALS_PATH` trap of T5 in a second place, and per-test
    discipline is what failed there too. A test that has to remember to isolate
    shared state is a test that will one day forget, so the isolation is applied
    to every test whether it asks or not.
    """
    from evals import ledger

    monkeypatch.setattr(ledger, "DEFAULT_PATH", tmp_path / "spend.json")


@pytest.fixture(autouse=True)
def isolated_history_store(tmp_path, monkeypatch):
    """Never let a test write the real history database.

    **The third instance of the same trap, isolated before it can bite rather
    than after.** T5's `EVALS_PATH` put a fabricated entry in the real
    `EVALS.md`; B-5's ledger took 6,800 tokens of fake spend from two tests that
    drove `main()` end to end and had no idea a ledger existed. Both were
    gitignored, so neither would have shown up in review.

    `POST /ask` now writes history, and the endpoint tests drive it without
    caring that a store exists -- which is precisely the shape of both earlier
    failures. Per-test discipline is what failed twice, so this is autouse and
    applies whether a test asks for it or not.
    """
    from api.store import history

    monkeypatch.setattr(history, "DEFAULT_PATH", tmp_path / "history.db")
    monkeypatch.setattr(history, "_degraded", "")


@pytest.fixture(autouse=True)
def isolated_answer_cache():
    """Never let one test's answer be served to another.

    **The fourth instance of the same trap, and the first where the shared state
    is in memory rather than on disk.** `EVALS.md`, the spend ledger and the
    history database were all files; this one is a module-level dict, which is
    worse in one specific way -- a leaked entry does not sit there waiting to be
    noticed, it makes a *later* test's provider call silently not happen.

    Consider the failure it prevents: a test asks a question and asserts the
    provider was called once. It passes alone and fails in a full run, or worse,
    passes in both because a *different* test was the one that paid. The suite
    would then be asserting the behaviour of whichever test happened to run
    first, which is not a property anybody chose.

    Cleared before and after: before, so a test never inherits; after, so a
    failing test does not leave a primed cache behind for the next file.
    """
    from api.http import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def isolated_quota_snapshot():
    """The fifth. Same rule, applied without waiting to be bitten.

    `api/http/quota.py` keeps the last rate-limit reading in a module global,
    because T4 made the provider per-request and there is nowhere else for it to
    live. A reading left behind by one test would make another test's `/quota`
    report limits nobody in that test observed -- and the warning it drives is
    exactly the kind of thing that looks right until it is wrong.
    """
    from api.http import quota

    quota.clear()
    yield
    quota.clear()


# --- there was a sixth isolator here, and B-14 removed the thing it guarded ---
#
# `isolated_schema_cache` cleared `api/db/schema_cache.py` around every test.
# It was the one isolator that could hold something **false** rather than merely
# stale: a test monkeypatching `get_schema` to return a hand-built `Schema` left
# that fake cached behind, and the next test built its prompt from a database
# that does not exist.
#
# Iteration 9 T4 retired the cache (B-14), so there is no slot left to falsify —
# every caller reads the catalog. This note stays because the hazard was real and
# is the kind of thing that gets re-introduced by someone adding a cache back
# without knowing what it cost: any future memo of the schema needs an isolator
# here on the day it lands, not the iteration after.


# --- Iteration 10 T3: authenticating, visibly ---------------------------------
#
# **Never autouse, and that is the whole design.** `013-auth.md` §2.6 counted 100
# endpoint calls across seven files that will meet the gate T4 wires in. An
# autouse credential fixture would carry all of them through it, leaving AC1 --
# *an unauthenticated request is refused and spends nothing* -- asserted by
# nothing while the suite stayed green. That is the shape `HANDOFF.md` §6 records
# twice, most recently at Iteration 9 T6 where a helper was correct, tested and
# unreachable.
#
# So there are two clients and each test names the one it wants. A reader can see
# from the signature whether a test authenticates, which is the property that
# makes the negative suite in `tests/test_auth.py` meaningful.

#: The identity the suite presents. Not a secret and not reused anywhere: it
#: exists only inside tests, and `QUERYPILOT_USERS` is set per-test rather than
#: read from the developer's environment.
TEST_USER = "test-analyst"
TEST_SECRET = "test-secret-not-used-anywhere-else"


@pytest.fixture
def authed_client(monkeypatch):
    """A `TestClient` presenting valid Basic credentials.

    **At T3 this authenticates against a gate that does not exist yet**, which is
    deliberate: the credential logic landed unwired at T2, the call sites learn
    to carry credentials here, and only then does T4 turn the gate on. Wiring it
    first would have put 100 call sites in the red at once.

    The consequence worth stating plainly: **this fixture proves nothing until
    T4.** Passing tests here mean the credentials are syntactically fine, not
    that anything checks them. T4's mutation -- remove the middleware and require
    `tests/test_auth.py` to go red -- is what proves the gate.

    Sets `QUERYPILOT_USERS` because that is where the app will look for its
    credential map. T4 adds whatever step is needed to make the running app
    re-read it; that step belongs with the mechanism, not here.
    """
    import json

    from fastapi.testclient import TestClient

    from api.http.auth import USERS_ENV
    from api.main import app

    monkeypatch.setenv(USERS_ENV, json.dumps({TEST_USER: TEST_SECRET}))
    client = TestClient(app)
    # Assigned rather than passed to the constructor: this `TestClient` takes no
    # `auth=` keyword, and `httpx` does the Basic encoding itself -- a hand-built
    # `Authorization: Basic <base64>` header would be a second implementation of
    # something the library already gets right.
    client.auth = (TEST_USER, TEST_SECRET)
    return client


@pytest.fixture
def anonymous_client(monkeypatch):
    """A `TestClient` presenting nothing, for the tests that must be refused.

    **Named `anonymous_client` rather than `client` on purpose.** A fixture
    called `client` is what a new test asks for without thinking, and it would
    hand back an unauthenticated one that works today and fails confusingly the
    first time the test touches a protected route. The name is the warning.

    The credential map is still configured, so a refusal here is the gate saying
    *these credentials are wrong*, not the app saying *I have no credentials at
    all*. Those are different failures and `tests/test_auth.py` covers both.
    """
    import json

    from fastapi.testclient import TestClient

    from api.http.auth import USERS_ENV
    from api.main import app

    monkeypatch.setenv(USERS_ENV, json.dumps({TEST_USER: TEST_SECRET}))
    return TestClient(app)
