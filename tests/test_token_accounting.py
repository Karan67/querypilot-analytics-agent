"""Token accounting tests — `specs/008-prompt-tuning.md` AC5, AC8, AC9 (T5).

Three things, and the seam between them is the point:

- **AC5** — what a run cost, per question and per pass, with every figure
  saying *which instrument* produced it.
- **AC8** — two budget guards. A worst-case projection counted locally, which
  refuses to start; and an in-flight check against what the provider actually
  billed, which stops a run whose projection was wrong.
- **AC9** — `rate_limited` is its own category, signalled by type rather than by
  matching message text, and a run containing one cannot be recorded.

The dual instrument is the thing most likely to go quietly wrong, so
`measured` is asserted on almost every path: a local `tiktoken` count and a
billed count are different quantities, and a total that mixed them would be
wrong in the one way nobody would notice.
"""

from __future__ import annotations

import dataclasses

import pytest

from api.agent.orchestrator import answer
from api.agent.single_shot import CATEGORY_PROVIDER_ERROR, CATEGORY_RATE_LIMITED
from api.llm.base import LLMError, RateLimitError, TokenUsage
from api.llm.counting import (
    count_tokens,
    estimate_usage,
    project_worst_case,
    usage_for_call,
)

pytestmark = pytest.mark.usefixtures("configured_database")


def act(name: str, argument: str = "") -> str:
    return f"ACTION: {name}\n{argument}".strip()


class Scripted:
    """Hands out canned responses. `reports_usage` decides whether it behaves
    like a provider that tells you what it charged."""

    def __init__(self, *responses, reports_usage=None, model="fake-model"):
        self._responses = list(responses)
        self._reports = reports_usage
        self.model = model
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        response = self._responses[index]
        if self._reports is not None:
            self.last_usage = self._reports
        if isinstance(response, Exception):
            raise response
        return response


# --- TokenUsage arithmetic --------------------------------------------------


def test_usage_adds_componentwise():
    a = TokenUsage(prompt_tokens=10, completion_tokens=2, calls=1, measured=True)
    b = TokenUsage(prompt_tokens=5, completion_tokens=3, calls=1, measured=True)
    total = a + b
    assert (total.prompt_tokens, total.completion_tokens, total.calls) == (15, 5, 2)
    assert total.total_tokens == 20


def test_a_sum_containing_an_estimate_is_an_estimate():
    """**The rule that keeps the two instruments apart.**

    A billed figure and a local count are different quantities. A total that
    silently claimed to be billed because most of its parts were would be wrong
    in exactly the way a reader could not detect — so `measured` degrades on
    contact, never upgrades.
    """
    billed = TokenUsage(prompt_tokens=10, calls=1, measured=True)
    local = TokenUsage(prompt_tokens=10, calls=1, measured=False)

    assert (billed + local).measured is False
    assert (local + billed).measured is False
    assert (billed + billed).measured is True


# --- the local instrument ---------------------------------------------------


def test_counting_uses_a_real_tokenizer_not_a_character_heuristic():
    """`chars // 4` is what produced every wrong figure in spec §2.

    Asserted **structurally**, not by comparing one string to its own length —
    a first attempt did that and failed on a string where the two happened to
    agree, which is exactly the coincidence that makes such a test worthless.

    Two strings of *identical* character length must tokenize differently. No
    character-count heuristic can do that, so this cannot pass by accident.
    """
    natural = "the quick brown fox jumped over a lazy sleeping dog nearby ok"
    repetitive = "a" * len(natural)
    assert len(repetitive) == len(natural), "same length, by construction"
    assert count_tokens(repetitive) != count_tokens(natural)


def test_an_estimate_is_never_marked_measured():
    usage = estimate_usage("system prompt", "the question", "the answer")
    assert usage.measured is False
    assert usage.calls == 1
    assert usage.prompt_tokens > 0
    assert usage.completion_tokens > 0


def test_the_projection_is_worst_case_not_average():
    """Every question burning its full budget, which is what a run of nothing
    but failures does. An optimistic projection that lets a run die at question
    31 wastes the 30 that worked."""
    assert project_worst_case(prompt_tokens=1000, questions=40, max_calls=3) == 120_000


# --- reading usage off a provider (D-1) -------------------------------------


def test_a_reporting_provider_is_believed():
    billed = TokenUsage(prompt_tokens=900, completion_tokens=40, calls=1, measured=True)
    provider = Scripted("x", reports_usage=billed)
    provider.complete("s", "u")

    assert usage_for_call(provider, "s", "u", "x") == billed


def test_a_silent_provider_falls_back_to_a_local_count():
    """D-1's fallback. **A real tokenizer, not the 4-character estimate the
    decision originally proposed** — that estimate is the thing this iteration
    exists to have stopped trusting."""
    provider = Scripted("x")
    usage = usage_for_call(provider, "system", "user", "response")

    assert usage.measured is False
    assert usage.prompt_tokens == count_tokens("system") + count_tokens("user")


def test_a_provider_with_a_nonsense_usage_attribute_is_ignored():
    """Defensive because `last_usage` is read off an object this project does
    not own. Trusting the shape would turn a paid-for, working answer into a
    crash."""
    provider = Scripted("x")
    provider.last_usage = {"prompt_tokens": 900}  # not a TokenUsage

    usage = usage_for_call(provider, "system", "user", "response")
    assert usage.measured is False, "fell back rather than trusting the shape"


def test_accounting_never_breaks_a_working_answer(monkeypatch):
    """Instrumentation that can fail a run is a bug. If the local counter is
    unavailable *and* the provider reports nothing, the answer still stands and
    the cost is simply unknown."""
    import api.llm.counting as counting

    def explode(*_args, **_kwargs):
        raise counting.TokenCountingUnavailable("no tokenizer")

    monkeypatch.setattr(counting, "estimate_usage", explode)

    usage = counting.usage_for_call(Scripted("x"), "s", "u", "r")
    assert usage == TokenUsage()


# --- AC5: usage reaches the caller ------------------------------------------


def test_ac5_the_agent_reports_what_a_question_cost():
    billed = TokenUsage(prompt_tokens=900, completion_tokens=20, calls=1, measured=True)
    provider = Scripted(
        act("execute_sql", "SELECT count(*) FROM track"), reports_usage=billed
    )
    result = answer("q", provider=provider)

    assert result.ok is True
    assert result.usage.calls == 1
    assert result.usage.prompt_tokens == 900
    assert result.usage.measured is True


def test_ac5_usage_accumulates_across_retries():
    """The cost model is `calls x prompt size`, so a question that needed two
    attempts cost twice one that needed one. A per-question figure that counted
    only the successful call would hide exactly the expensive cases."""
    billed = TokenUsage(prompt_tokens=900, completion_tokens=20, calls=1, measured=True)
    provider = Scripted(
        act("execute_sql", "SELECT nope FROM track"),
        act("execute_sql", "SELECT count(*) FROM track"),
        reports_usage=billed,
    )
    result = answer("q", provider=provider)

    assert result.ok is True
    assert result.usage.calls == 2
    assert result.usage.prompt_tokens == 1800


def test_ac5_a_failed_question_still_reports_its_cost():
    """A budget that counted only successes would understate the runs that
    spend the most: a question burning all three calls is the expensive case."""
    billed = TokenUsage(prompt_tokens=900, completion_tokens=20, calls=1, measured=True)
    # Distinct SQL each attempt: repeating one would trip the repeated-statement
    # guard and end the run at two calls, which is a different scenario.
    provider = Scripted(
        act("execute_sql", "SELECT a FROM track"),
        act("execute_sql", "SELECT b FROM track"),
        act("execute_sql", "SELECT c FROM track"),
        reports_usage=billed,
    )
    result = answer("q", provider=provider)

    assert result.ok is False
    assert result.usage.calls == 3, "the whole budget was spent and is accounted for"
    assert result.usage.prompt_tokens == 2700


# --- AC9: rate limits are their own category --------------------------------


def test_ac9_a_rate_limit_is_categorised_separately():
    """Iteration 4's 82.5% was 33 correct and 7 rate-limited, reported
    identically. One is evidence about the prompt; the other about the clock."""
    provider = Scripted(RateLimitError("quota exhausted"))
    result = answer("q", provider=provider)

    assert result.category == CATEGORY_RATE_LIMITED
    assert result.category != CATEGORY_PROVIDER_ERROR


def test_a_plain_provider_failure_is_still_a_provider_error():
    provider = Scripted(LLMError("connection reset"))
    result = answer("q", provider=provider)
    assert result.category == CATEGORY_PROVIDER_ERROR


def test_rate_limit_error_is_an_llm_error():
    """A caller that only knows about `LLMError` must still catch it. Widening
    the interface by a type must not narrow what existing handlers catch."""
    assert issubclass(RateLimitError, LLMError)


def test_ac9_a_rate_limit_ends_the_run_rather_than_retrying():
    """Retrying spends the remaining budget re-learning that the clock has not
    moved, and every retry is another request against the quota that just
    refused one."""
    from api.agent.orchestrator import RETRY_POLICY, STOP

    assert RETRY_POLICY[CATEGORY_RATE_LIMITED] == STOP

    provider = Scripted(RateLimitError("quota"))
    result = answer("q", provider=provider)
    assert result.attempts_used == 1, "did not burn the budget on a quota refusal"


def test_the_provider_maps_the_vendors_rate_limit_type(monkeypatch):
    """**Typed, never text-matched.** `003` established that message wording is
    not a contract; an SDK rephrasing would otherwise silently reclassify every
    rate-limited run as a model failure.
    """
    import groq

    from api.llm.groq_provider import GroqProvider

    provider = GroqProvider(api_key="test-key-not-real")

    class Boom:
        def create(self, **_kwargs):
            raise groq.RateLimitError(
                "429", response=_FakeResponse(), body=None
            )

    monkeypatch.setattr(provider._client.chat, "completions", Boom())

    with pytest.raises(RateLimitError):
        provider.complete("system", "user")


class _FakeResponse:
    """Minimal stand-in for the httpx response the SDK error requires."""

    status_code = 429
    headers: dict = {}
    request = None


# --- AC5: the terminal report states what the run cost -----------------------


def _report_costing(prompt, completion, calls, measured=True):
    """One pass whose only interesting property is its bill."""
    from evals.scoring import CaseResult, aggregate

    case = CaseResult(id="easy-001", tier="easy", question="q", correct=True)
    report = aggregate([case], model="m", split="dev")
    return dataclasses.replace(
        report,
        usage=TokenUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            calls=calls,
            measured=measured,
        ),
    )


def test_ac5_the_terminal_report_states_what_the_run_cost(capsys):
    """**A tuning run is never recorded**, so `EVALS.md` is not where its cost
    can be read.

    T5 put these figures in `format_report`, which only runs under `--record`.
    Every run this iteration actually makes is a `--split dev` run without it,
    and until T7 those printed no cost at all -- the same blindness Iteration 4
    described as discovering the ceiling through 31 provider errors.
    """
    from evals.dataset import load_dataset
    from evals.run_evals import print_report

    print_report([_report_costing(40_000, 800, 30)], load_dataset(), verbose=False)

    out = capsys.readouterr().out
    assert "40,800" in out, "the total is missing"
    assert "40,000 prompt" in out
    assert "800 completion" in out
    assert "provider calls     30" in out


def test_the_terminal_total_sums_every_pass(capsys):
    """The quota is charged for all of them.

    A per-pass figure is the wrong denominator for the only decision this
    number informs -- whether the next arm fits in the day -- and taking
    `reports[0]`, which is what every other line of the report does, would
    under-report a three-pass run by two thirds.
    """
    from evals.dataset import load_dataset
    from evals.run_evals import print_report

    passes = [_report_costing(10_000, 500, 30), _report_costing(12_000, 400, 30)]
    print_report(passes, load_dataset(), verbose=False)

    out = capsys.readouterr().out
    assert "22,900" in out, "passes were not summed"
    assert "per pass           11,450 mean" in out
    assert "provider calls     60" in out


def test_the_terminal_report_names_the_instrument(capsys):
    """D-1: a local count and a billed count are different quantities.

    Printed without the label, 22,900 tokens would read as money on a run where
    the provider reported nothing at all.
    """
    from evals.dataset import load_dataset
    from evals.run_evals import print_report

    print_report(
        [_report_costing(10_000, 500, 30, measured=False)],
        load_dataset(),
        verbose=False,
    )
    assert "local tiktoken (estimated)" in capsys.readouterr().out

    print_report([_report_costing(10_000, 500, 30)], load_dataset(), verbose=False)
    assert "provider (billed)" in capsys.readouterr().out


def test_an_unbilled_pass_does_not_degrade_a_billed_total():
    """Zero-call usage is the identity, so a pass that made no calls cannot
    turn a measured run into an estimated one. This is the T5 bug, asserted at
    the level the terminal report reads from."""
    from evals.run_evals import total_usage

    spent = total_usage(
        [_report_costing(10_000, 500, 30), _report_costing(0, 0, 0, measured=False)]
    )
    assert spent.measured is True
    assert spent.total_tokens == 10_500


# --- AC7: a comparison must say which arm produced it ------------------------


def test_the_terminal_report_identifies_the_rendering_arm(capsys):
    """**T7's two arms share a prompt fingerprint.**

    `fingerprint_for` covers the template and the glossary, both identical
    across the rendering A/B; only the schema block differs, and that lives in
    the schema fingerprint. Until T7 the terminal header printed neither the
    rendering nor the schema fingerprint, so `ddl` and `compact` runs were
    byte-identical above the accuracy line.
    """
    from evals.dataset import load_dataset
    from evals.run_evals import print_report
    from evals.scoring import CaseResult, aggregate

    dataset = load_dataset()
    case = CaseResult(id="easy-001", tier="easy", question="q", correct=True)

    ddl = aggregate([case], rendering="ddl", schema_fingerprint="aaaaaaaaaaaa",
                    glossary=True, split="dev")
    compact = aggregate([case], rendering="compact", schema_fingerprint="bbbbbbbbbbbb",
                        glossary=True, split="dev")

    print_report([ddl], dataset, verbose=False)
    ddl_out = capsys.readouterr().out
    print_report([compact], dataset, verbose=False)
    compact_out = capsys.readouterr().out

    assert "rendering          ddl  (aaaaaaaaaaaa)" in ddl_out
    assert "rendering          compact  (bbbbbbbbbbbb)" in compact_out
    assert ddl_out != compact_out, "the two arms printed identically"


def test_the_terminal_report_states_whether_the_glossary_was_on(capsys):
    """AC13's control differs from its treatment in exactly this one bit."""
    from evals.dataset import load_dataset
    from evals.run_evals import print_report
    from evals.scoring import CaseResult, aggregate

    dataset = load_dataset()
    case = CaseResult(id="easy-001", tier="easy", question="q", correct=True)

    print_report([aggregate([case], glossary=True)], dataset, verbose=False)
    assert "glossary           on" in capsys.readouterr().out

    print_report([aggregate([case], glossary=False)], dataset, verbose=False)
    assert "glossary           off" in capsys.readouterr().out


def test_the_recorded_token_figure_covers_every_pass():
    """`EVALS.md` files this row three lines above `Passes`.

    Taking the first pass under an unqualified "Tokens" heading under-reported
    a repeated run by (passes - 1) / passes -- and `EVALS.md` is append-only, so
    a wrong figure there is permanent.
    """
    from evals.dataset import load_dataset
    from evals.run_evals import format_report

    passes = [_report_costing(10_000, 500, 30), _report_costing(12_000, 400, 30)]
    block = format_report(passes, load_dataset(), split="dev")

    assert "| Tokens (all passes) | 22,900 (22,000 prompt + 900 completion) |" in block
    assert "| Provider calls | 60 |" in block


def test_the_recorded_block_states_whether_the_glossary_was_on():
    """Found by a mutation that was aimed somewhere else.

    An anchor meant for the terminal line matched this row first, the mutation
    survived, and the reason was that no test read it -- so the value recorded
    permanently in `EVALS.md` for AC13's central comparison could have been
    hardcoded either way without anything going red.
    """
    from evals.dataset import load_dataset
    from evals.run_evals import format_report
    from evals.scoring import CaseResult, aggregate

    dataset = load_dataset()
    case = CaseResult(id="easy-001", tier="easy", question="q", correct=True)

    on = format_report([aggregate([case], glossary=True)], dataset, split="dev")
    off = format_report([aggregate([case], glossary=False)], dataset, split="dev")

    assert "| Glossary | on |" in on
    assert "| Glossary | off |" in off


# --- D-3: the recorded block is the path that persists -----------------------


def _held_out_report(*, split="test", failures=1):
    """A report with a failing case, on whichever split is asked for."""
    from evals.scoring import CaseResult, aggregate

    cases = [CaseResult(id="easy-001", tier="easy", question="fine", correct=True)]
    for i in range(failures):
        cases.append(
            CaseResult(
                id=f"expert-{i + 10:03d}",
                tier="expert",
                question="How many charting artists have a credited track?",
                generated_sql="SELECT count(*) FROM artist",
                category="wrong_result",
                passed_validation=True,
                executed=True,
            )
        )
    return aggregate(cases, split=split, rendering="compact", glossary=True)


def test_d3_the_recorded_block_withholds_held_out_failures():
    """**The defect T8 shipped into an append-only file.**

    `print_report` has always gated this list; `format_report` never did. The
    consequence was worse in the recorded path than in the terminal, because a
    terminal leak scrolls away and a recorded one is permanent -- and it sat
    three rows below the `Test failures revealed | no` that the same block
    asserted, so the entry contradicted itself.
    """
    from evals.dataset import load_dataset
    from evals.run_evals import format_report

    block = format_report(
        [_held_out_report()], load_dataset(), split="test", revealed=False
    )

    assert "| Test failures revealed | no |" in block
    assert "expert-010" not in block, "the failing question id reached the record"
    assert "charting artists" not in block, "the question text reached the record"
    assert "SELECT count(*) FROM artist" not in block, "the generated SQL did"
    assert "withheld" in block, "the absence must be stated, not silent"


def test_the_flag_still_records_the_detail_it_audits():
    """D-3 is an audit trail, not a block. Passing the flag must actually
    produce the list -- otherwise the flag records an intent it never carried
    out, which is worse than either alternative."""
    from evals.dataset import load_dataset
    from evals.run_evals import format_report

    block = format_report(
        [_held_out_report()], load_dataset(), split="test", revealed=True
    )

    assert "| Test failures revealed | YES |" in block
    assert "expert-010" in block
    assert "SELECT count(*) FROM artist" in block


def test_dev_split_failures_are_never_withheld():
    """The gate is the *held-out* split, not failure detail in general. The dev
    list is the tuning backlog and withholding it would remove the one thing a
    tuning run is for."""
    from evals.dataset import load_dataset
    from evals.run_evals import format_report

    block = format_report(
        [_held_out_report(split="dev")], load_dataset(), split="dev", revealed=False
    )

    assert "expert-010" in block, "a dev failure was withheld"


def test_the_recorded_tier_counts_are_the_scored_split():
    """`easy (14)` on a run that scored 6 easy questions overstates what was
    measured by more than twice, permanently, in the file whose only job is
    making numbers comparable."""
    from evals.dataset import load_dataset, questions_in_split
    from evals.run_evals import format_report

    dataset = load_dataset()
    block = format_report(
        [_held_out_report()], dataset, split="test", revealed=False
    )

    in_test = questions_in_split(dataset, "test")
    easy_in_test = sum(1 for q in in_test if q.tier == "easy")
    easy_in_corpus = len(dataset.by_tier("easy"))

    assert easy_in_test != easy_in_corpus, "fixture cannot discriminate"
    assert f"| easy ({easy_in_test}) |" in block
    assert f"| easy ({easy_in_corpus}) |" not in block


def test_a_whole_corpus_run_still_counts_the_whole_corpus():
    """`split=None` scores everything, so there the two readings coincide --
    and the fix must not have quietly narrowed that case."""
    from evals.dataset import load_dataset
    from evals.run_evals import format_report

    dataset = load_dataset()
    block = format_report(
        [_held_out_report(split=None)], dataset, split=None, revealed=False
    )
    assert f"| easy ({len(dataset.by_tier('easy'))}) |" in block


def test_a_rate_limit_in_any_pass_refuses_the_record(monkeypatch, tmp_path, capsys):
    """**AC18 applies to the run, not to its first pass.**

    A three-pass run whose *second* pass hit the quota was recorded as a clean
    measurement, because the guard read `reports[0]`. That is the same pass-one
    blind spot as the token total (T7) and the held-out failure leak (T8), and
    it is the most consequential of the three: the other two mis-describe an
    entry, this one files a floor as a measurement.

    Asserting the file is never created also re-proves the `EVALS_PATH`
    isolation that T5 had to fix after a mutation run wrote a fake entry into
    the real record.
    """
    import evals.run_evals as runner
    from evals.scoring import CaseResult, aggregate

    clean = aggregate(
        [CaseResult(id="easy-001", tier="easy", question="q", correct=True)],
        split="test",
    )
    limited = aggregate(
        [
            CaseResult(
                id="easy-001",
                tier="easy",
                question="q",
                category=CATEGORY_RATE_LIMITED,
            )
        ],
        split="test",
    )

    target = tmp_path / "EVALS.md"
    monkeypatch.setattr(runner, "EVALS_PATH", target)
    monkeypatch.setattr(runner, "run_evaluation", lambda *a, **k: [clean, limited])

    exit_code = runner.main(["--split", "test", "--record", "--repeat", "2"])

    assert exit_code == 1
    assert not target.exists(), "a rate-limited run reached the record"
    assert "rate limited" in capsys.readouterr().err


# --- AC8: the two budget guards ---------------------------------------------


class _NeverRan(Exception):
    """Raised in place of `run_evaluation` so a pre-flight test spends nothing.

    The alternative -- letting `main` proceed and relying on the in-flight guard
    to stop it -- would put a real provider call inside a test about a check
    that runs *before* any call. That is the thing AC8 exists to prevent.
    """


def _raise_never_ran(*args, **kwargs):
    raise _NeverRan


def test_ac8_a_zero_budget_aborts_before_spending_anything(monkeypatch, capsys):
    """**Nothing is spent, which is the whole point of a pre-flight check.**

    A run that dies at question 31 wastes the 30 that worked and produces a
    number that is not a measurement. The projection is worst-case precisely so
    that a run allowed to start can be expected to finish.

    A zero ceiling is the cleanest possible assertion of that: no provider is
    configured in this test, and the run must fail before it would need one.
    """
    import evals.run_evals as runner

    # Added at T7: the assertion is unchanged, but a broken guard now hits a
    # sentinel rather than running the benchmark. A test named "aborts before
    # spending anything" should not be able to spend anything when it fails.
    monkeypatch.setattr(runner, "run_evaluation", _raise_never_ran)

    exit_code = runner.main(["--token-budget", "0", "--split", "dev"])
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "Aborted before spending anything" in err
    assert "worst case" in err


def test_a_zero_max_projection_aborts_and_names_the_flag_that_raises_it(
    monkeypatch, capsys
):
    """The pre-flight ceiling is `--max-projection`'s when it is given.

    Separate from the `--token-budget` case below rather than folded into it:
    the two flags now govern different guards, and a test that only exercised
    the shared path would not notice if one stopped being read.

    `--token-budget` is set absurdly high on purpose: if the ceilings were ever
    re-merged, this run would be authorised and the sentinel would fire. The
    sentinel is also what keeps mutating this guard cheap -- without it, a
    broken pre-flight check sends the mutation harness into a real 30-question
    run at ~41,000 billed tokens a time.
    """
    import evals.run_evals as runner

    monkeypatch.setattr(runner, "run_evaluation", _raise_never_ran)

    assert (
        runner.main(
            ["--max-projection", "0", "--token-budget", "10000000", "--split", "dev"]
        )
        == 1
    )

    err = capsys.readouterr().err
    assert "Aborted before spending anything" in err
    assert "--max-projection" in err, "the message must name the flag that lifts it"


def test_max_projection_authorises_a_run_the_projection_alone_would_refuse(monkeypatch):
    """**The whole reason the flags were split** (T7).

    A 30-question dev run projects a worst case of ~306,720 tokens at three
    passes, while a pass actually bills ~41,000. Under one knob, clearing the
    projection meant setting a ceiling seven times larger than any pass would
    ever reach, and the breaker could not fire. Here the projection is
    authorised and the breaker is left at 1 -- a combination the old single
    flag could not express at all.
    """
    import evals.run_evals as runner

    monkeypatch.setattr(runner, "run_evaluation", _raise_never_ran)

    with pytest.raises(_NeverRan):
        runner.main(
            ["--max-projection", "10000000", "--token-budget", "1", "--split", "dev"]
        )


def test_the_breaker_survives_a_raised_ceiling(monkeypatch, capsys):
    """The raised ceiling must not silently become the breaker's value too.

    Asserted on what reaches `run_evaluation`, not on the printed banner: the
    banner is a courtesy and the argument is the contract.
    """
    import evals.run_evals as runner

    seen = {}

    def capture(*args, **kwargs):
        seen.update(kwargs)
        raise _NeverRan

    monkeypatch.setattr(runner, "run_evaluation", capture)

    with pytest.raises(_NeverRan):
        runner.main(
            ["--max-projection", "10000000", "--token-budget", "55000", "--split", "dev"]
        )

    assert seen["token_budget"] == 55_000, "the breaker took the raised ceiling's value"

    err = capsys.readouterr().err
    assert "Pre-flight ceiling raised" in err
    assert "55,000 per pass" in err


def test_token_budget_still_supplies_the_pre_flight_ceiling_alone(monkeypatch, capsys):
    """Backwards compatibility, stated as a test rather than assumed.

    `--token-budget` on its own kept its old meaning after the split, so every
    invocation recorded in `EVALS.md` before T7 still behaves as it did. If
    that ever stops being wanted, this test is where the decision gets made.
    """
    import evals.run_evals as runner

    monkeypatch.setattr(runner, "run_evaluation", _raise_never_ran)

    assert runner.main(["--token-budget", "0", "--split", "dev"]) == 1
    assert "Aborted before spending anything" in capsys.readouterr().err


def test_the_projection_scales_with_the_split(dataset_for_projection):
    """A dev run projects less than the whole corpus, because it asks fewer
    questions. If it did not, `--split` would not be a cost lever."""
    from evals.run_evals import project_run_cost

    dev = project_run_cost(dataset_for_projection, "dev", "full", "ddl", True, 3)
    everything = project_run_cost(dataset_for_projection, None, "full", "ddl", True, 3)
    assert 0 < dev < everything


def test_the_projection_is_sensitive_to_the_rendering(dataset_for_projection):
    """T2 measured `compact` at 737 prompt tokens against `ddl`'s 925, so a
    projection that ignored the rendering would price the cheaper configuration
    identically and make the budget useless as a lever."""
    from evals.run_evals import project_run_cost

    ddl = project_run_cost(dataset_for_projection, "dev", "full", "ddl", True, 3)
    compact = project_run_cost(dataset_for_projection, "dev", "full", "compact", True, 3)
    assert compact < ddl


def test_the_projection_counts_the_glossary(dataset_for_projection):
    """178 measured tokens on every call, so a projection blind to it
    under-prices a 40-question pass by roughly 7,000."""
    from evals.run_evals import project_run_cost

    without = project_run_cost(dataset_for_projection, "dev", "full", "ddl", False, 3)
    with_it = project_run_cost(dataset_for_projection, "dev", "full", "ddl", True, 3)
    assert with_it > without


@pytest.fixture(scope="module")
def dataset_for_projection():
    from evals.dataset import load_dataset

    return load_dataset()


def test_ac8_the_in_flight_guard_stops_a_run_whose_projection_was_wrong():
    """**The guard against our own instruments disagreeing.**

    This should never fire: the pre-flight projection is worst-case. It exists
    for the case D-1 created by using two instruments — `tiktoken` under-counting
    against Groq's tokenizer — where a run passes pre-flight and then overspends.

    Simulated by running with a ceiling the billed usage crosses immediately.
    """
    from evals.dataset import load_dataset
    from evals.run_evals import TokenBudgetExceeded, run_pass

    dataset = load_dataset()
    gold = {q.id: _ok_gold() for q in dataset.questions}
    billed = TokenUsage(prompt_tokens=10_000, calls=1, measured=True)
    provider = Scripted(
        act("execute_sql", "SELECT count(*) FROM track"), reports_usage=billed
    )

    with pytest.raises(TokenBudgetExceeded, match="under-counted"):
        run_pass(
            dataset,
            provider,
            gold,
            strategy="loop",
            split="dev",
            token_budget=15_000,
        )


def test_the_in_flight_guard_ignores_an_estimated_spend():
    """Only *billed* usage polices the ceiling.

    Stopping a run because a local estimate crossed a line would let the
    instrument that is not denominated in the quota enforce the quota. The
    provider below reports nothing, so every figure is an estimate and the run
    completes regardless of how small the ceiling is.
    """
    from evals.dataset import load_dataset
    from evals.run_evals import run_pass

    dataset = load_dataset()
    gold = {q.id: _ok_gold() for q in dataset.questions}
    provider = Scripted(act("execute_sql", "SELECT count(*) FROM track"))

    report = run_pass(
        dataset, provider, gold, strategy="loop", split="dev", token_budget=1
    )
    assert report.usage.measured is False
    assert report.total == 30, "the run finished rather than being stopped"


def _ok_gold():
    from api.db.execution import execute_sql

    return execute_sql("SELECT count(*) FROM track")


def test_usage_is_cleared_before_each_call(monkeypatch):
    """**A failed call must not leave the previous call's cost standing.**

    `last_usage` is read once per call by an accumulator. If a failure left the
    prior value in place, the loop would add that call's cost a second time —
    billing a successful call twice and a failed one at someone else's rate,
    which inflates precisely the runs that hit trouble.
    """
    import groq

    from api.llm.groq_provider import GroqProvider

    provider = GroqProvider(api_key="test-key-not-real")

    class Succeeds:
        def create(self, **_kwargs):
            return _FakeCompletion(prompt=900, completion=20)

    monkeypatch.setattr(provider._client.chat, "completions", Succeeds())
    provider.complete("s", "u")
    assert provider.last_usage is not None
    assert provider.last_usage.prompt_tokens == 900

    class Fails:
        def create(self, **_kwargs):
            raise groq.APIConnectionError(request=None)

    monkeypatch.setattr(provider._client.chat, "completions", Fails())
    with pytest.raises(LLMError):
        provider.complete("s", "u")

    assert provider.last_usage is None, "the previous call's cost was left behind"


def test_a_response_without_a_usage_block_reports_nothing(monkeypatch):
    """`None`, never zero. A missing figure read as 0 would make a run look
    free, which is the one direction a cost report must never be wrong in."""
    from api.llm.groq_provider import GroqProvider

    provider = GroqProvider(api_key="test-key-not-real")

    class NoUsage:
        def create(self, **_kwargs):
            return _FakeCompletion(prompt=None, completion=None)

    monkeypatch.setattr(provider._client.chat, "completions", NoUsage())
    provider.complete("s", "u")

    assert provider.last_usage is None


class _FakeCompletion:
    def __init__(self, prompt, completion):
        self.choices = [_FakeChoice()]
        self.usage = (
            None
            if prompt is None
            else type(
                "Usage", (), {"prompt_tokens": prompt, "completion_tokens": completion}
            )()
        )


class _FakeChoice:
    class message:  # noqa: N801 - mirrors the SDK's shape
        content = "ACTION: execute_sql\nSELECT 1"


# --- AC18: a rate-limited run is not recordable -----------------------------


def test_ac18_a_rate_limited_run_is_not_recorded(monkeypatch, capsys, tmp_path):
    """**The guard that stops Iteration 4's mistake repeating.**

    Its 82.5% was 33 correct and 7 rate-limited, and it read as a measurement
    for weeks. AC18 says no prompt change is accepted on a run that was rate
    limited, because the comparison is against the clock rather than the prompt.

    The number still prints. What is refused is filing it *alongside comparable
    ones*, where it invites exactly that reading.
    """
    import evals.run_evals as runner

    evals_file = tmp_path / "EVALS.md"
    monkeypatch.setattr(runner, "EVALS_PATH", evals_file)
    monkeypatch.setattr(
        runner, "get_provider", lambda: Scripted(RateLimitError("quota")), raising=False
    )

    def fake_provider():
        return Scripted(RateLimitError("quota"))

    monkeypatch.setattr("api.llm.factory.get_provider", fake_provider)

    exit_code = runner.main(
        ["--strategy", "loop", "--split", "dev", "--record", "--repeat", "1"]
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Not recorded" in err
    assert "rate limited" in err
    assert not evals_file.exists(), "nothing was appended"


def test_the_evals_path_is_resolved_at_call_time(monkeypatch, tmp_path):
    """**Test isolation around the append-only record, which had a hole.**

    `append_to_evals` took `path=EVALS_PATH` as a default argument, binding the
    constant when the module was first imported. Monkeypatching the module
    attribute therefore did nothing, and a test that believed it was writing to
    a temporary file wrote to the real `EVALS.md` — which is how a fabricated
    entry (`model: fake-model`, accuracy 0.0%) got into the benchmark record
    during T5's mutation run and had to be reverted by hand.

    `006` AC25 makes that record append-only. A file every test can reach by
    accident is not append-only; it is merely usually-untouched, and the
    difference only shows up once.
    """
    import evals.run_evals as runner

    redirected = tmp_path / "EVALS.md"
    monkeypatch.setattr(runner, "EVALS_PATH", redirected)

    runner.append_to_evals("## a block\n")

    assert redirected.exists(), "the patched path was ignored"
    assert "## a block" in redirected.read_text(encoding="utf-8")
