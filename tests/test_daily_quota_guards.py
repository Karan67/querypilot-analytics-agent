"""Accumulated daily spend and the three-limit pre-flight — backlog B-5.

**What B-5 corrects.** `--token-budget` policed the cost of the invocation in
front of it and nothing else, so a 30,000 ceiling was satisfied on a run
starting at 199,000 tokens already spent, and then refused on its first call.
Iteration 5 lost two multi-pass held-out runs that way and spent roughly 117,000
tokens producing nothing recordable.

**Three limits apply; before B-5, one was guarded.**

| limit | how it is known |
|---|---|
| 8,000 tokens per minute | live, from headers |
| 1,000 requests per day | live, from headers |
| 200,000 tokens per day | a local ledger, corrected by any 429 |

The asymmetry is the design. Where the provider reports what is left, asking it
beats bookkeeping; the ledger exists only for the one figure no header carries.

Everything here runs against fakes and a `tmp_path` ledger. That is not only
because the account is at its daily limit today -- a guard that can only be
tested when the quota is nearly gone is a guard that never gets tested.
"""

from __future__ import annotations

import json

from api.llm.rate_limits import Bucket, RateLimitSnapshot
from evals import ledger
from evals.run_evals import DEFAULT_DAILY_TOKEN_LIMIT, check_quota, project_requests

LIVE_RAW = {"x-ratelimit-limit-tokens": "8000", "x-ratelimit-limit-requests": "1000"}


def snapshot(*, remaining_requests=900, remaining_tokens=8000, request_limit=1000):
    return RateLimitSnapshot(
        tokens=Bucket("tokens", 8000, remaining_tokens, 1.0),
        requests=Bucket("requests", request_limit, remaining_requests, 8640.0),
        raw=dict(LIVE_RAW),
    )


# --- the ledger --------------------------------------------------------------


def test_an_absent_ledger_reads_as_an_empty_day(tmp_path):
    spend = ledger.load(tmp_path / "spend.json")
    assert (spend.tokens, spend.requests) == (0, 0)
    assert spend.estimated is True


def test_spend_accumulates_across_invocations(tmp_path):
    """**The hole B-5 exists to close.** Each run is its own process, so an
    in-memory total would reset every time and the guard would approve every
    run in a day that had already spent all of it."""
    path = tmp_path / "spend.json"
    ledger.record(24_000, 20, path)
    ledger.record(45_000, 37, path)

    spend = ledger.load(path)
    assert spend.tokens == 69_000
    assert spend.requests == 57


def test_a_ledger_from_another_day_is_not_carried_forward(tmp_path):
    """The limit reset, so the count did too. Keyed by UTC date because the
    limit resets on one and the machine's local date is not necessarily it --
    Iteration 5 nearly mis-planned a day on exactly that gap."""
    path = tmp_path / "spend.json"
    ledger.record(199_000, 500, path)

    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["day"] = "1999-12-31"
    path.write_text(json.dumps(stale), encoding="utf-8")

    assert ledger.load(path).tokens == 0


def test_a_corrupt_ledger_does_not_stop_a_run(tmp_path):
    """A state file must not be able to break the benchmark. The cost of being
    wrong here is one optimistic guard; the cost of raising is a dead runner."""
    path = tmp_path / "spend.json"
    path.write_text("{not json at all", encoding="utf-8")

    assert ledger.load(path).tokens == 0


def test_a_provider_figure_overrides_the_estimate(tmp_path):
    """**A refusal is a free correction.**

    The ledger counts only what this project spent through the runner; the
    deployed API, another checkout or a colleague's script are invisible to it,
    so its number is a floor. A 429 states the provider's own total, so it
    overwrites rather than adds.
    """
    path = tmp_path / "spend.json"
    ledger.record(50_000, 40, path)

    spend = ledger.reconcile(199_301, path)

    assert spend.tokens == 199_301, "the provider's total must replace, not add"
    assert spend.estimated is False
    assert spend.requests == 40, "the request count is unrelated and must survive"


def test_the_source_of_the_figure_is_stated(tmp_path):
    """A floor and a provider-stated total are different quantities, exactly as
    `TokenUsage.measured` keeps a local count and a billed count apart."""
    path = tmp_path / "spend.json"
    ledger.record(50_000, 40, path)
    assert "local estimate" in ledger.load(path).describe(200_000)

    ledger.reconcile(199_301, path)
    assert "provider-reconciled" in ledger.load(path).describe(200_000)


# --- the guards --------------------------------------------------------------


def test_the_daily_token_guard_counts_the_day_not_the_run(tmp_path):
    """The exact case that motivated B-5: a modest run refused because the day
    is gone, not because the run is large."""
    spend = ledger.record(199_000, 500, tmp_path / "spend.json")

    reasons = check_quota(
        projected_tokens=24_000,
        projected_requests=20,
        spend=spend,
        snapshot=snapshot(),
        daily_token_limit=200_000,
    )

    assert any("tokens per day" in r for r in reasons)
    assert any("199,000 already spent" in r for r in reasons)


def test_the_same_run_is_allowed_on_a_fresh_day(tmp_path):
    """The guard must not simply refuse everything: a 24,000-token run against
    an untouched 200,000 allowance is exactly what the tool is for.

    Asserted on *blocking* reasons rather than on silence. 24,000 tokens does
    exceed an 8,000-token minute bucket, so the pacing note is correct and must
    still appear -- and a first version of this test read `reasons == []` and
    would have been satisfied by a guard that had stopped emitting it.
    """
    spend = ledger.load(tmp_path / "spend.json")

    reasons = check_quota(
        projected_tokens=24_000,
        projected_requests=20,
        spend=spend,
        snapshot=snapshot(),
        daily_token_limit=200_000,
    )

    assert [r for r in reasons if not r.startswith("NOTE")] == []
    assert any(r.startswith("NOTE") for r in reasons), "the pacing note is not noise"


def test_the_request_guard_uses_the_live_remaining_count(tmp_path):
    """**Unguarded entirely before B-5**, and the cheaper limit to exhaust: a
    three-pass dev run is 90 calls. Read live rather than ledgered, because the
    provider reports what is left and asking beats bookkeeping."""
    reasons = check_quota(
        projected_tokens=1_000,
        projected_requests=90,
        spend=ledger.load(tmp_path / "spend.json"),
        snapshot=snapshot(remaining_requests=12),
        daily_token_limit=200_000,
    )

    assert any("requests per day" in r for r in reasons)
    assert any("12 remaining" in r for r in reasons)


def test_the_minute_bucket_is_reported_but_never_refuses(tmp_path):
    """Pacing exists for this limit, so refusing on it would refuse every run
    larger than 8,000 tokens -- which is every run worth making. The projected
    wall clock is still worth knowing before committing to it."""
    reasons = check_quota(
        projected_tokens=48_000,
        projected_requests=40,
        spend=ledger.load(tmp_path / "spend.json"),
        snapshot=snapshot(),
        daily_token_limit=200_000,
    )

    assert len(reasons) == 1
    assert reasons[0].startswith("NOTE")
    assert "6 minutes" in reasons[0]


def test_every_breached_limit_is_reported_not_just_the_first(tmp_path):
    """A run refused three times in a row, each for a different limit, is worse
    than one told at the outset that it needs a smaller split and a fresh day."""
    spend = ledger.record(199_000, 900, tmp_path / "spend.json")

    reasons = check_quota(
        projected_tokens=48_000,
        projected_requests=900,
        spend=spend,
        snapshot=snapshot(remaining_requests=5),
        daily_token_limit=200_000,
    )

    assert any("tokens per day" in r for r in reasons)
    assert any("requests per day" in r for r in reasons)
    assert any(r.startswith("NOTE") for r in reasons)


def test_an_unknown_limit_never_becomes_a_refusal(tmp_path):
    """The same rule the pacer follows. A provider that reports nothing must not
    be treated as a provider with nothing left."""
    reasons = check_quota(
        projected_tokens=500_000,
        projected_requests=5_000,
        spend=None,
        snapshot=None,
        daily_token_limit=200_000,
    )

    assert reasons == []


def test_the_daily_limit_is_the_measured_one():
    """200,000 comes from a 429 body reading `Limit 200000`, not from
    documentation. It is the one figure here that goes stale silently if the
    tier changes, which is why it is overridable."""
    assert DEFAULT_DAILY_TOKEN_LIMIT == 200_000


# --- the request projection --------------------------------------------------


def test_requests_are_projected_worst_case():
    """Worst case for the same reason `project_run_cost` is (AC8). Measured
    reality is close to one call per question -- 60 calls for 60 attempts in
    Iteration 5's compact arm -- so this overstates threefold and will refuse
    some runs that would have fitted. That trade was already made for tokens and
    is kept rather than quietly reversed for requests."""
    from evals.dataset import load_dataset

    dataset = load_dataset()
    assert project_requests(dataset, "test", max_calls=3, repeat=2) == 20 * 3 * 2
    assert project_requests(dataset, "dev", max_calls=1, repeat=1) == 30


# --- end to end through main(), with a fake provider ------------------------


class _NeverRan(Exception):
    """Raised in place of `run_evaluation`, so a blocked run proves it never
    reached the model rather than merely returning the right exit code."""


class QuotaFake:
    """A provider that answers the pre-flight probe and reports its limits."""

    model = "fake-model"

    def __init__(self, *, remaining_requests=900):
        self.last_rate_limit = snapshot(remaining_requests=remaining_requests)
        self.last_usage = None
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        return "ok"


def _patch_provider(monkeypatch, provider):
    import api.llm.factory as factory

    monkeypatch.setattr(factory, "get_provider", lambda *a, **k: provider)


def test_a_spent_day_blocks_the_run_before_any_question(monkeypatch, tmp_path):
    """**The whole of B-5, end to end.**

    Iteration 5's two lost runs each fired every question, collected rate
    limits, and produced a number AC18 then refused to record. The quota was
    spent and nothing was bought. This must refuse before the first question.
    """
    import evals.run_evals as runner

    monkeypatch.setattr(ledger, "DEFAULT_PATH", tmp_path / "spend.json")
    ledger.record(199_000, 500, tmp_path / "spend.json")

    provider = QuotaFake()
    _patch_provider(monkeypatch, provider)
    monkeypatch.setattr(runner, "run_evaluation", _raise_never_ran)

    exit_code = runner.main(["--split", "test", "--strategy", "loop"])

    assert exit_code == 1
    assert provider.calls == 1, "only the pre-flight probe should have been made"


def test_the_request_limit_also_blocks_before_the_run(monkeypatch, tmp_path):
    """Unguarded before B-5. A three-pass dev run is 90 calls, and nothing
    anywhere counted them."""
    import evals.run_evals as runner

    monkeypatch.setattr(ledger, "DEFAULT_PATH", tmp_path / "spend.json")
    provider = QuotaFake(remaining_requests=3)
    _patch_provider(monkeypatch, provider)
    monkeypatch.setattr(runner, "run_evaluation", _raise_never_ran)

    assert runner.main(["--split", "test", "--strategy", "loop"]) == 1


def test_a_fresh_day_is_allowed_through(monkeypatch, tmp_path):
    """The guard has to let work happen. Reaching the sentinel is the proof
    that the pre-flight approved the run rather than silently refusing it."""
    import evals.run_evals as runner

    monkeypatch.setattr(ledger, "DEFAULT_PATH", tmp_path / "spend.json")
    _patch_provider(monkeypatch, QuotaFake())
    monkeypatch.setattr(runner, "run_evaluation", _raise_never_ran)

    with __import__("pytest").raises(_NeverRan):
        runner.main(["--split", "test", "--strategy", "loop"])


def test_ignore_daily_spend_bypasses_the_ledger(monkeypatch, tmp_path):
    """For a run against a different key than the ledger was built from, where
    its totals describe someone else's day."""
    import evals.run_evals as runner

    monkeypatch.setattr(ledger, "DEFAULT_PATH", tmp_path / "spend.json")
    ledger.record(199_000, 500, tmp_path / "spend.json")
    _patch_provider(monkeypatch, QuotaFake())
    monkeypatch.setattr(runner, "run_evaluation", _raise_never_ran)

    with __import__("pytest").raises(_NeverRan):
        runner.main(["--split", "test", "--strategy", "loop", "--ignore-daily-spend"])


def _raise_never_ran(*args, **kwargs):
    raise _NeverRan


def test_the_real_ledger_is_isolated_from_the_whole_suite():
    """Proves the autouse isolation is in effect rather than testing for the
    absence of a symptom.

    Asserting "the real file does not exist" would pass on a machine that had
    simply never run a benchmark, and would keep passing after the fixture was
    deleted. Asserting that the path *points somewhere else* only passes while
    the isolation is actually applied.
    """
    from evals import ledger

    assert "pytest" in str(ledger.DEFAULT_PATH) or "tmp" in str(ledger.DEFAULT_PATH).lower(), (
        f"the ledger is not redirected: {ledger.DEFAULT_PATH}"
    )
    assert ledger.DEFAULT_PATH.name == "spend.json"
