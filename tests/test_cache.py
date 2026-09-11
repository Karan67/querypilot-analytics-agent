"""Iteration 7 T4 — the answer cache, and the fingerprints that make it safe.

**Every hit is proved by a call that did not happen, never by a stopwatch.** A
cache that still calls the provider is invisible to timing — the second request
is faster anyway, because the first one warmed a connection pool and a schema
lookup — and obvious to a counter. The plan made that a requirement rather than
a preference, and it is the only reason the headline test asserts token spend
instead of milliseconds.

The fingerprint tests live here rather than in a file of their own because they
exist for this key. `evals/run_evals.py` has fingerprinted prompts since
Iteration 4 for a different purpose, and T4 moved the recipe into `api/` so both
callers share one definition; the pinned values below are what prove the move
changed nothing.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest
from fastapi.testclient import TestClient

from api.agent.fingerprints import (
    fingerprint,
    live_schema_fingerprint,
    loop_prompt_fingerprint,
)
from api.agent.orchestrator import AgentResult
from api.agent.prompts import SCHEMA_COMPACT, SCHEMA_DDL
from api.agent.single_shot import CATEGORY_NO_SQL
from api.db.execution import ExecutionResult
from api.http import cache
from api.llm.base import TokenUsage
from api.main import app
from api.store import history


@pytest.fixture
def client():
    return TestClient(app)


def _always(_value) -> bool:
    return True


# --- the key: exact text, and both fingerprints ------------------------------


def test_the_key_is_the_exact_question_text():
    """**No normalisation of any kind**, which is a decision rather than an
    omission.

    Case folding or whitespace stripping is a guess about which differences the
    model would have ignored, and when the guess is wrong the answer served is
    indistinguishable from a correct one. Two questions that differ at all are
    two questions.
    """
    base = cache.cache_key("How many tracks?", "s", "p")

    assert cache.cache_key("how many tracks?", "s", "p") != base
    assert cache.cache_key("How many tracks? ", "s", "p") != base
    assert cache.cache_key("How  many tracks?", "s", "p") != base
    assert cache.cache_key("How many tracks?", "s", "p") == base


def test_a_schema_change_changes_the_key():
    """The reason the schema is in the key at all.

    Without this the cache would happily serve an answer derived from a schema
    that no longer describes the database -- correct when it was computed, wrong
    now, and with nothing to distinguish the two.
    """
    assert cache.cache_key("q", "schema-v1", "p") != cache.cache_key("q", "schema-v2", "p")


def test_a_prompt_change_changes_the_key():
    """Same argument for the prompt. An edited prompt is a different system, and
    a process that outlives the edit must not keep answering as the old one."""
    assert cache.cache_key("q", "s", "prompt-v1") != cache.cache_key("q", "s", "prompt-v2")


def test_the_separator_prevents_a_boundary_collision():
    """Concatenation alone collides, and the collision is not exotic.

    `"ab" + "c"` and `"a" + "bc"` are the same string, so a question ending in a
    character that the next field begins with would key identically to a
    different question under a different configuration. Guards the mutation of
    joining with `""`.
    """
    assert cache.cache_key("ab", "c", "p") != cache.cache_key("a", "bc", "p")
    assert cache.cache_key("q", "ab", "c") != cache.cache_key("q", "a", "bc")


# --- computing at most once --------------------------------------------------


class _Counter:
    """Counts computations, thread-safely, incrementing *before* any blocking.

    The ordering matters for the single-flight test: a mutation that lets every
    thread compute must show up in the count even though all of those threads
    then block on the same event.
    """

    def __init__(self, value="answer", block: threading.Event | None = None):
        self.calls = 0
        self._value = value
        self._block = block
        self._lock = threading.Lock()
        self.entered = threading.Event()

    def __call__(self):
        with self._lock:
            self.calls += 1
        self.entered.set()
        if self._block is not None:
            self._block.wait(timeout=10)
        return self._value


def test_the_second_ask_does_not_compute():
    counter = _Counter()

    first, first_hit = cache.get_or_compute("k", counter, _always)
    second, second_hit = cache.get_or_compute("k", counter, _always)

    assert counter.calls == 1
    assert (first, second) == ("answer", "answer")
    assert (first_hit, second_hit) == (False, True)


def test_a_different_key_computes_again():
    counter = _Counter()

    cache.get_or_compute("k1", counter, _always)
    cache.get_or_compute("k2", counter, _always)

    assert counter.calls == 2


def test_a_value_the_caller_rejects_is_not_kept():
    """`cacheable` is required rather than defaulted, and this is why.

    Applied to answers it means *only successes are cached*, which follows from
    D-3 rather than taste: the cache has no expiry, so a cached failure would be
    served for the life of the process. A question asked during a thirty-second
    rate limit would become permanently unanswerable.
    """
    counter = _Counter()

    cache.get_or_compute("k", counter, lambda _v: False)
    cache.get_or_compute("k", counter, lambda _v: False)

    assert counter.calls == 2
    assert cache.size() == 0


def test_the_cache_is_bounded():
    """Resolved at T4. The key contains raw user text, so an unbounded dict
    grows with input rather than with data.

    Not expiry -- nothing goes stale by clock, and D-3's "no expiry" stands.
    Entries are dropped only when the bound is reached, oldest first.
    """
    for i in range(cache.CACHE_LIMIT + 1):
        cache.get_or_compute(f"k{i}", _Counter(value=i), _always)

    assert cache.size() == cache.CACHE_LIMIT

    recomputed = _Counter(value="recomputed")
    value, hit = cache.get_or_compute("k0", recomputed, _always)
    assert (value, hit) == ("recomputed", False), "the oldest entry should be gone"

    newest, newest_hit = cache.get_or_compute(
        f"k{cache.CACHE_LIMIT}", _Counter(value="unused"), _always
    )
    assert (newest, newest_hit) == (cache.CACHE_LIMIT, True), "the newest should remain"


# --- single-flight (resolved Q-B) -------------------------------------------


def test_identical_questions_in_flight_share_one_computation():
    """**Resolved Q-B.** Without this the cache helps only questions repeated
    *slowly*, which is the opposite of the load worth helping.

    The leader is held inside `compute` while seven more callers arrive. If the
    guard were removed all eight would compute -- and `_Counter` increments
    before it blocks, so the mutation shows up in the count rather than in a
    hang.
    """
    release = threading.Event()
    counter = _Counter(block=release)
    arrived = threading.Semaphore(0)

    def ask():
        arrived.release()
        return cache.get_or_compute("k", counter, _always)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        leader = pool.submit(ask)
        assert counter.entered.wait(timeout=5), "the leader never started computing"

        followers = [pool.submit(ask) for _ in range(7)]
        for _ in range(8):
            assert arrived.acquire(timeout=5)
        time.sleep(0.2)

        assert counter.calls == 1, "a second caller started its own computation"
        assert not any(f.done() for f in followers), "a follower answered on its own"

        release.set()
        results = [f.result(timeout=5) for f in [leader, *followers]]

    assert counter.calls == 1
    assert all(value == "answer" for value, _ in results)
    assert sum(1 for _, hit in results if not hit) == 1, "exactly one caller paid"


def test_a_different_question_is_not_made_to_wait():
    """Guards the lazy mutation: single-flighting everything behind one lock.

    A key that is not in flight must not be delayed by one that is, or the cache
    becomes a global bottleneck the moment a slow question arrives.
    """
    release = threading.Event()
    blocked = _Counter(block=release)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(cache.get_or_compute, "slow", blocked, _always)
        assert blocked.entered.wait(timeout=5)

        other = pool.submit(cache.get_or_compute, "other", _Counter(value="fast"), _always)
        assert other.result(timeout=5) == ("fast", False)

        release.set()
        slow.result(timeout=5)


def test_a_failing_computation_reaches_every_waiter():
    """The leader's exception is the followers' exception -- they asked for the
    same thing and there is no second answer to give them."""
    release = threading.Event()
    started = threading.Event()

    def explode():
        started.set()
        release.wait(timeout=10)
        raise RuntimeError("provider exploded")

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(cache.get_or_compute, "k", explode, _always)]
        assert started.wait(timeout=5)
        futures += [pool.submit(cache.get_or_compute, "k", explode, _always) for _ in range(3)]
        time.sleep(0.2)
        release.set()

        for future in futures:
            with pytest.raises(RuntimeError, match="provider exploded"):
                future.result(timeout=5)


def test_a_failing_computation_does_not_strand_the_key():
    """**The deadlock this would otherwise be.** If the in-flight entry survived
    a raised exception, every later caller would wait forever on an event that
    nothing will ever set -- and the question would be permanently unanswerable
    rather than merely failed once.
    """
    def explode():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        cache.get_or_compute("k", explode, _always)

    value, hit = cache.get_or_compute("k", _Counter(value="recovered"), _always)
    assert (value, hit) == ("recovered", False)


# --- the endpoint: a hit costs nothing ---------------------------------------


class _CountingAgent:
    """Stands in for `answer()`, counting how many times the agent ran.

    The agent is the only caller of the provider, so its call count *is* the
    provider call count -- and unlike a fake provider it needs no database. The
    live check against the container closes the remaining gap.
    """

    def __init__(self, ok=True, tokens=1069):
        self.calls = 0
        self._ok = ok
        self._tokens = tokens

    def __call__(self, question, **_kwargs):
        self.calls += 1
        if not self._ok:
            return AgentResult(ok=False, question=question, category=CATEGORY_NO_SQL)
        return AgentResult(
            ok=True,
            question=question,
            sql="SELECT count(*) FROM track",
            result=ExecutionResult(ok=True, columns=("count",), rows=((3503,),)),
            attempts_used=1,
            usage=TokenUsage(
                prompt_tokens=self._tokens - 69,
                completion_tokens=69,
                calls=1,
                measured=True,
            ),
        )


def test_a_repeated_question_costs_zero_tokens(client, monkeypatch):
    """**The headline, and the criterion the plan insisted on.**

    Not a stopwatch. The second request would be faster with no cache at all --
    a warm pool, a warm schema lookup -- so timing cannot tell a hit from a
    lucky miss. A provider call either happened or it did not.
    """
    agent = _CountingAgent()
    monkeypatch.setattr("api.main.answer", agent)

    first = client.post("/ask", json={"question": "how many tracks?"}).json()
    second = client.post("/ask", json={"question": "how many tracks?"}).json()

    assert agent.calls == 1, "the second question was answered by the provider again"

    assert first["cache_hit"] is False
    assert first["usage"]["total_tokens"] == 1069
    assert first["usage"]["calls"] == 1

    assert second["cache_hit"] is True
    assert second["usage"]["total_tokens"] == 0, "a hit spends nothing"
    assert second["usage"]["calls"] == 0
    assert second["provider_ms"] == 0

    assert second["rows"] == first["rows"], "and it is the same answer"
    assert second["sql"] == first["sql"]


def test_a_hit_gets_its_own_id_and_is_recorded_as_a_hit(client, monkeypatch):
    """A hit is still a question somebody asked, so it is still a row.

    It gets a **new** id: feedback attaches to an answer given to a person, and
    two people who received the same cached answer may judge it differently.
    """
    monkeypatch.setattr("api.main.answer", _CountingAgent())

    first = client.post("/ask", json={"question": "how many tracks?"}).json()
    second = client.post("/ask", json={"question": "how many tracks?"}).json()

    assert first["id"] != second["id"]

    rows = {row["id"]: row for row in history.recent()}
    assert len(rows) == 2
    assert rows[first["id"]]["cache_hit"] == 0
    assert rows[second["id"]]["cache_hit"] == 1


def test_the_recorded_tokens_sum_to_what_was_billed(client, monkeypatch):
    """**Resolved at T4**, and the reason a hit records zero rather than
    replaying the original figures.

    Summing the column has to give what the provider actually charged, with no
    filter to remember. A total that silently overstates spend is the same class
    of defect as the one that understated it and cost a hand reconstruction.
    """
    monkeypatch.setattr("api.main.answer", _CountingAgent(tokens=1069))

    for _ in range(4):
        client.post("/ask", json={"question": "how many tracks?"})

    rows = history.recent()
    assert len(rows) == 4
    assert sum(row["total_tokens"] for row in rows) == 1069
    assert sum(row["provider_calls"] for row in rows) == 1
    assert sum(row["cache_hit"] for row in rows) == 3


def test_a_failed_question_is_asked_again(client, monkeypatch):
    """End to end, the rule `_is_cacheable` encodes: a failure is not kept.

    With no expiry, caching this would make the question unanswerable until the
    process restarted -- including after whatever caused the failure was fixed.
    """
    agent = _CountingAgent(ok=False)
    monkeypatch.setattr("api.main.answer", agent)

    client.post("/ask", json={"question": "unanswerable"})
    second = client.post("/ask", json={"question": "unanswerable"})

    assert agent.calls == 2
    assert second.json()["cache_hit"] is False


def test_a_cache_hit_does_not_build_a_provider(client, monkeypatch):
    """**The provider is never constructed on a hit**, not merely unused.

    Which means a cached answer survives a provider outage or a revoked key --
    and it is what makes `provider_ms` an honest zero rather than a timer that
    was started and never used.
    """
    monkeypatch.setattr("api.main.answer", _CountingAgent())
    client.post("/ask", json={"question": "how many tracks?"})

    def no_provider_today(*_args, **_kwargs):
        raise AssertionError("a hit must not reach the provider factory")

    monkeypatch.setattr("api.main.get_provider", no_provider_today)

    body = client.post("/ask", json={"question": "how many tracks?"}).json()
    assert body["cache_hit"] is True
    assert body["rows"] == [[3503]]


def test_the_row_records_which_schema_and_prompt_produced_it(client, monkeypatch):
    """The two columns have existed since T2 and were always empty. They are the
    same two numbers the key is built from, which is what lets a stored answer
    still be interpreted after either one moves."""
    monkeypatch.setattr("api.main.answer", _CountingAgent())
    client.post("/ask", json={"question": "how many tracks?"})

    row = history.recent()[0]
    assert row["prompt_fp"] == loop_prompt_fingerprint(glossary=True)
    assert len(row["schema_fp"]) == 12


# --- the coupling that has no symptom ----------------------------------------


def test_the_fingerprinted_configuration_is_the_one_that_is_sent():
    """**A mismatch here would have no symptom at all.**

    If `answer()` were called with the glossary on while the key were built with
    it off, both halves would keep working: answers would be correct and hits
    would be served. They would simply be hits on a prompt the API does not
    send. One constant, used by both, is the fix; this is the test that keeps it
    that way.
    """
    import inspect

    from api.agent.orchestrator import answer
    from api.main import _DEPLOYED_GLOSSARY, _DEPLOYED_RENDERING

    source = inspect.getsource(__import__("api.main", fromlist=["_answer_or_replay"])._answer_or_replay)
    assert "glossary=_DEPLOYED_GLOSSARY" in source
    assert "rendering=_DEPLOYED_RENDERING" in source

    # And the constants say what the deployment actually ships.
    assert _DEPLOYED_GLOSSARY is True
    assert _DEPLOYED_RENDERING == SCHEMA_COMPACT
    assert inspect.signature(answer).parameters["glossary"].default is True


# --- fingerprints: the recipe moved, the numbers did not ---------------------


def test_the_loop_fingerprint_is_unchanged_by_the_move():
    """T4 moved the recipe from `evals/run_evals.py` into `api/`, because the
    cache needs it and `evals/` is not in the Docker build context.

    All three loop entries in `EVALS.md` carry `0d280c367c5e`. If the move had
    changed the hashed material, every recorded number would have been filed
    under a fingerprint that no longer reproduces.
    """
    assert loop_prompt_fingerprint(glossary=False) == "0d280c367c5e"


def test_the_eval_harness_and_the_api_agree():
    """One definition, not two that happen to match today.

    Two copies of this recipe would be free to drift in the direction nothing
    fails: a prompt edit could move the cache key and not the recorded
    fingerprint, and the first symptom would be an `EVALS.md` row that looks
    comparable and is not.
    """
    from evals.run_evals import STRATEGY_LOOP, fingerprint_for

    for glossary in (False, True):
        assert fingerprint_for(STRATEGY_LOOP, glossary=glossary) == loop_prompt_fingerprint(
            glossary=glossary
        )


def test_the_glossary_changes_the_prompt_fingerprint():
    """It adds 178 tokens to every call, so a glossary-on run is not the same
    prompt. `loop_prompt_fingerprint` takes it keyword-only with **no default**
    for this reason: the eval harness wants Iteration 4's configuration and the
    API ships the glossary on, and a default would silently record one caller's
    answers under the other's fingerprint."""
    assert loop_prompt_fingerprint(glossary=True) != loop_prompt_fingerprint(glossary=False)

    with pytest.raises(TypeError):
        loop_prompt_fingerprint()


def test_the_live_schema_fingerprint_moves_with_the_database(schema):
    """**Different from `evals.run_evals.schema_fingerprint`, deliberately.**

    That one hashes a small hand-built schema to identify the *renderer*, so a
    reseed cannot masquerade as a prompt change. This one hashes the real schema
    as rendered, because a cache key exists to protect correctness: if a column
    is added, every cached answer was derived from a description of the database
    that is no longer true.
    """
    import dataclasses

    before = live_schema_fingerprint(schema)

    widened = dataclasses.replace(schema, tables=schema.tables[:-1])
    assert live_schema_fingerprint(widened) != before, "a schema change must move it"

    assert live_schema_fingerprint(schema) == before, "and it must be stable otherwise"


def test_the_rendering_is_part_of_the_schema_fingerprint(schema):
    """Two renderings describe the same tables differently, and the model reads
    the rendering, not the `Schema` object."""
    assert live_schema_fingerprint(schema, SCHEMA_DDL) != live_schema_fingerprint(
        schema, SCHEMA_COMPACT
    )


def test_the_fingerprint_is_derived_not_declared():
    """A version constant somebody bumps fails in the situation that matters:
    the prompt changes, the constant does not, and every number afterwards is
    filed under the wrong version."""
    assert fingerprint("a") != fingerprint("b")
    assert fingerprint("a") == fingerprint("a")
    assert len(fingerprint("a")) == 12


# --- benchmark integrity: the risk the plan named and nothing tested ---------


def test_the_eval_runner_cannot_reach_the_answer_cache():
    """**`010`'s plan §7 named this as a risk and the iteration never tested it.**

    Its words: *the eval runner must not share the process-level cache with the
    API -- D-1 keeps them apart.* If it could, a repeated question in a
    multi-pass run would return a stored answer, and the benchmark would be
    measuring the cache instead of the model. A 100% second pass would look like
    stability and be an artefact.

    Asserted structurally rather than by running the harness, because the
    property is *reachability*: it must hold for every code path through
    `evals/`, not only the ones a test happens to exercise. `api/http/cache.py`
    is imported by exactly one module, and that module is the HTTP surface.
    """
    import ast
    import pathlib

    importers = []
    for path in sorted(pathlib.Path(".").glob("**/*.py")):
        parts = path.parts
        if not parts or parts[0] not in {"api", "evals"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
                modules += [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                modules += [a.name for a in node.names]
            if any(m == "api.http.cache" or m.endswith("http.cache") for m in modules):
                importers.append(path.as_posix())

    assert importers == ["api/main.py"], (
        f"the answer cache is reachable from {importers}; D-1 requires the "
        f"benchmark and the product's cache to stay apart"
    )


def test_the_schema_path_is_shared_with_the_eval_runner_deliberately():
    """The other half, and the asymmetry is the point.

    The orchestrator reads the schema through the same function an eval run
    does, so both see one definition of what the database looks like. That **is**
    shared, and it is fine for the reason the answer cache above is not: it
    returns the same schema either way, so it changes no answer, no token count
    and no recorded number. `EVALS.md` records no latency, so there is nothing
    for it to perturb.

    **This test was named after `schema_cache` until Iteration 9 T4 retired that
    module (B-14), and it was updated rather than deleted.** The asymmetry it
    pins is a property of the project, not of the cache: the answer cache must
    stay unreachable from `evals/`, and the schema path must stay shared with it.
    Deleting an assertion because the thing it named moved is how a guarantee
    quietly narrows -- `HANDOFF.md` §6 records five absence-assertions that
    rotted this way.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("api/agent/orchestrator.py").read_text(encoding="utf-8"))
    imported = {
        f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    assert "api.db.introspection.get_schema" in imported, (
        f"the orchestrator no longer reads the schema through the shared "
        f"introspection path; its api.db imports are "
        f"{sorted(m for m in imported if m.startswith('api.db'))}"
    )
