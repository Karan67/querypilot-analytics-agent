"""Multi-pass runs through the **real** recorded path — B-1's first commit.

Four defects of one shape reached `EVALS.md` or its terminal report before this
file existed, and every one survived a fully green suite:

| found | defect |
|---|---|
| T7 | `format_report` recorded `reports[0].usage` under an unqualified `Tokens` heading, three rows above `Passes` |
| T7 | `print_report` showed no cost at all, because the figures lived only in the `--record` path |
| T8 | `format_report` wrote held-out failure detail unconditionally, below a `Test failures revealed \\| no` it had just asserted |
| T8 | the AC18 record refusal inspected `reports[0].cases`, so a rate limit in pass two was filed as a clean measurement |

They share a cause: **the recorded path read the first pass where it had to read
the run**, and nothing exercised `run_evaluation` at `repeat > 1` all the way
through `format_report` to a file. Every existing test either stopped at one
pass, or built `EvalReport`s by hand and skipped the code that assembles them.

So these tests are deliberately end-to-end over the seam that kept breaking:
a real dataset, a real `run_evaluation`, real gold execution through
`execute_sql`, more than one pass, and a real write to a real file — with only
the provider faked, because the provider is the only part that costs money.
"""

from __future__ import annotations

import pytest

from api.llm.base import TokenUsage
from evals.dataset import load_dataset
from evals.run_evals import (
    append_to_evals,
    execute_gold,
    format_report,
    run_evaluation,
)
from api.agent.single_shot import CATEGORY_RATE_LIMITED


def act(name: str, argument: str = "") -> str:
    return f"ACTION: {name}\n{argument}".strip()


class CountingProvider:
    """Answers every question correctly and bills a fixed amount per call.

    A fixed per-call cost is what makes the arithmetic checkable: with `n`
    questions over `p` passes the run must bill exactly `n * p * per_call`, so a
    figure taken from one pass is off by a factor of `p` and cannot hide.
    """

    model = "fake-model"

    def __init__(self, per_call=170, sql="SELECT count(*) FROM track"):
        self._per_call = per_call
        self._sql = sql
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        self.last_usage = TokenUsage(
            prompt_tokens=self._per_call - 20,
            completion_tokens=20,
            calls=1,
            measured=True,
        )
        return act("execute_sql", self._sql)


@pytest.fixture(scope="module")
def dataset():
    return load_dataset()


@pytest.fixture(scope="module")
def gold(dataset):
    return execute_gold(dataset)


def _run(dataset, gold, provider, *, passes, split="dev"):
    return run_evaluation(
        dataset,
        provider,
        repeat=passes,
        strategy="loop",
        split=split,
        check_fingerprint=False,
    )


def test_the_recorded_cost_covers_every_pass(dataset, gold, tmp_path):
    """**The T7 defect, at the level that ships it.**

    `Tokens (all passes)` sits three rows above `Passes` in the recorded block.
    Reading the first pass there under-reported a repeated run by
    `(passes - 1) / passes` — permanently, in an append-only file.
    """
    provider = CountingProvider(per_call=170)
    reports = _run(dataset, gold, provider, passes=3)

    assert len(reports) == 3
    expected = provider.calls * 170
    assert expected == sum(r.usage.total_tokens for r in reports)

    block = format_report(reports, dataset, strategy="loop", split="dev")

    assert f"| Tokens (all passes) | {expected:,} " in block
    assert f"| Provider calls | {provider.calls:,} |" in block
    assert "| Passes | 3 |" in block

    # The load-bearing assertion: a first-pass figure must not satisfy this.
    assert f"| Tokens (all passes) | {reports[0].usage.total_tokens:,} " not in block


def test_a_rate_limit_in_a_later_pass_blocks_the_record(dataset, gold, tmp_path):
    """**The T8 defect.** The guard read `reports[0].cases`, so a run whose
    first pass was a clean sweep and whose second hit the quota was filed as a
    measurement. Driven here through `main`, because the guard lives there and
    the earlier version of this test stopped short of it."""
    import evals.run_evals as runner

    clean = _run(dataset, gold, CountingProvider(), passes=1, split="test")[0]
    limited = _run(dataset, gold, CountingProvider(), passes=1, split="test")[0]
    limited = _with_rate_limit(limited)

    target = tmp_path / "EVALS.md"
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(runner, "EVALS_PATH", target)
        monkey.setattr(runner, "run_evaluation", lambda *a, **k: [clean, limited])
        exit_code = runner.main(["--split", "test", "--record", "--repeat", "2"])
    finally:
        monkey.undo()

    assert exit_code == 1
    assert not target.exists(), "a rate-limited run reached the record"


def _with_rate_limit(report):
    """Re-badge one case as rate limited, keeping everything else real."""
    import dataclasses

    cases = list(report.cases)
    cases[0] = dataclasses.replace(cases[0], category=CATEGORY_RATE_LIMITED, correct=False)
    return dataclasses.replace(report, cases=tuple(cases))


def test_a_multi_pass_record_is_a_pure_append(dataset, gold, tmp_path):
    """`EVALS.md` is append-only (`006` AC25), and a multi-pass block is the
    longest thing ever written to it. Asserted as a byte-exact prefix rather
    than a line count, because a rewrite that happened to preserve the length
    would pass the weaker check."""
    target = tmp_path / "EVALS.md"
    # Seeded as bytes on purpose. `write_text` translates newlines to the
    # platform default, so on Windows the fixture would plant the very CRLF this
    # test then blames on `append_to_evals` -- which is what the first version of
    # it did, and the assertion below was right while the setup was wrong.
    target.write_bytes(b"# Existing record\n\nEarlier entries.\n")
    before = target.read_bytes()

    reports = _run(dataset, gold, CountingProvider(), passes=2)
    append_to_evals(
        format_report(reports, dataset, strategy="loop", split="dev"), path=target
    )

    after = target.read_bytes()
    assert after.startswith(before), "the existing record was rewritten"
    # Scoped to what was appended. The seed above is LF, so a CRLF here can only
    # have come from the writer under test.
    assert b"\r\n" not in after[len(before):], "CRLF written into the record"
    assert after.count(b"| Passes | 2 |") == 1


def test_the_spread_is_reported_only_when_there_is_one(dataset, gold):
    """A single pass has no spread, and printing `100.0%-100.0%` beside a
    one-pass run invites reading determinism into a number that never measured
    it. Iteration 5 learned that the hard way: `007` recorded a 0.0% spread over
    three passes and eight later passes disagreed."""
    one = format_report(
        _run(dataset, gold, CountingProvider(), passes=1), dataset,
        strategy="loop", split="dev",
    )
    many = format_report(
        _run(dataset, gold, CountingProvider(), passes=2), dataset,
        strategy="loop", split="dev",
    )

    assert "spread" not in one.lower()
    assert "spread" in many.lower()
