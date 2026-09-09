"""Iteration 7 T3 — the answer's cost and trace survive the API boundary.

Separate from `test_ask_endpoint.py` because the subject is different: that file
asks whether the endpoint answers correctly, this one asks whether answering
leaves a record. The two failure modes are independent — an endpoint can be
perfectly correct and completely amnesiac, which is precisely what it was until
this task.

The test that carries the weight is `test_usage_survives_the_api_boundary`.
`AgentResult.usage` has existed since Iteration 4 and the boundary threw it
away; the consequence was concrete, not theoretical — a 20,370-token gap in the
spend ledger on 2026-09-09 had to be reconstructed by hand from three
instrumented probes, because the one component that knew what a question cost
never said so.
"""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.agent.orchestrator import AgentResult, Step
from api.agent.single_shot import CATEGORY_NO_SQL
from api.db.execution import CATEGORY_DATABASE_ERROR, ExecutionResult
from api.http.errors import STATUS_ANSWERED
from api.llm.base import TokenUsage
from api.main import app
from api.store import history


@pytest.fixture
def client():
    return TestClient(app)


def _answer(monkeypatch, result: AgentResult) -> None:
    # `**_` absorbs `provider=`, which T3 added so the endpoint can time
    # `complete()` separately (AC2).
    monkeypatch.setattr("api.main.answer", lambda question, **_: result)


def _ok(columns, rows, sql="SELECT 1", steps=(), usage=None) -> AgentResult:
    return AgentResult(
        ok=True,
        question="q",
        sql=sql,
        result=ExecutionResult(ok=True, columns=tuple(columns), rows=tuple(rows)),
        steps=tuple(steps),
        attempts_used=1,
        usage=usage or TokenUsage(),
    )


def _failed(category: str) -> AgentResult:
    return AgentResult(ok=False, question="q", category=category)


# --- AC1: the cost crosses the boundary -------------------------------------


def test_usage_survives_the_api_boundary(client, monkeypatch):
    """The defect this task exists to fix."""
    _answer(monkeypatch, _ok(
        ["count"], [[3503]],
        usage=TokenUsage(prompt_tokens=1000, completion_tokens=69, calls=1, measured=True),
    ))

    body = client.post("/ask", json={"question": "how many?"}).json()
    assert body["usage"]["total_tokens"] == 1069
    assert body["usage"]["prompt_tokens"] == 1000
    assert body["usage"]["completion_tokens"] == 69
    assert body["usage"]["calls"] == 1


def test_the_payload_says_which_instrument_measured_the_cost(client, monkeypatch):
    """D-1's standing rule, carried across the boundary. A billed number and a
    locally counted one are different quantities, and a payload that does not
    say which it holds is not reporting a measurement."""
    for measured in (False, True):
        _answer(monkeypatch, _ok(
            ["count"], [[1]],
            usage=TokenUsage(prompt_tokens=10, completion_tokens=1, calls=1,
                             measured=measured),
        ))
        body = client.post("/ask", json={"question": "?"}).json()
        assert body["usage"]["measured"] is measured


def test_the_cost_reaches_the_store_as_well_as_the_payload(client, monkeypatch):
    """Returning it and recording it are different guarantees, and only one of
    them survives the browser tab being closed."""
    _answer(monkeypatch, _ok(
        ["count"], [[1]],
        usage=TokenUsage(prompt_tokens=900, completion_tokens=100, calls=1, measured=True),
    ))
    client.post("/ask", json={"question": "?"})

    row = history.recent()[0]
    assert row["total_tokens"] == 1000
    assert row["prompt_tokens"] == 900
    assert row["usage_measured"] == 1
    assert row["provider_calls"] == 1


# --- AC2: two durations, kept apart -----------------------------------------


def test_the_two_durations_are_reported_separately(client, monkeypatch):
    """010 §2.2 measured the provider at 94.2% of wall clock; a single duration
    would hide the only number that moves."""
    _answer(monkeypatch, _ok(["count"], [[1]]))
    body = client.post("/ask", json={"question": "?"}).json()

    assert "total_ms" in body and "provider_ms" in body
    assert body["total_ms"] >= body["provider_ms"] >= 0


def test_the_durations_are_recorded_not_only_returned(client, monkeypatch):
    _answer(monkeypatch, _ok(["count"], [[1]]))
    client.post("/ask", json={"question": "?"})

    row = history.recent()[0]
    assert row["total_ms"] >= row["provider_ms"] >= 0


# --- AC4: the answer is recorded --------------------------------------------


def test_an_answer_is_recorded_and_returns_its_id(client, monkeypatch):
    """The id Iteration 8's feedback attaches to -- an answer, never a question
    string, because the agent may answer the same question differently next
    time."""
    _answer(monkeypatch, _ok(["count"], [[3503]], sql="SELECT count(*) FROM track"))
    body = client.post("/ask", json={"question": "how many tracks?"}).json()

    assert body["id"]
    rows = history.recent()
    assert len(rows) == 1
    assert rows[0]["id"] == body["id"]
    assert rows[0]["question"] == "how many tracks?"
    assert rows[0]["sql"] == "SELECT count(*) FROM track"
    assert rows[0]["shape"] == "scalar"


def test_a_failed_answer_is_recorded_too(client, monkeypatch):
    """A history that keeps only successes cannot answer *what does it get
    wrong*, which is most of what AC1 is for."""
    _answer(monkeypatch, _failed(CATEGORY_NO_SQL))
    client.post("/ask", json={"question": "unanswerable"})

    row = history.recent()[0]
    assert row["ok"] == 0
    assert row["category"] == CATEGORY_NO_SQL


def test_the_trace_is_recorded_with_the_answer(client, monkeypatch):
    """Charter §1's claim is *read the error, revise*. A record of only the
    final answer cannot show that ever happened."""
    _answer(monkeypatch, _ok(["name"], [["AC/DC"]], steps=(
        Step(attempt=1, action="execute_sql", ok=False,
             category=CATEGORY_DATABASE_ERROR, error="boom", sql="SELECT nope"),
        Step(attempt=2, action="execute_sql", ok=True, sql="SELECT name FROM artist"),
    )))
    body = client.post("/ask", json={"question": "artists?"}).json()

    steps = history.steps_for(body["id"])
    assert [s["attempt"] for s in steps] == [1, 2]
    assert steps[0]["ok"] == 0
    assert steps[0]["error"] == "boom"
    assert steps[1]["ok"] == 1


# --- resolved D-2, end to end -----------------------------------------------


def test_a_broken_store_never_fails_the_answer(client, monkeypatch):
    """**The mutation that matters for T3.** An observability failure must never
    cascade into user-facing downtime: make the store raise, and the question
    must still be answered."""
    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    _answer(monkeypatch, _ok(["count"], [[3503]]))
    monkeypatch.setattr(history, "_connect", explode)

    response = client.post("/ask", json={"question": "how many?"})
    assert response.status_code == STATUS_ANSWERED
    body = response.json()
    assert body["ok"] is True
    assert body["rows"] == [[3503]]
    assert body["id"] is None, "no id when the write failed, and no exception"


def test_a_broken_store_is_reported_by_health(client, monkeypatch):
    """D-2's other half: swallowing the error is right, swallowing it silently
    is not.

    `/health` stays **200**. History is observability, and reporting the service
    as down because logging broke would be the same cascade D-2 prevents, moved
    into the health check.
    """
    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    _answer(monkeypatch, _ok(["count"], [[1]]))
    monkeypatch.setattr(history, "_connect", explode)
    client.post("/ask", json={"question": "?"})

    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["history"]["writable"] is False
    assert "readonly" in body["history"]["error"]
    assert body["status"] == "degraded_history"


def test_health_reports_a_working_store_as_healthy(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["history"]["writable"] is True
    assert body["database"]["connected"] is True


# --- AC8's field, plumbed early ---------------------------------------------


def test_cache_hit_is_present_and_false_before_the_cache_exists(client, monkeypatch):
    """Plumbed in T3 so the contract does not change under the page when T4
    lands. False everywhere until there is a cache to hit."""
    _answer(monkeypatch, _ok(["count"], [[1]]))
    body = client.post("/ask", json={"question": "?"}).json()

    assert body["cache_hit"] is False
    assert history.recent()[0]["cache_hit"] == 0


# --- the wrapper that makes AC2 possible ------------------------------------


def test_the_timing_wrapper_still_exposes_provider_attributes():
    """**The silent failure this would otherwise cause.**

    `usage_for_call()` reads `last_usage` off the provider with `getattr`, and
    `/quota` will read `last_rate_limit`. A wrapper that did not delegate would
    produce unmeasured token counts everywhere — the exact thing D-1 exists to
    prevent, and invisible to any test that does not check `measured`.
    """
    from api.main import _TimedProvider

    class Inner:
        model = "openai/gpt-oss-120b"
        last_usage = "sentinel"

        def complete(self, system, user):
            return "ok"

    wrapped = _TimedProvider(Inner())
    assert wrapped.model == "openai/gpt-oss-120b"
    assert wrapped.last_usage == "sentinel"
    assert wrapped.complete("s", "u") == "ok"
    assert wrapped.calls == 1
    assert wrapped.elapsed_ms >= 0


def test_the_timing_wrapper_times_every_call_including_failures():
    """A wrapper that only counts successful calls under-reports exactly when
    the system is misbehaving, which is when the number is wanted."""
    from api.main import _TimedProvider

    class Boom:
        def complete(self, system, user):
            raise RuntimeError("provider down")

    wrapped = _TimedProvider(Boom())
    with pytest.raises(RuntimeError):
        wrapped.complete("s", "u")
    assert wrapped.calls == 1


def test_the_timing_wrapper_does_not_pace():
    """`009` AC5: the deployed API never sleeps inside a user's request.

    Comments are stripped before asserting — the repository rule, after this
    trap appeared four times in Iteration 6.
    """
    import inspect

    from api.main import _TimedProvider

    source = inspect.getsource(_TimedProvider)
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert "sleep" not in code
    assert "pacing" not in code
