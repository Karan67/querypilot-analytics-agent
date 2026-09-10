"""Iteration 7 T5 — `GET /quota`, and the two things the page now says.

AC10 and AC11 exist because of a measurement, not an intuition.
`010-hardening.md` §2.3 timed twelve questions of near-identical size and
watched latency climb from 742ms to 10,393ms. There was no 429, no error, and
no header a user could have been shown: the provider does not refuse when its
per-minute token bucket drains, it slows down. §2.3 calls that a silent penalty
and this task is the end of it.

Two properties carry the weight here.

`test_a_stale_reading_is_never_reported_low` is the honest half. The bucket
refills in sixty seconds, so a warning drawn from a five-minute-old sample
describes a state that has not existed for four minutes.

`test_quota_never_calls_the_provider` is the other. Asking the provider how much
quota is left would spend quota, which is the kind of instrument that changes
what it measures.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.agent.orchestrator import AgentResult
from api.db.execution import ExecutionResult
from api.http import quota
from api.llm.base import TokenUsage
from api.llm.rate_limits import Bucket, RateLimitSnapshot
from api.main import app


@pytest.fixture
def client():
    return TestClient(app)


#: The measured minute bucket (B-1, 2026-09-04).
TOKENS_PER_MINUTE = 8000


def _snapshot(remaining: int, limit: int = TOKENS_PER_MINUTE, window: float = 60.0):
    """A snapshot whose numbers are internally consistent.

    `reset_seconds` is derived from `remaining` rather than picked, so the
    window `Bucket` computes actually comes out at sixty seconds. Inventing an
    unrelated reset time would produce a snapshot no provider could send and
    would quietly test `describe_window` against nonsense.
    """
    return RateLimitSnapshot(
        tokens=Bucket(
            "tokens",
            limit=limit,
            remaining=remaining,
            reset_seconds=window * (limit - remaining) / limit,
        ),
        requests=Bucket("requests", limit=1000, remaining=900, reset_seconds=8640.0),
        raw={"x-ratelimit-limit-tokens": str(limit)},
    )


class _FakeProvider:
    """A provider that reports limits and nothing else interesting."""

    model = "openai/gpt-oss-120b"

    def __init__(self, snapshot=None):
        self.last_rate_limit = snapshot

    def complete(self, system, user):  # pragma: no cover - the agent is faked
        return "ACTION: execute_sql SELECT 1"


def _ask(monkeypatch, snapshot=None, question="how many tracks?", client=None):
    """Drive one question with a provider carrying `snapshot`."""
    monkeypatch.setattr("api.main.get_provider", lambda: _FakeProvider(snapshot))
    monkeypatch.setattr(
        "api.main.answer",
        lambda question, **_: AgentResult(
            ok=True,
            question=question,
            sql="SELECT count(*) FROM track",
            result=ExecutionResult(ok=True, columns=("count",), rows=((3503,),)),
            attempts_used=1,
            usage=TokenUsage(prompt_tokens=1000, completion_tokens=69, calls=1, measured=True),
        ),
    )
    return client.post("/ask", json={"question": question})


# --- absence is reported as absence -----------------------------------------


def test_quota_is_unknown_before_any_question(client):
    """**No numbers at all until a real call has produced some.**

    A default of "8,000 remaining" would be a fabrication, and one that reads
    exactly like a healthy reading -- the reader could not tell a measurement
    from a placeholder, which is the property `usage_measured` exists to protect
    everywhere else in this iteration.
    """
    body = client.get("/quota").json()

    assert body["known"] is False
    assert body["tokens"] is None
    assert body["requests"] is None
    assert body["low"] is False
    assert body["note"] == ""


def test_the_endpoint_is_always_200_even_knowing_nothing(client):
    """A low bucket is not a service failure and neither is an empty one.

    The answer still arrives, it arrives more slowly, and AC11 is about saying
    so rather than about starting to refuse.
    """
    assert client.get("/quota").status_code == 200

    quota.observe(_snapshot(remaining=100))
    assert client.get("/quota").status_code == 200


def test_the_payload_invents_no_daily_token_bucket(client):
    """**Two buckets, not three, and the third is the one that bites.**

    B-1 measured a per-minute token limit and a per-day request limit in the
    headers, and a 200,000 tokens-per-day limit that appears in **no header at
    all** -- it surfaces only in the body of a 429. Reporting a figure for it
    would repeat B-1's original error in the opposite direction: that task's
    first conclusion was that the daily limit did not exist, because no header
    mentioned it.

    Pinned as an exact key set, so a well-meaning addition has to come past
    this test and read the paragraph above.
    """
    quota.observe(_snapshot(remaining=4000))
    body = client.get("/quota").json()

    assert set(body) == {
        "known",
        "observed_at",
        "age_seconds",
        "stale",
        "low",
        "note",
        "tokens",
        "requests",
    }
    assert set(body["tokens"]) == {"limit", "remaining", "reset_seconds", "window"}


# --- what a question teaches it ---------------------------------------------


def test_a_question_records_what_the_provider_said(client, monkeypatch):
    """The snapshot has to be kept somewhere that outlives the request.

    T4 made the endpoint build a provider per request, so `last_rate_limit` dies
    with the request that observed it. Before T5 there was nowhere for it to go.
    """
    _ask(monkeypatch, _snapshot(remaining=5120), client=client)

    body = client.get("/quota").json()
    assert body["known"] is True
    assert body["tokens"]["remaining"] == 5120
    assert body["tokens"]["limit"] == TOKENS_PER_MINUTE
    assert body["requests"]["remaining"] == 900


def test_the_window_is_derived_rather_than_declared(client, monkeypatch):
    """B-1's property, surfaced. The window comes out of the arithmetic between
    limit, remaining and reset time, so it survives a provider changing tiers --
    a hardcoded "per minute" would not."""
    _ask(monkeypatch, _snapshot(remaining=2000), client=client)

    body = client.get("/quota").json()
    assert body["tokens"]["window"] == "per minute"
    assert body["requests"]["window"] == "per day"


def test_a_cache_hit_does_not_erase_the_last_reading(client, monkeypatch):
    """A cache hit builds no provider, so there is nothing to observe.

    **This test originally claimed to cover `observe(None)` and did not.** The
    mutation that made `observe` store a `None` snapshot left it green, because
    a hit never reaches `observe` at all -- `ask()` guards on `provider is not
    None` first. The guarantee here is real and worth pinning; it is just a
    different guarantee, and the one it was named for now has its own test
    below.
    """
    _ask(monkeypatch, _snapshot(remaining=1000), question="repeated?", client=client)
    assert client.get("/quota").json()["low"] is True

    second = _ask(monkeypatch, None, question="repeated?", client=client)
    assert second.json()["cache_hit"] is True

    body = client.get("/quota").json()
    assert body["known"] is True, "the last real reading must survive a cache hit"
    assert body["tokens"]["remaining"] == 1000
    assert body["low"] is True


def test_a_provider_that_reports_nothing_does_not_erase_the_last_reading(
    client, monkeypatch
):
    """**`observe(None)` is ignored rather than stored**, tested on the path
    that actually reaches it.

    A *miss* whose provider carries no telemetry -- a response without the
    headers, or a provider that never grew the attribute -- calls `observe`
    with `None`. Writing that absence over the last real reading would turn *we
    have not looked lately* into *we looked and there was nothing*. Those are
    different claims, and the second one silently switches the warning off at
    the moment it is most likely to be needed.
    """
    _ask(monkeypatch, _snapshot(remaining=900), question="first?", client=client)
    assert client.get("/quota").json()["tokens"]["remaining"] == 900

    # A different question, so this is a miss and a provider really is built.
    _ask(monkeypatch, None, question="a different question?", client=client)

    body = client.get("/quota").json()
    assert body["known"] is True, "an uninformative call must not erase what we knew"
    assert body["tokens"]["remaining"] == 900


# --- low, and the honesty around it -----------------------------------------


def test_a_low_bucket_is_reported_low(client):
    quota.observe(_snapshot(remaining=quota.LOW_TOKENS - 1))
    body = client.get("/quota").json()

    assert body["low"] is True
    assert body["note"]


def test_a_healthy_bucket_says_nothing(client):
    """No banner when there is nothing to explain. A warning that is always on
    is one nobody reads by the time it matters."""
    quota.observe(_snapshot(remaining=quota.LOW_TOKENS + 1))
    body = client.get("/quota").json()

    assert body["low"] is False
    assert body["note"] == ""


def test_the_threshold_is_derived_from_the_measured_cost_of_a_question():
    """Not a round number somebody liked.

    §2.3 held twelve questions between 1,047 and 1,256 tokens, and three live
    questions on 2026-09-09 cost 1,047, 1,077 and 1,134. The threshold is two of
    those, which is the point past which the *next* question plausibly does not
    fit in the bucket.
    """
    assert quota.LOW_TOKENS == 2 * quota.TYPICAL_QUESTION_TOKENS
    assert 1047 <= quota.TYPICAL_QUESTION_TOKENS <= 1256


def test_the_note_reports_the_measurement_not_only_an_alarm(client):
    """A user told "this may be slow" learns nothing they cannot already see.

    The sentence has to carry the numbers, so it explains *why* and implies the
    remedy -- the bucket refills on its own.
    """
    quota.observe(_snapshot(remaining=1200))
    note = client.get("/quota").json()["note"]

    assert "1,200" in note, "the note must say what is actually left"
    assert "8,000" in note, "...and what it is out of"


def test_a_stale_reading_is_never_reported_low():
    """**The reading expires, and saying so is the point.**

    The minute bucket refills in sixty seconds. A warning drawn from a
    five-minute-old sample would describe a bucket that has been full for four
    minutes -- technically a real measurement, and a lie about the present.

    Guards the mutation of dropping the staleness check, which leaves the banner
    stuck on after a single busy moment until another question is asked.
    """
    quota.observe(_snapshot(remaining=200))
    now = datetime.now(timezone.utc)

    fresh = quota.snapshot(now=now)
    assert fresh["low"] is True
    assert fresh["stale"] is False

    later = quota.snapshot(now=now + timedelta(seconds=300))
    assert later["stale"] is True
    assert later["low"] is False, "a stale reading must not drive a warning"
    assert later["known"] is True, "but it is still a reading, and still reported"
    assert later["tokens"]["remaining"] == 200


def test_the_reading_stays_valid_inside_its_own_window():
    """The other side of the same rule: freshness is measured against the
    bucket's own reset time, not a constant somebody picked."""
    quota.observe(_snapshot(remaining=400))
    now = datetime.now(timezone.utc)

    # reset_seconds for remaining=400 of 8,000 over a 60s window is 57s.
    assert quota.snapshot(now=now + timedelta(seconds=5))["low"] is True


# --- the instrument does not change what it measures ------------------------


def test_quota_never_calls_the_provider(client, monkeypatch):
    """Asking the provider how much quota is left would spend quota."""
    def no_calls(*_args, **_kwargs):
        raise AssertionError("/quota must not build or call a provider")

    monkeypatch.setattr("api.main.get_provider", no_calls)
    quota.observe(_snapshot(remaining=3000))

    assert client.get("/quota").status_code == 200
    assert client.get("/quota").json()["tokens"]["remaining"] == 3000


def test_the_quota_module_does_not_pace():
    """Resolved Q-C, and `009` AC5. **The deployed API does not sleep.**

    A module that knows how drained the bucket is, is exactly where somebody
    would reach for a sleep. Telling the user what is happening is this
    iteration's answer; waiting on their behalf is not. Comments are stripped
    first -- the repository rule after that trap appeared four times.
    """
    import ast
    import pathlib

    source = pathlib.Path("api/http/quota.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "sleep" not in called
    assert not any("pacing" in (n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom))


# --- the page ---------------------------------------------------------------


def _javascript_code(source: str) -> str:
    """`source` with comments removed; line comments only at the start of a
    line, because `//` also occurs inside string literals in this file."""
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return "\n".join(
        line for line in without_blocks.splitlines() if not line.strip().startswith("//")
    )


def test_the_quota_banner_starts_hidden_in_the_served_markup(client):
    """A declarative default, following the chart toggle.

    A default expressed only in a script is one no test can see without a
    browser, and it can drift from what the script later sets.
    """
    page = client.get("/").text
    banner = page[page.index('id="quota"') :][:60]
    assert "hidden" in banner
    assert 'aria-live="polite"' in banner, "it appears without warning; announce it"


def test_the_cache_note_starts_hidden_in_the_served_markup(client):
    page = client.get("/").text
    note = page[page.index('id="cache-note"') :][:60]
    assert "hidden" in note


def test_the_page_asks_the_server_for_quota(client):
    code = _javascript_code(client.get("/static/app.js").text)
    assert '"/quota"' in code
    assert "refreshQuota" in code


def test_the_warning_text_comes_from_the_server(client):
    """One place decides the wording.

    The server measured the bucket and knows the numbers; a sentence assembled
    in the browser would be a second copy of that judgement, free to drift from
    the one `/quota` returns and from the thresholds it was derived from.

    **Asserts the assignment, not the mention.** The first version checked that
    `quota.note` appeared anywhere in the file, and a mutation that replaced the
    banner's text with a hardcoded "Quota is low." left it green -- the guard
    clause above the assignment still referenced `quota.note`. A name appearing
    in a file says nothing about whether it reaches the screen.
    """
    code = _javascript_code(client.get("/static/app.js").text)
    assert "quotaBox.textContent = quota.note" in code


def test_the_page_marks_a_cached_answer(client):
    """AC8's other half, which the payload alone did not satisfy: `cache_hit`
    was in the response since T3 and nothing rendered it, so the page presented
    a reused answer exactly as it presented a fresh one."""
    code = _javascript_code(client.get("/static/app.js").text)
    assert "cache_hit" in code

    # The *call*, not merely the definition. A test satisfied by a function
    # nobody invokes would pass with the page silently unchanged, which is the
    # exact defect this test exists to prevent.
    render_body = code[code.index("function render(body)") :]
    render_body = render_body[: render_body.index("\n}")]
    assert "renderCacheNote(body)" in render_body


def test_the_page_still_loads_nothing_remote(client):
    """T5 added a fetch and two elements, and it must not have added a CDN.

    Re-asserted here rather than left to the Iteration 6 test, because this is
    the task that touched the page.
    """
    page = client.get("/").text
    assert "https://" not in page
    assert "cdn" not in page.lower()
