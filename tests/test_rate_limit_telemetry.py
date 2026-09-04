"""Rate-limit telemetry and pacing — charter backlog B-1.

Iteration 5 lost two multi-pass held-out runs to rate limits and could not say
which limit it had hit, because every guard the project owned was denominated in
a *daily token* figure of 200,000 that no provider header reports. The headers
measured against Groq on 2026-09-04 say the binding limit is 8,000 tokens a
minute, and the fixtures below are those exact values.

The load-bearing idea is that **the window is derived, not assumed**:

    refill = (limit - remaining) / reset_seconds
    window = limit / refill

which returned 60.0s for tokens and 86400.0s for requests on live headers. A
test that hardcoded "tokens are per-minute" would pass forever and tell us
nothing the day a tier changes.
"""

from __future__ import annotations

import pytest

from api.llm.base import LLMError, RateLimitError, TokenUsage
from api.llm.pacing import PacedProvider
from api.llm.rate_limits import (
    Bucket,
    RateLimitSnapshot,
    parse_duration,
    snapshot_from_headers,
)

#: Verbatim from a live Groq response, 2026-09-04.
LIVE_HEADERS = {
    "x-ratelimit-limit-requests": "1000",
    "x-ratelimit-limit-tokens": "8000",
    "x-ratelimit-remaining-requests": "931",
    "x-ratelimit-remaining-tokens": "7381",
    "x-ratelimit-reset-requests": "1h39m21.6s",
    "x-ratelimit-reset-tokens": "4.642s",
}


# --- duration parsing --------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("547ms", 0.547),
        ("4.642s", 4.642),
        ("25.687s", 25.687),
        ("1m30s", 90.0),
        ("1h39m21.6s", 5961.6),
        ("1h43m40.799s", 6220.799),
        ("7", 7.0),  # `retry-after` sends a bare integer
    ],
)
def test_provider_durations_parse(text, expected):
    assert parse_duration(text) == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("text", [None, "", "soon", "3 fortnights", "--"])
def test_an_unparseable_duration_is_none_not_an_exception(text):
    """A telemetry reader that raises on an unfamiliar format turns a
    diagnostic aid into an outage."""
    assert parse_duration(text) is None


def test_minutes_and_milliseconds_are_not_confused():
    """The failure that would matter most: `547ms` read as 547 minutes turns a
    half-second pause into a nine-hour one.

    A note on the pattern, because it is easy to over-credit. The `(?!s)`
    lookahead after the minutes group *looks* load-bearing and is not: with it
    removed, backtracking lets the `ms` group match anyway, and all five header
    formats parse identically. Measured, when a mutation removing it survived.
    The lookahead is kept as a statement of intent, but this test passes because
    of the `ms` group's existence, not because of the lookahead.
    """
    assert parse_duration("547ms") == pytest.approx(0.547)
    assert parse_duration("547m") == pytest.approx(547 * 60)


def test_seconds_to_afford_is_never_negative():
    """A wait is a duration, and a negative one is not a smaller wait.

    Asserted here rather than left to the pacer, which filters non-positive
    waits before sleeping. That filter made the guard inside this method
    invisible: removing it returned -38.26 for an affordable amount and every
    test still passed, because the only caller happened to discard it. Two
    copies of a protection with one test between them means neither is really
    tested.
    """
    bucket = Bucket("tokens", limit=8000, remaining=7900, reset_seconds=0.75)

    assert bucket.seconds_to_afford(2_800) == 0.0, "affordable must be no wait"
    assert bucket.seconds_to_afford(7_900) == 0.0, "exactly affordable is no wait"
    assert bucket.seconds_to_afford(8_000) > 0.0, "unaffordable must wait"

    for amount in (0, 1, 100, 2_800, 7_899, 7_900, 7_901, 20_000):
        assert bucket.seconds_to_afford(amount) >= 0.0, amount


# --- the diagnosis: which window is each limit on? --------------------------


def test_the_token_window_is_derived_as_one_minute():
    """**The answer B-1 exists to produce**, computed rather than looked up."""
    snapshot = snapshot_from_headers(LIVE_HEADERS)

    assert snapshot.tokens.refill_per_second == pytest.approx(133.3, abs=0.5)
    assert snapshot.tokens.window_seconds == pytest.approx(60.0, abs=0.5)
    assert snapshot.tokens.describe_window() == "per minute"


def test_the_request_window_is_derived_as_one_day():
    snapshot = snapshot_from_headers(LIVE_HEADERS)

    assert snapshot.requests.window_seconds == pytest.approx(86400.0, rel=0.01)
    assert snapshot.requests.describe_window() == "per day"


def test_the_window_follows_the_headers_rather_than_a_constant():
    """If the tier changes, the reported window must change with it. A test
    that pinned "per minute" would pass forever and detect nothing."""
    daily_tokens = dict(
        LIVE_HEADERS,
        **{
            "x-ratelimit-limit-tokens": "200000",
            "x-ratelimit-remaining-tokens": "100000",
            "x-ratelimit-reset-tokens": "43200s",
        },
    )
    snapshot = snapshot_from_headers(daily_tokens)
    assert snapshot.tokens.describe_window() == "per day"


def test_unmodelled_headers_are_kept_verbatim():
    """B-1 exists because the project assumed which limits applied. Anything
    unrecognised is preserved so the next surprise is visible in the record
    rather than dropped on the floor."""
    snapshot = snapshot_from_headers(
        dict(LIVE_HEADERS, **{"x-ratelimit-limit-audio-seconds": "7200"})
    )
    assert snapshot.raw["x-ratelimit-limit-audio-seconds"] == "7200"


def test_a_response_without_limit_headers_reports_nothing():
    """`None` and an empty snapshot are different claims: one says the provider
    said nothing, the other would say there are no limits."""
    assert snapshot_from_headers({"content-type": "application/json"}) is None
    assert snapshot_from_headers(None) is None


def test_a_full_bucket_has_no_measurable_refill_rate():
    """Nothing is draining, so there is nothing to divide by. Honest as `None`
    rather than a division by zero or a fabricated rate."""
    full = Bucket("tokens", limit=8000, remaining=8000, reset_seconds=0.0)
    assert full.refill_per_second is None
    assert full.window_seconds is None
    assert full.seconds_to_afford(9_000) == 0.0


def test_the_summary_names_the_window_for_each_limit():
    summary = snapshot_from_headers(LIVE_HEADERS).summary()
    assert "tokens 7,381/8,000 (per minute)" in summary
    assert "requests 931/1,000 (per day)" in summary


# --- pacing ------------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class StubProvider:
    """Reports whatever snapshot and usage it is told to."""

    model = "fake-model"

    def __init__(self, snapshot=None, usage=None, raises=None):
        self.last_rate_limit = snapshot
        self.last_usage = usage
        self.calls = 0
        self._raises = raises

    def complete(self, system, user):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return "ACTION: execute_sql\nSELECT 1"


def _snapshot(remaining_tokens, *, limit=8000, reset=None):
    used = limit - remaining_tokens
    reset_seconds = reset if reset is not None else (used / 133.3 if used else 0.0)
    return RateLimitSnapshot(
        tokens=Bucket("tokens", limit, remaining_tokens, reset_seconds),
        requests=Bucket("requests", 1000, 900, 8640.0),
        raw=dict(LIVE_HEADERS),
    )


def test_a_full_bucket_is_not_paced():
    """Pacing that fires when nothing is constrained is just latency."""
    clock = FakeClock()
    inner = StubProvider(_snapshot(7_900), TokenUsage(prompt_tokens=1_200, calls=1))
    paced = PacedProvider(inner, sleep=clock.sleep, monotonic=clock.monotonic)

    paced.complete("s", "u")
    assert clock.slept == []
    assert paced.total_slept == 0.0


def test_a_nearly_empty_bucket_is_paced():
    """The failure mode from Iteration 5: six calls fit, the seventh does not,
    and without a pause the rest of the run is refused."""
    clock = FakeClock()
    inner = StubProvider(_snapshot(200), TokenUsage(prompt_tokens=1_200, calls=1))
    paced = PacedProvider(inner, sleep=clock.sleep, monotonic=clock.monotonic)

    paced.complete("s", "u")

    assert paced.waits == 1
    # Needs 1,200 + 20% headroom = 2,800; has 200; refills at ~133/s.
    assert clock.slept[0] == pytest.approx((2_800 - 200) / 133.3, rel=0.05)


def test_a_provider_reporting_nothing_is_never_paced():
    """An unknown limit must not become an infinite wait. A pacer that stalls
    on missing telemetry is worse than no pacer at all."""
    clock = FakeClock()
    paced = PacedProvider(
        StubProvider(None, None), sleep=clock.sleep, monotonic=clock.monotonic
    )
    paced.complete("s", "u")
    assert clock.slept == []


def test_the_estimate_is_the_previous_billed_call():
    """`008` D-1: the provider's number wins for anything describing quota, so
    the pacing estimate is the last billed cost rather than a local count or a
    constant. A run whose calls get more expensive paces itself accordingly."""
    clock = FakeClock()
    inner = StubProvider(_snapshot(1_000), TokenUsage(prompt_tokens=4_000, calls=1))
    paced = PacedProvider(inner, sleep=clock.sleep, monotonic=clock.monotonic)

    paced.complete("s", "u")
    cheap = clock.slept[0]

    clock.slept.clear()
    inner.last_usage = TokenUsage(prompt_tokens=6_000, calls=1)
    paced.complete("s", "u")

    assert clock.slept[0] > cheap, "a costlier call must wait longer"


def test_time_already_elapsed_is_credited():
    """A pass does real work between calls -- generation, validation, SQL
    execution, scoring -- and the bucket refills throughout. Sleeping for time
    that has already passed would roughly double a run's wall clock."""
    clock = FakeClock()
    inner = StubProvider(_snapshot(200), TokenUsage(prompt_tokens=1_200, calls=1))
    paced = PacedProvider(inner, sleep=clock.sleep, monotonic=clock.monotonic)

    paced.complete("s", "u")
    without_credit = clock.slept[0]

    clock.slept.clear()
    clock.advance(10.0)  # ten seconds of scoring and database work
    paced.complete("s", "u")

    assert clock.slept[0] == pytest.approx(without_credit - 10.0, rel=0.05)


def test_retry_after_is_honoured_on_the_next_call():
    """A 429 carries the only authoritative answer to "when may I retry".
    Charging straight back in is what turns one refusal into a run of them."""
    clock = FakeClock()
    snapshot = RateLimitSnapshot(
        tokens=Bucket("tokens", 8000, 0, 60.0),
        requests=Bucket("requests", 1000, 900, 8640.0),
        retry_after_seconds=7.0,
        raw=dict(LIVE_HEADERS),
    )
    inner = StubProvider(snapshot, raises=RateLimitError("429"))
    paced = PacedProvider(inner, sleep=clock.sleep, monotonic=clock.monotonic)

    with pytest.raises(RateLimitError):
        paced.complete("s", "u")

    inner._raises = None
    paced.complete("s", "u")
    assert clock.slept[-1] == pytest.approx(7.0)


def test_the_wrapper_is_transparent_to_getattr_callers():
    """`describe_model`, the token accounting and the telemetry line all read
    the provider with `getattr`. A wrapper that hid any of them would silently
    file runs as `unknown` model with estimated costs."""
    from evals.run_evals import describe_model

    usage = TokenUsage(prompt_tokens=10, calls=1, measured=True)
    snapshot = _snapshot(7_000)
    paced = PacedProvider(StubProvider(snapshot, usage))

    assert describe_model(paced) == "fake-model"
    assert paced.last_usage is usage
    assert paced.last_rate_limit is snapshot


def test_a_non_rate_limit_failure_still_propagates():
    """Pacing must not swallow anything. `run_case` depends on provider errors
    reaching it as failures of that question (AC21)."""
    paced = PacedProvider(StubProvider(None, None, raises=LLMError("boom")))
    with pytest.raises(LLMError):
        paced.complete("s", "u")


def test_the_snapshot_is_cleared_before_each_call(monkeypatch):
    """A stale snapshot is worse here than a stale usage figure.

    `last_usage` going stale over-bills a report; `last_rate_limit` going stale
    paces the next call against a bucket that has since drained, which is the
    exact failure the pacer exists to prevent. Cleared first for the same reason
    T5 cleared usage first, and asserted because "we remembered to" is not a
    guarantee.
    """
    import groq

    from api.llm.groq_provider import GroqProvider

    provider = GroqProvider(api_key="test-key")
    provider._last_rate_limit = snapshot_from_headers(LIVE_HEADERS)
    assert provider.last_rate_limit is not None

    def explode(*args, **kwargs):
        raise groq.APIConnectionError(request=None)

    monkeypatch.setattr(provider, "_create", explode)

    with pytest.raises(LLMError):
        provider.complete("s", "u")

    assert provider.last_rate_limit is None, "a failed call left a stale snapshot"


# --- the limit no header reports --------------------------------------------

#: A real Groq 429, 2026-09-04, trimmed only of the organization id.
REAL_429 = (
    "Error code: 429 - {'error': {'message': 'Rate limit reached for model "
    "`openai/gpt-oss-120b` in organization `org_x` service tier `on_demand` "
    "on tokens per day (TPD): Limit 200000, Used 199301, Requested 1279. "
    "Please try again in 4m10.559999999s. Need more tokens? Upgrade to Dev "
    "Tier today at https://console.groq.com/settings/billing', "
    "'type': 'tokens', 'code': 'rate_limit_exceeded'}}"
)


def test_the_429_body_names_the_limit_no_header_reports():
    """**The correction at the centre of B-1.**

    The headers describe an 8,000-token per-minute bucket and a 1,000-request
    per-day bucket. From that it was concluded that the project's 200,000-a-day
    figure was unenforced -- absence of a header read as absence of a limit.
    It is enforced, and this refusal arrived with the per-minute bucket reading
    a full 8,000/8,000.
    """
    from api.llm.rate_limits import limit_from_message

    assert limit_from_message(REAL_429) == ("TPD", 200_000, 199_301)


def test_a_full_minute_bucket_can_accompany_a_refusal():
    """The two limits are independent, and reading only the headers on a 429
    says the opposite of what happened: plenty left, yet refused."""
    from api.llm.rate_limits import snapshot_from_headers

    snapshot = snapshot_from_headers(
        dict(LIVE_HEADERS, **{"x-ratelimit-remaining-tokens": "8000", "retry-after": "251"})
    )
    assert snapshot.tokens.remaining == 8000
    assert snapshot.retry_after_seconds == 251.0


@pytest.mark.parametrize(
    "message", [None, "", "connection reset", "429 with no limit named"]
)
def test_a_message_naming_no_limit_returns_none(message):
    from api.llm.rate_limits import limit_from_message

    assert limit_from_message(message) is None


def test_a_daily_refusal_is_not_slept_through():
    """A daily allowance does not refill on any timescale a run can wait for.

    Measured `retry-after` values on an exhausted daily quota were 85 and 251
    seconds. Sleeping through those turned a 30-question run into a crawl that
    was still going after eighteen minutes and would have produced nothing
    recordable anyway, since AC18 refuses to file a rate-limited run. Failing in
    a minute beats failing in two hours.
    """
    from api.llm.pacing import MAX_RETRY_WAIT_SECONDS

    clock = FakeClock()
    snapshot = RateLimitSnapshot(
        tokens=Bucket("tokens", 8000, 8000, 0.0),
        requests=Bucket("requests", 1000, 900, 8640.0),
        retry_after_seconds=251.0,
        raw=dict(LIVE_HEADERS),
    )
    inner = StubProvider(snapshot, raises=RateLimitError("429 TPD"))
    paced = PacedProvider(inner, sleep=clock.sleep, monotonic=clock.monotonic)

    with pytest.raises(RateLimitError):
        paced.complete("s", "u")

    inner._raises = None
    paced.complete("s", "u")

    assert clock.slept[-1] == MAX_RETRY_WAIT_SECONDS
    assert clock.slept[-1] < 251.0, "slept through a daily allowance"
