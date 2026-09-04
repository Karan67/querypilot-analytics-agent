"""The evaluation runner — `specs/006-evals.md` AC11, AC18-AC25.

Produces one number, honestly, and the machinery to reproduce it.

**This module runs the real path.** `answer_question()` unmodified, no test
hook that skips the prompt, the provider or the safety layer (AC19), and the
reference queries go through `execute_sql()` exactly like the generated ones
(AC11, AC20). `000-project.md` §4 names this file explicitly when it says no
code path may execute SQL without passing through the validator, and there is
nothing in here that reaches around it -- not for the gold queries, not for the
fingerprint check.

It also does nothing clever. No retries, no self-correction, no reordering, no
sampling. Iteration 4 adds retries *to the agent*, and the improvement has to
show up as a delta against this; a runner that retried would be measuring
something other than what ships.

Usage::

    python -m evals.run_evals                 # score and print
    python -m evals.run_evals --repeat 3      # three passes, report the spread
    python -m evals.run_evals --repeat 3 --record   # ... and append to EVALS.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import inspect
import os
import pathlib
import statistics
import sys

from api.agent.prompts import (
    ADOPTED_RENDERING,
    SCHEMA_DDL,
    SCHEMA_FULL,
    SCHEMA_MODES,
    SCHEMA_RENDERINGS,
    SYSTEM_TEMPLATE,
    build_loop_system,
    render_schema,
)
from api.db.introspection import get_schema as _introspect_schema
from api.agent.single_shot import answer_question
from api.agent.single_shot import CATEGORY_RATE_LIMITED
from api.db.execution import CATEGORY_REJECTED, execute_sql
from api.llm.base import TokenUsage
from api.llm.pacing import PacedProvider
from api.llm.rate_limits import limit_from_message
from evals import ledger
from api.llm.counting import (
    TokenCountingUnavailable,
    count_tokens,
    project_worst_case,
)
from api.db.introspection import (
    KIND_TABLE,
    KIND_VIEW,
    Column,
    ForeignKey,
    Schema,
    Table,
)
from evals.dataset import (
    SPLIT_TEST,
    SPLITS,
    TIERS,
    Dataset,
    DatasetError,
    Question,
    load_dataset,
    questions_in_split,
    split_fingerprint,
    verify_fingerprint,
)
from evals.scoring import (
    CATEGORY_BROKEN_GOLD,
    CATEGORY_WRONG_RESULT,
    CaseResult,
    EvalReport,
    aggregate,
    results_match,
)

#: Re-exported so the `006-evals.md` §5 contract's import path resolves. They are
#: defined in `scoring.py` because aggregation needs them and the runner needs
#: aggregation.
__all__ = ["CaseResult", "EvalReport", "main", "run_evaluation", "run_pass"]

#: Where the log lives.
EVALS_PATH = pathlib.Path(__file__).resolve().parent.parent / "EVALS.md"

#: Characters of the prompt hash kept for the fingerprint. Twelve hex digits is
#: 48 bits -- unambiguous for a handful of prompt versions and short enough to
#: read in a table.
FINGERPRINT_LENGTH = 12


#: Reproduces the Iteration 3 baseline: `answer_question`, one call, no tools.
STRATEGY_SINGLE_SHOT = "single-shot"

#: The Iteration 4 agent loop.
STRATEGY_LOOP = "loop"

STRATEGIES = (STRATEGY_SINGLE_SHOT, STRATEGY_LOOP)


def prompt_fingerprint(template: str = SYSTEM_TEMPLATE) -> str:
    """A hash of the prompt template (AC24).

    **Derived, not declared.** The alternative -- a version constant somebody
    bumps -- fails in exactly the situation that matters: the prompt changes,
    the constant does not, and every number afterwards is filed under the wrong
    version. A number recorded against the wrong prompt is worse than one
    recorded against none, because it looks comparable.
    """
    return hashlib.sha256(template.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def fingerprint_for(strategy: str, glossary: bool = False) -> str:
    """The prompt fingerprint for one strategy (AC24, resolved D-1).

    Single-shot hashes `SYSTEM_TEMPLATE` alone, exactly as Iteration 3 did, so
    the fingerprint in the existing `EVALS.md` entries still matches and that
    history stays comparable.

    **The loop hashes its template plus the source of `render_transcript`**
    (resolved D-1). The loop's behaviour depends on how failures are fed back as
    much as on the instructions, and a template-only hash would let a change to
    the feedback format produce a different number under an identical
    fingerprint -- precisely the silent mismatch AC24 exists to prevent. The
    cost is that editing a comment in that function changes the fingerprint,
    which is a far cheaper failure than the one it rules out.

    **The glossary is folded in here, unlike the schema rendering** (T3), and
    the asymmetry is deliberate rather than an inconsistency:

    - A rendering change leaves the DDL prompt byte-identical, so churning the
      hash would falsely signal a change that did not happen. It got its own
      `schema_fingerprint` field at T2 to keep Iteration 4's entries comparable.
    - The glossary genuinely **adds 178 tokens to the prompt**. A glossary-on
      run is not the same prompt as Iteration 4's, and recording it under
      `0d280c367c5e` would file an incomparable number as comparable.

    The default is therefore `False`: it is Iteration 4's configuration, and
    calling this with no glossary argument reproduces the recorded value
    exactly. `tests/test_glossary.py` pins both.
    """
    if strategy == STRATEGY_SINGLE_SHOT:
        return prompt_fingerprint(SYSTEM_TEMPLATE)

    from api.agent.glossary import render_glossary
    from api.agent.prompts import LOOP_SYSTEM_TEMPLATE, render_transcript

    material = LOOP_SYSTEM_TEMPLATE + inspect.getsource(render_transcript)
    if glossary:
        material += render_glossary()
    return prompt_fingerprint(material)


#: A tiny hand-built schema, hashed to fingerprint a rendering (spec AC7).
#:
#: **Not the live database.** Hashing the real schema would fold the *contents*
#: of Chinook into a number that is supposed to identify the *renderer*, so a
#: reseed would masquerade as a prompt change. Reseeds are already caught, and
#: caught better, by the dataset row-count fingerprint (`006` AC12).
#:
#: It exercises every feature the compact form can emit, which is what makes the
#: hash sensitive to a renderer change rather than merely to its name: a view
#: and a base table, a nullable and a NOT NULL column, a composite primary key,
#: a foreign key naming its target column, and a type that abbreviates.
_FINGERPRINT_SCHEMA = Schema(
    tables=(
        Table(
            name="parent",
            kind=KIND_TABLE,
            columns=(
                Column(name="parent_id", type="INTEGER", nullable=False, primary_key=True),
                Column(name="label", type="VARCHAR(40)", nullable=True, primary_key=False),
            ),
            foreign_keys=(),
        ),
        Table(
            name="child",
            kind=KIND_TABLE,
            columns=(
                Column(name="parent_id", type="INTEGER", nullable=False, primary_key=True),
                Column(name="seq", type="INTEGER", nullable=False, primary_key=True),
                Column(name="amount", type="NUMERIC(10, 2)", nullable=True, primary_key=False),
            ),
            foreign_keys=(
                ForeignKey(
                    columns=("parent_id",),
                    referred_table="parent",
                    referred_columns=("parent_id",),
                ),
            ),
        ),
        Table(
            name="child_summary",
            kind=KIND_VIEW,
            columns=(
                Column(name="parent_id", type="INTEGER", nullable=True, primary_key=False),
                Column(name="total", type="NUMERIC", nullable=True, primary_key=False),
            ),
            foreign_keys=(),
        ),
    )
)


def schema_fingerprint(rendering: str) -> str:
    """A hash identifying one schema rendering (spec AC7).

    **Recorded beside `prompt_fingerprint`, not folded into it** (resolved T2
    decision). Folding would have changed the loop fingerprint for `ddl` as
    well, and Iteration 4's three recorded runs all carry `0d280c367c5e` --
    re-baselining them to satisfy a new field would break the comparison this
    iteration exists to make.

    **Derived from output, not from the rendering's name.** A name-only hash
    would distinguish `compact` from `ddl` but would not notice `render_schema`
    being edited, which is the blind spot this closes: until now, changing
    `render_schema_ddl` produced an identical fingerprint and a silently
    incomparable number.
    """
    return prompt_fingerprint(f"{rendering}\n{render_schema(_FINGERPRINT_SCHEMA, rendering)}")


def resolve_rendering(strategy: str, rendering: str | None) -> str:
    """The default rendering depends on the strategy, so no constant can be it.

    `single-shot` uses the frozen Iteration 3 template, which renders DDL
    unconditionally (AC1); `build_strategy` rejects anything else rather than
    accepting a flag it would ignore. `loop` gets `ADOPTED_RENDERING`, which
    D-2 set to `compact` at T7.

    Written once and shared by the CLI, `build_strategy` and `run_pass`. Three
    copies of a two-branch rule is how the documented no-argument invocation
    ends up raising a ValueError from its own default -- which is exactly what
    the first version of T7's adoption did.
    """
    if rendering is not None:
        return rendering
    return SCHEMA_DDL if strategy == STRATEGY_SINGLE_SHOT else ADOPTED_RENDERING


def build_strategy(
    strategy: str,
    schema_mode: str,
    max_calls: int | None = None,
    rendering: str | None = None,
    glossary: bool = False,
):
    """Return the callable that answers one question.

    `rendering=None` means "whatever this strategy ships with", resolved by
    `resolve_rendering`.

    Both strategies return objects carrying the same field names, so `run_case`
    scores either through one code path rather than two that could disagree.
    """
    if strategy == STRATEGY_SINGLE_SHOT:
        if schema_mode != SCHEMA_FULL:
            # `answer_question` renders the schema unconditionally and offers no
            # injection point. Running it blind would mean editing it, and AC1
            # keeps it untouched so the Iteration 3 baseline stays reproducible.
            # The blind control is `--strategy loop --max-calls 1` instead,
            # which is a better control anyway: identical prompt, identical
            # protocol, and the budget as the only variable.
            raise ValueError(
                "single-shot cannot run with the schema withheld: it always "
                "renders the schema and has no way to look one up. Use "
                "--strategy loop --max-calls 1 for a one-call control."
            )
        if glossary:
            # `answer_question` uses SYSTEM_TEMPLATE, which has no glossary
            # block and is frozen by AC1. Accepting the flag and ignoring it
            # would record a single-shot run as glossary-on when the model
            # never saw one.
            raise ValueError(
                "single-shot cannot run with a glossary: it uses the frozen "
                "Iteration 3 template. Use --strategy loop, or --no-glossary."
            )
        if rendering != SCHEMA_DDL:
            # `answer_question` renders DDL unconditionally (AC1 keeps it
            # untouched). Accepting the flag and ignoring it would file the
            # run under a rendering that never ran.
            raise ValueError(
                f"single-shot cannot run with rendering {rendering!r}: it "
                f"always renders DDL. Use --strategy loop."
            )
        return lambda question, provider: answer_question(question, provider=provider)

    from api.agent.orchestrator import MAX_PROVIDER_CALLS
    from api.agent.orchestrator import answer as loop_answer

    budget = MAX_PROVIDER_CALLS if max_calls is None else max_calls
    return lambda question, provider: loop_answer(
        question,
        provider=provider,
        schema_mode=schema_mode,
        max_calls=budget,
        rendering=rendering,
        glossary=glossary,
    )


def describe_model(provider) -> str:
    """Best-effort model id for the record (AC23).

    `GroqProvider` exposes `model`; the `LLMProvider` protocol does not, and is
    not being widened for a benchmark. A provider that declines to say returns
    "unknown", which is honest and visible in `EVALS.md` rather than silently
    wrong.
    """
    return str(getattr(provider, "model", None) or "unknown")


def describe_rate_limits(provider) -> str:  # noqa: D401
    """One line of provider rate-limit state, or "" when nothing is reported.

    Read with `getattr`, like `model` and `last_usage` before it (`008` D-1).
    The `LLMProvider` protocol stays one method; a provider that says nothing
    about its limits simply produces no line.
    """
    snapshot = getattr(provider, "last_rate_limit", None)
    if snapshot is None or not snapshot.known:
        return ""
    return snapshot.summary()


def probe_rate_limits(provider) -> str:
    """One minimal call, to learn the limits *before* the run commits to them.

    Measured at 73 tokens against Groq -- under 1% of an 8,000-token bucket and
    one of 1,000 daily requests. It buys two things that are worth more than
    that: a pre-flight reading of how much budget is actually left, and a primed
    pacer, so the first real call is not fired blind into a bucket that a
    previous run may have just drained.

    Uses `complete()` and nothing else, so it needs no addition to the provider
    interface. Best-effort throughout: any failure returns "" and the run
    proceeds, because a diagnostic must never be the thing that stops a
    benchmark.
    """
    named = ""
    try:
        provider.complete("", "Reply with the single word: ok")
    except Exception as exc:  # noqa: BLE001 - a probe may never break a run
        # **Reported, not swallowed.** The first version discarded this, and the
        # discarded text was the only place the binding limit is named -- the
        # headers show a full per-minute bucket while the daily allowance is
        # gone. A diagnostic that hides the diagnosis is worse than none.
        detail = limit_from_message(str(exc))
        if detail is not None:
            code, limit, used = detail
            named = f" -- refused on {code}: {used:,} of {limit:,} used"
        else:
            named = f" -- probe failed: {type(exc).__name__}"

    return (describe_rate_limits(provider) or "rate limits: not reported") + named


def run_case(question: Question, gold, provider, strategy_fn=None) -> CaseResult:
    """Score one question.

    Args:
        question: the case.
        gold: the reference query's already-executed `ExecutionResult`.
        provider: the LLM provider, passed through to the strategy.
        strategy_fn: `(question, provider) -> result`. Defaults to the
            Iteration 3 single-shot path, which is what `006`'s own tests
            were written against and must keep measuring.

    A provider failure is recorded as a failure of this question and nothing
    more (AC21). `answer_question` never raises, so one bad case cannot end a
    run that has forty of them.
    """
    if not gold.ok:
        # The dataset tests make this unreachable in a healthy repo. Handled
        # anyway, and kept out of the model's failure categories: a broken
        # reference query is not evidence about the model, and counting it as
        # `wrong_result` would put a defect of ours into Iteration 5's backlog.
        return CaseResult(
            id=question.id,
            tier=question.tier,
            question=question.question,
            category=CATEGORY_BROKEN_GOLD,
            error=f"reference query failed: {gold.category}: {gold.error}",
        )

    if strategy_fn is None:
        strategy_fn = build_strategy(STRATEGY_SINGLE_SHOT, SCHEMA_FULL)
    answer = strategy_fn(question.question, provider)

    # Recorded where each fact is actually known, rather than reverse-engineered
    # from the category string later. Gate 2 runs before anything else in
    # `execute_sql`, so any category other than `rejected` on a non-empty SQL
    # string means validation passed.
    passed_validation = bool(answer.sql) and answer.category != CATEGORY_REJECTED

    if not answer.ok:
        return CaseResult(
            id=question.id,
            tier=question.tier,
            question=question.question,
            generated_sql=answer.sql,
            passed_validation=passed_validation,
            executed=False,
            category=answer.category,
            error=answer.error,
            # `getattr` because `AnswerResult` (single-shot, frozen by AC1) has
            # no usage field and must not grow one. A strategy that reports
            # nothing costs an empty TokenUsage, which sums to an estimate.
            usage=getattr(answer, "usage", TokenUsage()),
        )

    correct = results_match(
        gold.rows, answer.result.rows, ordered=question.ordered
    )

    return CaseResult(
        id=question.id,
        tier=question.tier,
        question=question.question,
        generated_sql=answer.sql,
        correct=correct,
        passed_validation=True,
        executed=True,
        # `wrong_result` is assigned here and nowhere else. It is the only
        # category meaning the model reasoned incorrectly rather than failed
        # mechanically, and separating it from `database_error` is what makes
        # the breakdown actionable.
        category="" if correct else CATEGORY_WRONG_RESULT,
        error=""
        if correct
        else (
            f"returned {answer.result.row_count} row(s); the reference query "
            f"returns {gold.row_count}"
        ),
        # Recorded on the success path too, and originally was not — which made
        # the whole of AC5 useless, because most questions succeed and every one
        # of them reported zero cost. Caught by a test asserting a completed run
        # carried a measured spend; the run reported nothing at all.
        usage=getattr(answer, "usage", TokenUsage()),
    )


def execute_gold(dataset: Dataset) -> dict:
    """Run every reference query once, through `execute_sql` (AC11, AC20).

    Once per *run*, not once per pass: the dataset is fixed and the fingerprint
    has already established that the data underneath it has not moved, so
    re-running 40 identical queries on each repeat would only cost time. Both
    sides of every comparison still go through the same execution path with the
    same row cap, which is what AC11 is actually about.
    """
    return {question.id: execute_sql(question.gold_sql) for question in dataset.questions}


class TokenBudgetExceeded(RuntimeError):
    """The run would cost more than the configured ceiling (AC8).

    Raised **before the first call** by the projection, and mid-run by the
    in-flight check. Distinct from `DatasetError` so `main` can report the two
    differently: a dataset problem means the benchmark is broken, a budget
    problem means the benchmark is too expensive today.
    """


def project_run_cost(
    dataset: Dataset,
    split: str | None,
    schema_mode: str,
    rendering: str,
    glossary: bool,
    max_calls: int,
) -> int:
    """Worst-case token cost of a run, before it spends anything (AC8).

    **Counted locally**, with `tiktoken`, because a projection by definition
    precedes any provider telemetry. The instrument split is deliberate: this
    number decides whether to start, and the provider's reported usage decides
    whether to stop.

    Worst case means every question burning its full call budget, which is what
    a run of nothing but failures does. The plan is explicit about why: an
    optimistic projection that lets a run die at question 31 wastes the 30 that
    worked and produces a number that is not a measurement.

    Excludes completion tokens, which cannot be known before generating them.
    Stated rather than hidden -- it makes this a floor on the worst case, and the
    in-flight check against reported usage is what covers the gap.
    """
    schema = _introspect_schema() if schema_mode == SCHEMA_FULL else None
    system = build_loop_system(schema, schema_mode, rendering, glossary)

    questions = questions_in_split(dataset, split)
    # The longest question, not the mean: a worst case built from an average is
    # not a worst case.
    longest = max((q.question for q in questions), key=len, default="")

    per_call = count_tokens(system) + count_tokens(longest)
    return project_worst_case(per_call, len(questions), max_calls)


#: The daily token allowance, which **no provider header reports** (B-1).
#:
#: Every other limit in this file is read live from the provider, because asking
#: is more truthful than believing. This one cannot be: it is named only in a
#: 429 body, and by then the run is already refused. 200,000 is Groq's free tier
#: as stated in that body -- `Limit 200000` -- so it is a measured figure rather
#: than a documentation figure, but it is the one number here that goes stale
#: silently if the tier changes. `--daily-token-limit` overrides it.
DEFAULT_DAILY_TOKEN_LIMIT = 200_000


def project_requests(dataset: Dataset, split: str | None, max_calls: int, repeat: int) -> int:
    """Worst-case provider calls for a run.

    Worst case for the same reason `project_run_cost` is (AC8): a run that dies
    at question 31 wastes the 30 that worked. Measured reality is close to one
    call per question -- 60 calls for 60 attempts in Iteration 5's `compact` arm
    -- so this over-states a healthy run threefold and will refuse some runs
    that would have fitted. That is the trade AC8 already made for tokens, kept
    here rather than quietly reversed for requests.
    """
    return len(questions_in_split(dataset, split)) * max_calls * repeat


class QuotaExceeded(RuntimeError):
    """A limit would be crossed. Distinct from `TokenBudgetExceeded`, which is
    about *this run's* ceiling; this is about the account's day."""


def check_quota(
    *,
    projected_tokens: int,
    projected_requests: int,
    spend,
    snapshot,
    daily_token_limit: int,
) -> list[str]:
    """Every reason this run should not start. Empty means go.

    Returns all of them rather than the first. A run refused three times in a
    row, each for a different limit, is a worse experience than one told at the
    outset that it needs a smaller split, a fresh day and a slower pace.

    Each check is skipped when the figure it needs is unavailable, because an
    unknown limit must not become a refusal -- the same rule the pacer follows.
    """
    reasons = []

    # 1. Tokens per day. The ledger, because nothing reports it.
    if spend is not None:
        after = spend.tokens + projected_tokens
        if after > daily_token_limit:
            source = "provider-reconciled" if spend.reconciled else "local estimate"
            reasons.append(
                f"tokens per day: {spend.tokens:,} already spent today "
                f"({source}) plus {projected_tokens:,} projected = {after:,}, "
                f"over the {daily_token_limit:,} limit. This is the limit that "
                f"stopped Iteration 5, and it is not reported in any header."
            )

    if snapshot is None or not snapshot.known:
        return reasons

    # 2. Requests per day. Read live -- the provider reports what is left.
    requests = snapshot.requests
    if requests.remaining is not None and projected_requests > requests.remaining:
        reasons.append(
            f"requests per day: {projected_requests:,} projected against "
            f"{requests.remaining:,} remaining of {requests.limit:,}. "
            f"Unguarded before B-5, and the cheaper limit to exhaust: a "
            f"three-pass dev run is 90 calls."
        )

    # 3. Tokens per minute. Not a refusal -- pacing exists for exactly this --
    #    but the projected wall clock is worth knowing before committing to it.
    tokens = snapshot.tokens
    if tokens.limit and projected_tokens > tokens.limit:
        minutes = projected_tokens / tokens.limit
        if minutes > 1:
            reasons.append(
                f"NOTE tokens per minute: {projected_tokens:,} projected against "
                f"an {tokens.limit:,}/minute bucket, so pacing will stretch this "
                f"run over at least {minutes:.0f} minutes. Not a refusal."
            )

    return reasons


def run_pass(
    dataset: Dataset,
    provider,
    gold: dict,
    strategy: str = STRATEGY_SINGLE_SHOT,
    schema_mode: str = SCHEMA_FULL,
    max_calls: int | None = None,
    rendering: str | None = None,
    glossary: bool = False,
    split: str | None = None,
    token_budget: int | None = None,
) -> EvalReport:
    """One full pass over the selected split, in file order (AC22).

    `split=None` scores the whole corpus. The runner's own default is
    `dev` (see `main`), so reaching the held-out questions takes typing
    `--split test` rather than forgetting a flag.
    """
    rendering = resolve_rendering(strategy, rendering)
    strategy_fn = build_strategy(strategy, schema_mode, max_calls, rendering, glossary)

    cases = []
    spent = TokenUsage(measured=True)
    for question in questions_in_split(dataset, split):
        case = run_case(question, gold[question.id], provider, strategy_fn)
        cases.append(case)
        spent = spent + case.usage

        # The in-flight half of AC8, checked against what the provider actually
        # billed rather than what we projected.
        #
        # This should never fire: the pre-flight projection is worst-case, so a
        # run that was allowed to start cannot legitimately exceed the ceiling.
        # It fires only when the two instruments disagree -- when `tiktoken`
        # under-counts against Groq's tokenizer -- which is precisely the risk
        # D-1 created by using two. A guard against our own measurement being
        # wrong, not redundancy.
        #
        # Only enforced on *measured* spend. Stopping a run because a local
        # estimate crossed a line would be the estimate policing the ceiling it
        # is not denominated in.
        if token_budget is not None and spent.measured and spent.total_tokens > token_budget:
            raise TokenBudgetExceeded(
                f"Stopped after {len(cases)} of "
                f"{len(questions_in_split(dataset, split))} questions: "
                f"{spent.total_tokens:,} tokens billed exceeds the "
                f"{token_budget:,} ceiling. The pre-flight projection allowed "
                f"this run, so the local tokenizer under-counted against the "
                f"provider's -- the projection is the thing to fix."
            )
    return aggregate(
        cases,
        model=describe_model(provider),
        prompt_fingerprint=fingerprint_for(strategy, glossary),
        schema_fingerprint=schema_fingerprint(rendering),
        rendering=rendering,
        glossary=glossary,
        split=split,
        split_fingerprint=split_fingerprint(dataset),
        temperature=_temperature(),
    )


def _temperature() -> float:
    from api.llm.groq_provider import TEMPERATURE

    return TEMPERATURE


def run_evaluation(
    dataset: Dataset,
    provider,
    repeat: int = 1,
    check_fingerprint: bool = True,
    strategy: str = STRATEGY_SINGLE_SHOT,
    schema_mode: str = SCHEMA_FULL,
    max_calls: int | None = None,
    rendering: str | None = None,
    glossary: bool = False,
    split: str | None = None,
    token_budget: int | None = None,
) -> list[EvalReport]:
    """Run the benchmark `repeat` times and return one report per pass.

    Raises:
        DatasetError: if the fingerprint no longer matches (AC12). Deliberately
            before any model call -- a run that cannot produce a comparable
            number should not spend 40 requests discovering that.
    """
    if check_fingerprint:
        verify_fingerprint(dataset)

    gold = execute_gold(dataset)
    return [
        run_pass(
            dataset, provider, gold, strategy, schema_mode, max_calls,
            rendering, glossary, split, token_budget,
        )
        for _ in range(repeat)
    ]


# --- reporting --------------------------------------------------------------


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def format_report(
    reports: list[EvalReport],
    dataset: Dataset,
    strategy: str = STRATEGY_SINGLE_SHOT,
    schema_mode: str = SCHEMA_FULL,
    max_calls: int | None = None,
    rendering: str | None = None,
    glossary: bool = False,
    split: str | None = None,
    revealed: bool = False,
) -> str:
    """The `EVALS.md` block for one run (AC23).

    Append-only in spirit (AC25): bad numbers stay. A regression quietly deleted
    destroys the value of the entire record, and the record is the only reason
    any later accuracy claim means anything.
    """
    first = reports[0]
    accuracies = [r.accuracy for r in reports]
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    headline = _percent(statistics.fmean(accuracies))
    if len(reports) > 1:
        spread = (
            f" (spread {_percent(min(accuracies))}-{_percent(max(accuracies))} "
            f"across {len(reports)} passes)"
        )
    else:
        spread = ""

    rendering = resolve_rendering(strategy, rendering)
    recorded = total_usage(reports)

    lines = [
        f"## {stamp} — {strategy}, schema {schema_mode}, rendering {rendering}, "
        f"split {split or 'all'}",
        "",
        "| | |",
        "|---|---|",
        f"| Strategy | `{strategy}` |",
        f"| Schema | `{schema_mode}` |",
        f"| Model | `{first.model}` |",
        f"| Temperature | {first.temperature} |",
        f"| Prompt fingerprint | `{first.prompt_fingerprint}` |",
        f"| Schema rendering | `{first.rendering}` |",
        f"| Schema fingerprint | `{first.schema_fingerprint}` |",
        f"| Glossary | {'on' if first.glossary else 'off'} |",
        f"| Split | `{first.split or 'all'}` |",
        f"| Split fingerprint | `{first.split_fingerprint}` |",
        # D-3's audit trail. Reading which held-out questions failed is what
        # makes question-level overfitting possible, so obtaining that
        # detail is recorded beside the number it was obtained for.
        f"| Test failures revealed | {'YES' if revealed else 'no'} |",
        # Every pass, not the first: this row sits three lines above
        # `Passes`, and a per-pass figure filed under an unqualified "Tokens"
        # beside it under-reports a repeated run by (passes - 1) / passes.
        # Found at T7; entries written before then are single-pass, where the
        # two readings coincide.
        f"| Tokens (all passes) | {recorded.total_tokens:,} "
        f"({recorded.prompt_tokens:,} prompt + "
        f"{recorded.completion_tokens:,} completion) |",
        # AC5, and D-1's requirement that a figure says which instrument
        # produced it. A billed number and a local count are different
        # quantities; a table that mixed them silently would be unreadable.
        f"| Token source | {'provider (billed)' if recorded.measured else 'local tiktoken (estimated)'} |",
        f"| Provider calls | {recorded.calls:,} |",
        f"| Dataset | `questions.yaml` v{dataset.version}, {first.total} questions |",
        "| Authorship | agent-derived from schema coverage, human-reviewed |",
        f"| Provider-call budget | {max_calls if max_calls is not None else _default_budget(strategy)} |",
        f"| Passes | {len(reports)} |",
        "",
        f"**Execution accuracy: {headline}**{spread}",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Gate 2 pass rate | {_percent(first.gate2_pass_rate)} |",
        f"| Execution rate | {_percent(first.execution_rate)} |",
        "",
        "| Tier | Accuracy |",
        "|---|---|",
    ]

    # Counted over the questions this run actually scored, not the whole
    # corpus. A `--split test` run scores 6 easy questions out of 14, and
    # recording `easy (14) 100.0%` overstates what was measured by more than
    # twice -- permanently, in a file whose only job is to make numbers
    # comparable. Found at T8 alongside the D-3 leak above.
    scored = questions_in_split(dataset, first.split)
    for tier in TIERS:
        if tier in first.per_tier:
            count = sum(1 for question in scored if question.tier == tier)
            lines.append(f"| {tier} ({count}) | {_percent(first.per_tier[tier])} |")

    if first.breakdown:
        lines += ["", "| Failure | Count |", "|---|---|"]
        lines += [f"| {name} | {count} |" for name, count in first.breakdown.items()]

    # D-3, which `print_report` has always applied and this function never did.
    # The consequence was worse here than in the terminal: `format_report` is the
    # path that *persists*, so a held-out run wrote every failing question's text
    # and generated SQL permanently into an append-only file, three rows below a
    # `Test failures revealed | no` that the same block had just asserted.
    # Found at T8 by reading back what was recorded.
    withheld = first.split == SPLIT_TEST and not revealed

    if first.failures and not withheld:
        # AC18. This list is Iteration 5's backlog; a run that printed only a
        # percentage would throw away the one thing that says what to fix.
        lines += ["", "<details>", "<summary>Failing cases (first pass)</summary>", ""]
        for case in first.failures:
            lines.append(f"- **{case.id}** ({case.category}) — {case.question}")
            if case.generated_sql:
                lines.append(f"  - generated: `{case.generated_sql.strip()}`")
            if case.error:
                lines.append(f"  - {case.error.strip()}")
        lines += ["", "</details>"]
    elif first.failures:
        # The count is already in the breakdown above; what is withheld is
        # *which* questions, which is the part that enables overfitting. Saying
        # so in the record makes the absence deliberate rather than an omission
        # a later reader has to guess at.
        lines += [
            "",
            f"{len(first.failures)} failing question(s). Per-question detail is "
            f"withheld: this is the held-out split and "
            f"`--reveal-test-failures` was not passed (D-3).",
        ]

    return "\n".join(lines) + "\n"


def _default_budget(strategy: str) -> int:
    if strategy == STRATEGY_SINGLE_SHOT:
        return 1
    from api.agent.orchestrator import MAX_PROVIDER_CALLS

    return MAX_PROVIDER_CALLS


def append_to_evals(block: str, path: pathlib.Path | None = None) -> None:
    """Append one run to `EVALS.md` (AC25).

    Append, never rewrite. Nothing in this module reads the existing content,
    so there is no code path that can drop an earlier entry.

    **`path` resolves `EVALS_PATH` at call time, not at import time.** It was a
    default argument, which bound the module constant once when the module was
    first imported -- so `monkeypatch.setattr(run_evals, "EVALS_PATH", tmp)` had
    no effect and a test that believed it was writing to a temporary file wrote
    to the real record instead. That is exactly how a fabricated entry
    (`model: fake-model`, accuracy 0.0%) reached `EVALS.md` during T5's mutation
    run, and it had to be reverted by hand.

    The append-only rule (`006` AC25) is only as strong as the isolation around
    it: a record every test can accidentally write to is not append-only, it is
    merely usually-untouched.
    """
    path = EVALS_PATH if path is None else path
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    separator = "\n---\n\n" if existing.strip() else ""
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(separator + block)


def total_usage(reports: list[EvalReport]) -> TokenUsage:
    """Billed usage across every pass of a run.

    `TokenUsage.__add__` makes zero-call usage the identity, so an empty run
    sums to an estimate-free zero rather than silently degrading `measured` --
    the bug T5 shipped and caught.
    """
    spent = TokenUsage()
    for report in reports:
        spent = spent + report.usage
    return spent


def print_report(
    reports: list[EvalReport],
    dataset: Dataset,
    verbose: bool,
    reveal_test_failures: bool = False,
) -> None:
    first = reports[0]
    accuracies = [r.accuracy for r in reports]

    print()
    print(f"  model              {first.model}")
    print(f"  prompt             {first.prompt_fingerprint}")
    # AC7 in the terminal, added at T7. Two arms of the rendering A/B share a
    # prompt fingerprint -- the template and the glossary are identical and only
    # the schema block differs -- so without these three lines the `ddl` and
    # `compact` runs printed byte-identical headers. A comparison whose output
    # cannot say which configuration produced it is not a comparison.
    print(
        f"  rendering          {first.rendering or 'ddl'}  "
        f"({first.schema_fingerprint})"
    )
    print(f"  glossary           {'on' if first.glossary else 'off'}")
    print(f"  split              {first.split or 'all'}  ({first.split_fingerprint})")
    print(f"  dataset            v{dataset.version}, {first.total} questions")
    print(f"  passes             {len(reports)}")
    print()
    print(f"  EXECUTION ACCURACY {_percent(statistics.fmean(accuracies))}", end="")
    if len(reports) > 1:
        print(f"   (spread {_percent(min(accuracies))}-{_percent(max(accuracies))})")
    else:
        print()
    print(f"  gate 2 pass rate   {_percent(first.gate2_pass_rate)}")
    print(f"  execution rate     {_percent(first.execution_rate)}")
    print()
    for tier in TIERS:
        if tier in first.per_tier:
            in_tier = [c for c in first.cases if c.tier == tier]
            passed = sum(1 for c in in_tier if c.correct)
            print(f"  {tier:8} {passed:3}/{len(in_tier):<3} {_percent(first.per_tier[tier])}")
    if first.breakdown:
        print()
        for name, count in first.breakdown.items():
            print(f"  {name:18} {count}")

    # AC5 in the terminal, added at T7 on the first real run of this iteration.
    # `format_report` has carried these figures into `EVALS.md` since T5, but
    # `print_report` never showed them -- so a run without `--record`, which is
    # every tuning run by design, reported no cost at all. That is exactly the
    # blindness Iteration 4 had when it discovered the daily ceiling as 31
    # provider errors with no visibility.
    #
    # Summed over passes rather than taken from the first, because the quota is
    # charged for all of them and a per-pass figure is the wrong denominator
    # for deciding whether the next arm fits in the day.
    spent = total_usage(reports)
    print()
    print(
        f"  tokens             {spent.total_tokens:,}"
        f"   ({spent.prompt_tokens:,} prompt + "
        f"{spent.completion_tokens:,} completion)"
    )
    if len(reports) > 1:
        print(f"  per pass           {spent.total_tokens // len(reports):,} mean")
    print(f"  provider calls     {spent.calls:,}")
    # Which instrument produced the figure, never left to be inferred: a local
    # count and a billed count are different quantities (D-1).
    source = "provider (billed)" if spent.measured else "local tiktoken (estimated)"
    print(f"  token source       {source}")
    # D-3's guard. Aggregate and per-tier numbers above always print; the
    # per-question case list is what makes question-level overfitting possible,
    # so on the held-out split obtaining it takes an explicit flag that is then
    # recorded in EVALS.md beside the number.
    #
    # **Not a hard block, deliberately.** A block would eventually be worked
    # around by querying the database by hand, and an unlogged bypass is worse
    # than a logged look. This makes looking auditable, not impossible.
    withheld = first.split == SPLIT_TEST and not reveal_test_failures

    if verbose and first.failures and not withheld:
        print()
        for case in first.failures:
            print(f"  --- {case.id} [{case.category}] {case.question}")
            if case.generated_sql:
                print(f"      SQL: {case.generated_sql.strip()}")
            if case.error:
                print(f"      {case.error.strip()}")
    elif withheld and first.failures:
        print()
        print(
            f"  {len(first.failures)} failing question(s) on the held-out split. "
            f"Per-question detail is withheld;"
        )
        print(
            "  pass --reveal-test-failures to see it. Doing so is recorded in "
            "EVALS.md."
        )
    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run_evals",
        description="Score the single-shot agent against evals/questions.yaml.",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help=(
            "run the dataset N times and report the spread. Determinism was "
            "measured at 1 distinct SQL over 5 identical calls, but on one "
            "question; the baseline is recorded with 3 so the noise is "
            "documented rather than assumed (resolved Q-E)."
        ),
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help=(
            "append the result to EVALS.md. Off by default (resolved D-1): the "
            "value of that file is that every entry is a real measurement, and "
            "debugging runs appending to it would destroy exactly that."
        ),
    )
    parser.add_argument(
        "--strategy",
        choices=STRATEGIES,
        default=STRATEGY_LOOP,
        help=(
            "which agent answers the questions (resolved Q-E). `single-shot` "
            "reproduces the Iteration 3 baseline; `loop` is the Iteration 4 "
            "agent. Recorded in EVALS.md, because a number is only comparable "
            "to another taken the same way."
        ),
    )
    parser.add_argument(
        "--schema",
        choices=SCHEMA_MODES,
        default=SCHEMA_FULL,
        dest="schema_mode",
        help=(
            "`full` renders the schema into the prompt; `withheld` makes the "
            "agent look it up. `withheld` is the secondary benchmark of "
            "resolved Q-A -- the case the loop actually exists for."
        ),
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=None,
        dest="max_calls",
        help=(
            "override the loop's provider-call budget. Its purpose is "
            "`--max-calls 1`, the one-call control for the withheld harness: "
            "same prompt, same protocol, budget as the only variable."
        ),
    )
    parser.add_argument(
        "--rendering",
        choices=SCHEMA_RENDERINGS,
        default=None,
        help=(
            "how the schema is rendered into the prompt (spec AC6). `ddl` is "
            "the default because every earlier number was measured against "
            "it. Each rendering has its own schema fingerprint, so runs "
            "cannot be compared across renderings by accident (AC7)."
        ),
    )
    glossary_group = parser.add_mutually_exclusive_group()
    glossary_group.add_argument(
        "--glossary",
        action="store_true",
        dest="glossary",
        default=None,
        help=(
            "inject the business-term block (spec AC10). Defaults to on for "
            "--strategy loop, which is what a deployment ships, and off for "
            "single-shot, which uses the frozen Iteration 3 template and "
            "cannot carry one."
        ),
    )
    glossary_group.add_argument(
        "--no-glossary",
        action="store_false",
        dest="glossary",
        help=(
            "omit the block. A glossary-off loop run reproduces Iteration "
            "4's prompt byte for byte, which is what makes it AC13's "
            "control rather than merely a cheaper run."
        ),
    )
    parser.add_argument(
        "--split",
        choices=(*SPLITS, "all"),
        default="dev",
        help=(
            "which half of the corpus to score (spec Q-A). Defaults to `dev`, "
            "so tuning runs cannot touch the held-out questions by omission — "
            "reaching them takes typing `--split test`. `all` scores the whole "
            "corpus, which is what earlier iterations' numbers were taken over."
        ),
    )
    parser.add_argument(
        "--reveal-test-failures",
        action="store_true",
        dest="reveal_test_failures",
        help=(
            "print per-question failure detail for a `test` run. Aggregate and "
            "per-tier numbers always print; only the case list is gated, "
            "because knowing *which* held-out questions failed is what enables "
            "question-level overfitting. Using this is recorded in EVALS.md."
        ),
    )
    parser.add_argument(
        "--token-budget",
        type=int,
        default=None,
        dest="token_budget",
        help=(
            "the live circuit breaker: stop a pass once the provider has "
            "billed this many tokens (AC8). Denominated in the provider's "
            "count, which is the one the 200000-a-day free tier is charged "
            "in. Also supplies the pre-flight ceiling unless "
            "--max-projection overrides it."
        ),
    )
    parser.add_argument(
        "--max-projection",
        type=int,
        default=None,
        dest="max_projection",
        help=(
            "override the pre-flight ceiling only, leaving --token-budget to "
            "police billed spend. The two guards are denominated differently "
            "-- the projection is a local worst case over the whole "
            "invocation, the breaker is the provider's bill for one pass -- "
            "so a single number cannot set both without making one of them "
            "vacuous. Raising this authorises a run the projection would "
            "refuse, and is the only way to keep a meaningful breaker while "
            "doing so."
        ),
    )
    parser.add_argument(
        "--daily-token-limit",
        type=int,
        default=DEFAULT_DAILY_TOKEN_LIMIT,
        dest="daily_token_limit",
        help=(
            "the account's tokens-per-day allowance (B-5). Distinct from "
            "--token-budget, which is this run's own ceiling: this is checked "
            "against what the whole day has already spent. A constant rather "
            "than a live reading because no header reports it -- it is named "
            "only in a 429 body, by which point the run is already refused."
        ),
    )
    parser.add_argument(
        "--ignore-daily-spend",
        action="store_true",
        dest="ignore_daily_spend",
        help=(
            "skip the accumulated-spend guard and the ledger write. For a run "
            "against a different key than the ledger was built from, where its "
            "totals describe someone else's day."
        ),
    )
    parser.add_argument("--dataset", type=pathlib.Path, default=None, help="dataset path")
    parser.add_argument(
        "--verbose", action="store_true", help="print every failing case with its SQL"
    )
    return parser


def _load_dotenv() -> None:
    """Make `.env` visible to a host-side run.

    The same arrangement `tests/conftest.py` uses, and for the same reason: the
    container gets its environment from docker compose, which is the 12-factor
    setup, but a benchmark run on the host has no such injector. Called from
    `main()` only, never from the library functions, so importing this module
    never touches the environment.

    Existing environment always wins, so CI can override without editing files.
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _load_dotenv()

    if args.repeat < 1:
        print("--repeat must be at least 1", file=sys.stderr)
        return 2

    try:
        dataset = load_dataset(args.dataset)
    except DatasetError as exc:
        print(f"Dataset error: {exc}", file=sys.stderr)
        return 2

    from api.llm.base import LLMError
    from api.llm.factory import get_provider

    try:
        provider = get_provider()
    except LLMError as exc:
        print(f"Provider error: {exc}", file=sys.stderr)
        return 2

    # Pacing is opt-in by wrapping, and only the benchmark opts in (B-1). The
    # deployed API must not sleep inside a user's request: that would trade a
    # rare 429 for a guaranteed delay on every call. `PacedProvider` satisfies
    # the same one-method interface, so nothing downstream can tell.
    provider = PacedProvider(provider)

    # Resolved here rather than by argparse: the sensible default depends on the
    # strategy. Loop runs ship with the glossary; single-shot cannot carry one at
    # all, and defaulting it on would break the documented no-argument
    # invocation with a ValueError.
    glossary = args.glossary
    if glossary is None:
        glossary = args.strategy == STRATEGY_LOOP

    # Same reason, same shape (T7). `single-shot` renders DDL unconditionally
    # and `build_strategy` refuses any other rendering, so defaulting the flag
    # to the adopted `compact` would break the documented no-argument run with
    # a ValueError from its own default.
    rendering = resolve_rendering(args.strategy, args.rendering)

    split = None if args.split == "all" else args.split

    try:
        build_strategy(
            args.strategy, args.schema_mode, args.max_calls, rendering, glossary
        )
    except ValueError as exc:
        print(f"Invalid combination: {exc}", file=sys.stderr)
        return 2

    try:
        selected = questions_in_split(dataset, split)
    except DatasetError as exc:
        print(f"Dataset error: {exc}", file=sys.stderr)
        return 2

    if args.reveal_test_failures and split != SPLIT_TEST:
        # Not an error, but worth saying: the flag only gates the held-out
        # split, and a reader of EVALS.md should not see it recorded against a
        # run where it did nothing.
        print(
            "Note: --reveal-test-failures only affects --split test; ignoring.",
            file=sys.stderr,
        )

    # AC8's two guards, given two knobs. Before T7 they shared one, which made
    # the authorisation the plan anticipated -- raise the ceiling past the
    # projection, keep the breaker -- impossible to express: the single value
    # had to clear a 306,720-token worst-case projection, which left the
    # breaker with 7x headroom over a pass that actually bills ~41,000. It
    # could not fire, so it was not a guard.
    projection_ceiling = (
        args.max_projection if args.max_projection is not None else args.token_budget
    )

    if projection_ceiling is not None:
        try:
            projected = project_run_cost(
                dataset,
                split,
                args.schema_mode,
                rendering,
                glossary,
                args.max_calls or _default_budget(args.strategy),
            ) * args.repeat
        except TokenCountingUnavailable as exc:
            # Refuse rather than guess. A budget policed by a heuristic is
            # the failure this iteration spent T1 correcting.
            print(f"Cannot project cost: {exc}", file=sys.stderr)
            return 2

        if projected > projection_ceiling:
            # AC8: nothing has been spent at this point, and nothing will be.
            print(
                f"Aborted before spending anything: worst case is "
                f"{projected:,} tokens against a {projection_ceiling:,} "
                f"ceiling. Narrow with --split, or raise --max-projection.",
                file=sys.stderr,
            )
            return 1
        print(f"Projected worst case: {projected:,} tokens.", file=sys.stderr)
        # Stated next to the projection it should be read against. The
        # projection is a local worst case in *tokens per run*; the limits below
        # are the provider's, and Iteration 5 discovered the hard way that they
        # are not denominated in the same thing -- 200,000 tokens a day was
        # never the constraint, 8,000 a minute was.
        limits = probe_rate_limits(provider)
        if limits:
            print(f"Pre-flight {limits}", file=sys.stderr)
        if args.max_projection is not None:
            # Said out loud, on the run it applies to. A raised ceiling is a
            # deliberate act and should not be inferable only from shell
            # history.
            print(
                f"Pre-flight ceiling raised to {args.max_projection:,} by "
                f"--max-projection; billed spend is policed at "
                + (
                    f"{args.token_budget:,} per pass."
                    if args.token_budget is not None
                    else "nothing -- no --token-budget was given."
                ),
                file=sys.stderr,
            )

    # B-5. Three limits apply to this account and AC8 knew about one. The probe
    # below is the same call the pre-flight telemetry makes, so it costs one
    # request rather than two.
    spend = None if args.ignore_daily_spend else ledger.load()
    limits_line = probe_rate_limits(provider)
    snapshot = getattr(provider, "last_rate_limit", None)

    if limits_line:
        print(f"Pre-flight {limits_line}", file=sys.stderr)
    if spend is not None:
        print(f"Pre-flight {spend.describe(args.daily_token_limit)}", file=sys.stderr)

    # A refusal names the provider's own total, which is worth more than the
    # ledger's estimate. Taking it here means even a blocked run improves the
    # figure the next one is judged against.
    named = limit_from_message(limits_line)
    if named is not None and named[0] == "TPD" and not args.ignore_daily_spend:
        spend = ledger.reconcile(named[2])
        print(
            f"Ledger reconciled from the provider: {spend.tokens:,} used today.",
            file=sys.stderr,
        )

    try:
        projected_tokens = project_run_cost(
            dataset,
            split,
            args.schema_mode,
            rendering,
            glossary,
            args.max_calls or _default_budget(args.strategy),
        ) * args.repeat
    except TokenCountingUnavailable:
        projected_tokens = 0

    reasons = check_quota(
        projected_tokens=projected_tokens,
        projected_requests=project_requests(
            dataset,
            split,
            args.max_calls or _default_budget(args.strategy),
            args.repeat,
        ),
        spend=spend,
        snapshot=snapshot,
        daily_token_limit=args.daily_token_limit,
    )
    for reason in reasons:
        print(f"  {reason}", file=sys.stderr)

    blocking = [r for r in reasons if not r.startswith("NOTE")]
    if blocking:
        # Nothing has been spent beyond the probe. Refusing here is the point:
        # crossing these mid-run produces a rate-limited result that AC18 then
        # refuses to record, so the run costs quota and buys nothing.
        print(
            f"Aborted before the run: {len(blocking)} limit(s) would be crossed.",
            file=sys.stderr,
        )
        return 1
    print(
        f"Running {len(selected)} questions x {args.repeat} "
        f"pass(es): strategy={args.strategy}, schema={args.schema_mode}, "
        f"rendering={rendering}, "
        f"glossary={'on' if glossary else 'off'}, split={args.split}, "
        f"model={describe_model(provider)}...",
        file=sys.stderr,
    )

    try:
        reports = run_evaluation(
            dataset,
            provider,
            repeat=args.repeat,
            strategy=args.strategy,
            schema_mode=args.schema_mode,
            max_calls=args.max_calls,
            rendering=rendering,
            glossary=glossary,
            split=split,
            token_budget=args.token_budget,
        )
    except TokenBudgetExceeded as exc:
        print(f"Aborted: {exc}", file=sys.stderr)
        return 1
    except DatasetError as exc:
        # AC12. Aborting is the point: an incomparable number filed alongside
        # comparable ones is wrong in a way nobody can detect later.
        print(f"Aborted: {exc}", file=sys.stderr)
        return 1

    print_report(
        reports,
        dataset,
        verbose=args.verbose,
        reveal_test_failures=args.reveal_test_failures,
    )

    # After the run, because the interesting number is what the run *left* --
    # a pass that finishes with the bucket near empty is one that was about to
    # start failing, and that is invisible in an accuracy figure.
    limits = describe_rate_limits(provider)
    if limits:
        print(f"\n  {limits}")
    if getattr(provider, "waits", 0):
        print(
            f"  paced             {provider.waits} wait(s), "
            f"{provider.total_slept:.0f}s total"
        )

    # Every pass, not the first. A three-pass run whose second pass hit the
    # quota would have been recorded as a clean measurement, because this read
    # `reports[0]` -- the same pass-one blind spot as the token total fixed at
    # T7 and the D-3 leak fixed at T8, in the same function. The guard is only
    # worth having if it sees the whole run it is guarding.
    # B-5: the day's running total, written whether or not the run is
    # recorded. `EVALS.md` is about accuracy and refuses a rate-limited run; the
    # ledger is about quota, and a rate-limited run spent it just the same.
    spent_now = total_usage(reports)
    if not args.ignore_daily_spend and spent_now.measured:
        after = ledger.record(spent_now.total_tokens, spent_now.calls)
        print(f"  {after.describe(args.daily_token_limit)}")

    rate_limited = sum(
        1
        for report in reports
        for case in report.cases
        if case.category == CATEGORY_RATE_LIMITED
    )
    if args.record and rate_limited:
        # AC18: no prompt change is accepted on a run that was rate limited,
        # because the comparison would be against the clock rather than the
        # prompt. Iteration 4's 82.5% was 33 correct and 7 rate-limited, and
        # it was read as a measurement for weeks.
        #
        # The number still printed above. What is refused is filing it
        # alongside comparable ones, where it would invite exactly that
        # reading.
        print(
            f"Not recorded: {rate_limited} question(s) were rate limited, "
            f"so this number is a floor rather than a measurement (AC18). "
            f"Re-run when the quota resets.",
            file=sys.stderr,
        )
        return 1

    if args.record:
        append_to_evals(
            format_report(
                reports,
                dataset,
                strategy=args.strategy,
                schema_mode=args.schema_mode,
                max_calls=args.max_calls,
                rendering=rendering,
                glossary=glossary,
                split=split,
                # Only meaningful on a test run, and only recorded as YES when
                # it actually revealed something.
                revealed=args.reveal_test_failures and split == SPLIT_TEST,
            )
        )
        print(f"Recorded in {EVALS_PATH}", file=sys.stderr)
    else:
        print("Not recorded. Pass --record to append to EVALS.md.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
