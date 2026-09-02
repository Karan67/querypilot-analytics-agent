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

from api.agent.prompts import SCHEMA_FULL, SCHEMA_MODES, SYSTEM_TEMPLATE
from api.agent.single_shot import answer_question
from api.db.execution import CATEGORY_REJECTED, execute_sql
from evals.dataset import Dataset, DatasetError, Question, load_dataset, verify_fingerprint
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


def fingerprint_for(strategy: str) -> str:
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
    """
    if strategy == STRATEGY_SINGLE_SHOT:
        return prompt_fingerprint(SYSTEM_TEMPLATE)

    from api.agent.prompts import LOOP_SYSTEM_TEMPLATE, render_transcript

    return prompt_fingerprint(
        LOOP_SYSTEM_TEMPLATE + inspect.getsource(render_transcript)
    )


def build_strategy(strategy: str, schema_mode: str, max_calls: int | None = None):
    """Return the callable that answers one question.

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
        return lambda question, provider: answer_question(question, provider=provider)

    from api.agent.orchestrator import MAX_PROVIDER_CALLS
    from api.agent.orchestrator import answer as loop_answer

    budget = MAX_PROVIDER_CALLS if max_calls is None else max_calls
    return lambda question, provider: loop_answer(
        question, provider=provider, schema_mode=schema_mode, max_calls=budget
    )


def describe_model(provider) -> str:
    """Best-effort model id for the record (AC23).

    `GroqProvider` exposes `model`; the `LLMProvider` protocol does not, and is
    not being widened for a benchmark. A provider that declines to say returns
    "unknown", which is honest and visible in `EVALS.md` rather than silently
    wrong.
    """
    return str(getattr(provider, "model", None) or "unknown")


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


def run_pass(
    dataset: Dataset,
    provider,
    gold: dict,
    strategy: str = STRATEGY_SINGLE_SHOT,
    schema_mode: str = SCHEMA_FULL,
    max_calls: int | None = None,
) -> EvalReport:
    """One full pass over the dataset, in file order (AC22)."""
    strategy_fn = build_strategy(strategy, schema_mode, max_calls)
    cases = [
        run_case(question, gold[question.id], provider, strategy_fn)
        for question in dataset.questions
    ]
    return aggregate(
        cases,
        model=describe_model(provider),
        prompt_fingerprint=fingerprint_for(strategy),
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
        run_pass(dataset, provider, gold, strategy, schema_mode, max_calls)
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

    lines = [
        f"## {stamp} — {strategy}, schema {schema_mode}",
        "",
        "| | |",
        "|---|---|",
        f"| Strategy | `{strategy}` |",
        f"| Schema | `{schema_mode}` |",
        f"| Model | `{first.model}` |",
        f"| Temperature | {first.temperature} |",
        f"| Prompt fingerprint | `{first.prompt_fingerprint}` |",
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

    for tier in ("easy", "medium", "hard"):
        if tier in first.per_tier:
            count = len(dataset.by_tier(tier))
            lines.append(f"| {tier} ({count}) | {_percent(first.per_tier[tier])} |")

    if first.breakdown:
        lines += ["", "| Failure | Count |", "|---|---|"]
        lines += [f"| {name} | {count} |" for name, count in first.breakdown.items()]

    if first.failures:
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

    return "\n".join(lines) + "\n"


def _default_budget(strategy: str) -> int:
    if strategy == STRATEGY_SINGLE_SHOT:
        return 1
    from api.agent.orchestrator import MAX_PROVIDER_CALLS

    return MAX_PROVIDER_CALLS


def append_to_evals(block: str, path: pathlib.Path = EVALS_PATH) -> None:
    """Append one run to `EVALS.md` (AC25).

    Append, never rewrite. Nothing in this module reads the existing content,
    so there is no code path that can drop an earlier entry.
    """
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    separator = "\n---\n\n" if existing.strip() else ""
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(separator + block)


def print_report(reports: list[EvalReport], dataset: Dataset, verbose: bool) -> None:
    first = reports[0]
    accuracies = [r.accuracy for r in reports]

    print()
    print(f"  model              {first.model}")
    print(f"  prompt             {first.prompt_fingerprint}")
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
    for tier in ("easy", "medium", "hard"):
        if tier in first.per_tier:
            in_tier = [c for c in first.cases if c.tier == tier]
            passed = sum(1 for c in in_tier if c.correct)
            print(f"  {tier:8} {passed:3}/{len(in_tier):<3} {_percent(first.per_tier[tier])}")
    if first.breakdown:
        print()
        for name, count in first.breakdown.items():
            print(f"  {name:18} {count}")
    if verbose and first.failures:
        print()
        for case in first.failures:
            print(f"  --- {case.id} [{case.category}] {case.question}")
            if case.generated_sql:
                print(f"      SQL: {case.generated_sql.strip()}")
            if case.error:
                print(f"      {case.error.strip()}")
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

    try:
        build_strategy(args.strategy, args.schema_mode, args.max_calls)
    except ValueError as exc:
        print(f"Invalid combination: {exc}", file=sys.stderr)
        return 2

    print(
        f"Running {len(dataset.questions)} questions x {args.repeat} "
        f"pass(es): strategy={args.strategy}, schema={args.schema_mode}, "
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
        )
    except DatasetError as exc:
        # AC12. Aborting is the point: an incomparable number filed alongside
        # comparable ones is wrong in a way nobody can detect later.
        print(f"Aborted: {exc}", file=sys.stderr)
        return 1

    print_report(reports, dataset, verbose=args.verbose)

    if args.record:
        append_to_evals(
            format_report(
                reports,
                dataset,
                strategy=args.strategy,
                schema_mode=args.schema_mode,
                max_calls=args.max_calls,
            )
        )
        print(f"Recorded in {EVALS_PATH}", file=sys.stderr)
    else:
        print("Not recorded. Pass --record to append to EVALS.md.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
