"""Runner tests — `specs/006-evals.md` AC11, AC18-AC25.

Scored against a **fake provider with scripted responses**, so a full pass runs
with no API key and no network. That is not only convenience: it means the
metric arithmetic is verified against outcomes chosen by the test rather than
whatever the model happened to do that afternoon, which is the only way to
assert that a `wrong_result` is counted as a `wrong_result`.

The database is real. Both sides of every comparison go through `execute_sql`
(AC11), and faking that would prove nothing about the property under test.

Most tests run against an eight-question subset. Each question costs a schema
read and two round trips, and a test asserting how one failure is categorised
does not need forty of them — the full corpus is used where the corpus is the
subject.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest

from api.llm.base import LLMError
from evals.dataset import load_dataset
from evals.run_evals import (
    append_to_evals,
    describe_model,
    execute_gold,
    format_report,
    prompt_fingerprint,
    run_evaluation,
    run_pass,
)
from evals.scoring import CATEGORY_BROKEN_GOLD, CATEGORY_WRONG_RESULT

SUBSET = (
    "easy-001",
    "easy-002",
    "easy-003",
    "easy-004",
    "medium-001",
    "medium-002",
    "hard-003",
    "hard-008",
)


class ScriptedProvider:
    """A perfect model by default: it answers each question with that question's
    own gold query, and named questions are overridden to fail in a chosen way.

    **Keyed on the question text, not on call order.** The first version indexed
    a list by call count, and it broke the moment a case short-circuited without
    reaching the provider: a broken gold consumed no response, every later
    question received the previous question's SQL, and 39 of 40 scored
    `wrong_result` for no reason connected to the code under test. Keying on the
    prompt makes that impossible, and incidentally asserts that the runner
    passes each question through verbatim.
    """

    def __init__(self, dataset, overrides: dict | None = None, model: str = "fake-model"):
        self._by_question = {q.question: q.gold_sql for q in dataset.questions}
        for question_id, response in (overrides or {}).items():
            question = next(q for q in dataset.questions if q.id == question_id)
            self._by_question[question.question] = response
        self.model = model
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        response = self._by_question[user]
        if isinstance(response, Exception):
            raise response
        return response


class LoopProvider(ScriptedProvider):
    """`ScriptedProvider`, but speaking the loop's action protocol.

    The bare-SQL responses the single-shot tests use would reach the loop as
    `explicit=False`, which changes how a rejection is categorised — so scoring
    the loop needs a provider that actually emits an `ACTION:` line, exactly as
    the real model does.
    """

    def complete(self, system: str, user: str) -> str:
        # The loop sends a transcript, not the bare question, so the lookup key
        # is the `Question:` line the transcript opens with. That keeps the
        # provider keyed on the question rather than on call order — the same
        # property that stopped the single-shot fake desynchronising.
        self.calls += 1
        question = user.splitlines()[0].removeprefix("Question: ")
        response = self._by_question[question]
        if isinstance(response, Exception):
            raise response
        return f"ACTION: execute_sql\n{response}"


@pytest.fixture(scope="module")
def full():
    return load_dataset()


@pytest.fixture(scope="module")
def dataset(full):
    """Eight questions spanning the three tiers."""
    chosen = tuple(q for q in full.questions if q.id in SUBSET)
    assert len(chosen) == len(SUBSET), "the subset names a question that moved"
    return dataclasses.replace(full, questions=chosen)


@pytest.fixture(scope="module")
def gold(dataset, configured_database):
    return execute_gold(dataset)


@pytest.fixture(scope="module")
def full_gold(full, configured_database):
    return execute_gold(full)


# --- the corpus agrees with the scorer --------------------------------------


def test_single_shot_still_defaults_to_ddl():
    """`answer_question` renders DDL unconditionally and `build_strategy`
    refuses any other rendering rather than accepting a flag it would ignore.

    So the adopted default cannot be applied blindly: a bare
    `--strategy single-shot` would raise a ValueError from its own default.
    That is not hypothetical -- it is what the first version of T7's adoption
    did, and it is the same trap the glossary default hit at T3.
    """
    from api.agent.prompts import ADOPTED_RENDERING, SCHEMA_DDL
    from evals.run_evals import resolve_rendering

    assert resolve_rendering("single-shot", None) == SCHEMA_DDL
    assert resolve_rendering("loop", None) == ADOPTED_RENDERING


def test_an_explicit_rendering_always_wins():
    """Resolution fills a blank; it never overrides an answer.

    A resolver that ignored its argument would silently score every run under
    the adopted rendering, including the `ddl` arm of an A/B -- which would make
    the comparison that selected the adopted rendering unfalsifiable.
    """
    from api.agent.prompts import ADOPTED_RENDERING, SCHEMA_DDL
    from evals.run_evals import resolve_rendering

    assert resolve_rendering("loop", SCHEMA_DDL) == SCHEMA_DDL
    assert resolve_rendering("single-shot", SCHEMA_DDL) == SCHEMA_DDL


def test_a_perfect_run_over_the_whole_corpus_scores_100_percent(full, full_gold):
    """Echoing each gold query back must score 100% on all forty.

    Worth the forty calls: if it does not hold, the scorer and the dataset
    disagree somewhere, and every other number in this file is measuring that
    disagreement rather than the runner. It is also the only test that would
    catch a question whose gold is non-deterministic between two executions.
    """
    report = run_pass(full, ScriptedProvider(full), full_gold)

    assert report.accuracy == 1.0, [c.id for c in report.failures]
    assert report.correct == report.total == 50
    assert report.breakdown == {}
    assert report.per_tier == {
        "easy": 1.0,
        "medium": 1.0,
        "hard": 1.0,
        "expert": 1.0,
    }


# --- AC13-AC17: the arithmetic ----------------------------------------------


def test_the_runner_asks_the_model_once_per_question(dataset, gold):
    """No retries in the runner. Iteration 4 adds them to the agent, and the
    improvement has to show up as a delta against this."""
    provider = ScriptedProvider(dataset)
    run_pass(dataset, provider, gold)
    assert provider.calls == len(dataset.questions)


def test_a_wrong_but_valid_answer_is_wrong_result(dataset, gold):
    """**The category that matters most.** Everything worked mechanically and
    the rows were still wrong — a reasoning failure, not a mechanical one, and
    the fix for it is completely different."""
    provider = ScriptedProvider(dataset, {"easy-001": "SELECT count(*) FROM artist"})
    report = run_pass(dataset, provider, gold)

    case = next(c for c in report.cases if c.id == "easy-001")
    assert case.correct is False
    assert case.category == CATEGORY_WRONG_RESULT
    assert case.passed_validation is True, "it validated fine; it was just wrong"
    assert case.executed is True, "it ran fine; it was just wrong"
    assert report.correct == len(dataset.questions) - 1


def test_a_rejected_answer_is_recorded_as_rejected(dataset, gold):
    """Gate 2 refused it, so it never ran. A prompting problem, not a reasoning
    one."""
    provider = ScriptedProvider(dataset, {"easy-002": "DROP TABLE track"})
    report = run_pass(dataset, provider, gold)

    case = next(c for c in report.cases if c.id == "easy-002")
    assert case.category == "rejected"
    assert case.passed_validation is False
    assert case.executed is False
    assert case.generated_sql == "DROP TABLE track", "AC18 — the SQL is kept"


def test_a_bad_column_is_recorded_as_a_database_error(dataset, gold):
    """Passed Gate 2, refused by PostgreSQL. A schema-grounding problem — the
    model did not know what exists."""
    provider = ScriptedProvider(dataset, {"easy-003": "SELECT no_such_column FROM album"})
    report = run_pass(dataset, provider, gold)

    case = next(c for c in report.cases if c.id == "easy-003")
    assert case.category == "database_error"
    assert case.passed_validation is True
    assert case.executed is False


def test_a_refusal_is_not_scored_as_correct(dataset, gold):
    provider = ScriptedProvider(
        dataset, {"easy-004": "I'm sorry, but I can't help with that."}
    )
    report = run_pass(dataset, provider, gold)

    case = next(c for c in report.cases if c.id == "easy-004")
    # Prose reaches Gate 2 and is refused as unparseable — the correct outcome,
    # and the measured one. What matters here is that it is not scored right.
    assert case.correct is False
    assert case.category in {"rejected", "no_sql_returned"}


def test_ac21_a_provider_failure_does_not_abort_the_run(dataset, gold):
    """One bad question must cost one question, not the run."""
    provider = ScriptedProvider(
        dataset,
        {"easy-002": LLMError("Groq request failed. RuntimeError: boom")},
    )
    report = run_pass(dataset, provider, gold)

    assert report.total == len(dataset.questions), "the other questions still ran"
    failed = next(c for c in report.cases if c.id == "easy-002")
    assert failed.category == "provider_error"
    assert failed.passed_validation is False
    assert report.correct == len(dataset.questions) - 1


def test_a_mixed_run_reports_every_metric_consistently(dataset, gold):
    """The whole pipeline at once: correct, wrong, rejected, database error."""
    provider = ScriptedProvider(
        dataset,
        {
            "easy-001": "SELECT count(*) FROM artist",  # wrong_result
            "easy-002": "DROP TABLE track",  # rejected
            "easy-003": "SELECT nope FROM album",  # database_error
        },
    )
    report = run_pass(dataset, provider, gold)

    assert report.correct == len(dataset.questions) - 3
    assert sum(report.breakdown.values()) == 3
    assert report.breakdown["wrong_result"] == 1
    assert report.breakdown["rejected"] == 1
    assert report.breakdown["database_error"] == 1
    assert report.accuracy <= report.execution_rate <= report.gate2_pass_rate


def test_ac17_per_tier_accuracy_splits_the_headline(dataset, gold):
    """"89%" must not be able to hide a tier at zero."""
    hard_ids = {q.id for q in dataset.by_tier("hard")}
    provider = ScriptedProvider(dataset, {qid: "SELECT 1" for qid in hard_ids})
    report = run_pass(dataset, provider, gold)

    assert report.per_tier["easy"] == 1.0
    assert report.per_tier["medium"] == 1.0
    assert report.per_tier["hard"] == 0.0
    assert 0.0 < report.accuracy < 1.0


def test_ac18_failures_carry_their_sql_and_reason(dataset, gold):
    provider = ScriptedProvider(dataset, {"easy-001": "SELECT count(*) FROM artist"})
    report = run_pass(dataset, provider, gold)

    failure = report.failures[0]
    assert failure.generated_sql == "SELECT count(*) FROM artist"
    assert failure.error, "a failure with no reason is not a backlog item"


#: The `medium-001` answer with the correct three rows in the wrong order.
#:
#: The obvious way to write this — swapping `DESC` for `ASC` in the gold — does
#: not test ordering at all: it returns the three *least* prolific artists, so
#: it fails on content and passes whether or not the runner honours the flag.
#: A mutation run caught that; ignoring `ordered` entirely left all 37 tests
#: green. Sorting the correct top three ascending is the discriminating case.
SAME_ROWS_WRONG_ORDER = """
SELECT name, n FROM (
  SELECT ar.name AS name, count(*) AS n FROM album al
  JOIN artist ar ON ar.artist_id = al.artist_id
  GROUP BY ar.artist_id, ar.name
  ORDER BY count(*) DESC LIMIT 3
) top_three
ORDER BY n ASC
"""


def test_the_ordered_flag_reaches_the_comparison(dataset, gold):
    """AC10 end to end: the flag has to travel from the YAML, through the
    runner, into `results_match`. `medium-001` is ordered, so the right three
    rows in the wrong order is a wrong answer."""
    ordered = next(q for q in dataset.questions if q.id == "medium-001")
    assert ordered.ordered is True

    report = run_pass(
        dataset, ScriptedProvider(dataset, {"medium-001": SAME_ROWS_WRONG_ORDER}), gold
    )
    case = next(c for c in report.cases if c.id == "medium-001")

    assert case.executed is True, "the answer ran fine; only its order is wrong"
    assert case.correct is False
    assert case.category == CATEGORY_WRONG_RESULT


def test_the_same_rows_would_pass_an_unordered_question(dataset, gold):
    """The other half of the discriminator. `medium-002` is unordered, so this
    proves the previous test failed for the ordering and not for the rows —
    without it, any answer-shaped difference would satisfy that assertion."""
    from evals.scoring import results_match

    from api.db.execution import execute_sql

    permuted = execute_sql(SAME_ROWS_WRONG_ORDER)
    assert permuted.ok, permuted.error

    reference = gold["medium-001"]
    assert results_match(reference.rows, permuted.rows, ordered=False) is True
    assert results_match(reference.rows, permuted.rows, ordered=True) is False


# --- AC22: determinism ------------------------------------------------------


def test_ac22_the_same_script_produces_the_same_report(dataset, gold):
    """Everything except the model is deterministic. Structural equality on
    frozen dataclasses is what asserts it."""
    overrides = {"easy-001": "SELECT count(*) FROM artist"}
    first = run_pass(dataset, ScriptedProvider(dataset, overrides), gold)
    second = run_pass(dataset, ScriptedProvider(dataset, overrides), gold)
    assert first == second


def test_ac22_cases_are_reported_in_dataset_order(dataset, gold):
    report = run_pass(dataset, ScriptedProvider(dataset), gold)
    assert [c.id for c in report.cases] == [q.id for q in dataset.questions]


# --- AC11 / AC20: no bypass -------------------------------------------------


def test_ac20_the_runner_never_reaches_around_the_validator():
    """`000-project.md` §4 names `run_evals.py` explicitly.

    Asserted against the parsed module rather than its text. A source grep
    matches its own docstring, which has caught this project out twice — once in
    the `003` bypass test and again in `005` AC20.
    """
    import ast

    tree = ast.parse(pathlib.Path("evals/run_evals.py").read_text(encoding="utf-8"))

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    } | {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    for forbidden in ("get_engine", "create_engine", "connect", "raw_connection", "text"):
        assert forbidden not in called, (
            f"run_evals.py calls {forbidden}() — every query must go through "
            f"execute_sql()"
        )

    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "execute_sql" in imported
    assert "validate_sql" not in imported, (
        "the runner must not call the validator itself — execute_sql owns the "
        "gate ordering, and two callers could disagree"
    )


def test_ac11_both_sides_go_through_execute_sql(gold):
    """The reference results the runner compares against are `ExecutionResult`s,
    so they carry the same row cap and truncation as the generated side. A
    comparison where one side was capped and the other was not would silently
    mis-score every large result."""
    from api.db.execution import ExecutionResult

    assert gold and all(isinstance(result, ExecutionResult) for result in gold.values())


def test_execute_sql_still_offers_no_way_to_skip_validation():
    """The runner cannot bypass Gate 2 because there is no bypass to reach for.
    Restated here because this is the module `000-project.md` §4 singles out."""
    import inspect

    from api.db.execution import execute_sql

    assert list(inspect.signature(execute_sql).parameters) == ["sql"]


# --- AC12: the fingerprint gate ---------------------------------------------


def test_ac12_a_drifted_fingerprint_aborts_before_any_model_call(dataset, configured_database):
    """Before, not after. A run that cannot produce a comparable number should
    not spend forty requests finding that out."""
    from evals.dataset import DatasetError

    provider = ScriptedProvider(dataset)
    drifted = dataclasses.replace(dataset, fingerprint={"track": 1})

    with pytest.raises(DatasetError, match="Fingerprint mismatch"):
        run_evaluation(drifted, provider, repeat=1)

    assert provider.calls == 0, "the model was called despite an aborted run"


def test_run_evaluation_returns_one_report_per_pass(dataset, configured_database):
    reports = run_evaluation(dataset, ScriptedProvider(dataset), repeat=2)
    assert len(reports) == 2
    assert all(r.accuracy == 1.0 for r in reports)


# --- broken gold ------------------------------------------------------------


def test_a_broken_gold_query_is_not_charged_to_the_model(dataset, gold):
    """A defect of ours must not enter Iteration 5's backlog as a model failure.
    The dataset tests make this unreachable in a healthy repo; it is handled
    because the alternative is a crash mid-run."""
    from api.db.execution import ExecutionResult

    broken = dict(gold)
    broken["easy-001"] = ExecutionResult(
        ok=False, category="database_error", error="relation does not exist"
    )

    report = run_pass(dataset, ScriptedProvider(dataset), broken)
    case = next(c for c in report.cases if c.id == "easy-001")

    assert case.category == CATEGORY_BROKEN_GOLD
    assert case.correct is False
    assert CATEGORY_WRONG_RESULT not in report.breakdown


def test_a_broken_gold_does_not_consume_a_model_call(dataset, gold):
    from api.db.execution import ExecutionResult

    broken = dict(gold)
    broken["easy-001"] = ExecutionResult(ok=False, category="database_error", error="x")

    provider = ScriptedProvider(dataset)
    run_pass(dataset, provider, broken)
    assert provider.calls == len(dataset.questions) - 1


# --- AC24: the prompt fingerprint -------------------------------------------


def test_ac24_the_fingerprint_is_derived_from_the_template():
    """Derived, not declared. A constant someone must remember to bump fails in
    exactly the situation that matters — the prompt changed and the constant did
    not."""
    from api.agent.prompts import SYSTEM_TEMPLATE

    assert prompt_fingerprint() == prompt_fingerprint(SYSTEM_TEMPLATE)
    assert prompt_fingerprint("different template") != prompt_fingerprint()
    assert len(prompt_fingerprint()) == 12


def test_a_whitespace_change_to_the_prompt_changes_the_fingerprint():
    """Sensitive on purpose. Whitespace in a prompt is not cosmetic — it changes
    tokenisation, and therefore possibly the number."""
    from api.agent.prompts import SYSTEM_TEMPLATE

    assert prompt_fingerprint(SYSTEM_TEMPLATE + "\n") != prompt_fingerprint()


def test_describe_model_reads_the_provider(dataset):
    provider = ScriptedProvider(dataset, model="openai/gpt-oss-120b")
    assert describe_model(provider) == "openai/gpt-oss-120b"


def test_describe_model_says_unknown_rather_than_guessing():
    """A provider that declines to identify itself is recorded as `unknown` —
    honest and visible in EVALS.md, rather than silently wrong."""

    class Anonymous:
        def complete(self, system, user):
            return ""

    assert describe_model(Anonymous()) == "unknown"


def test_the_real_provider_reports_its_model():
    """`GroqProvider.model` exists so the runner never has to read `_model`
    through the class's back door."""
    from api.llm.groq_provider import GroqProvider

    assert GroqProvider(api_key="gsk_fake", model="some-model").model == "some-model"


# --- AC23, AC25: the record -------------------------------------------------


def test_ac23_the_block_records_everything_the_number_depends_on(dataset, gold):
    report = run_pass(dataset, ScriptedProvider(dataset), gold)
    block = format_report([report], dataset)

    assert "fake-model" in block
    assert report.prompt_fingerprint in block
    assert "Temperature" in block
    assert f"v{dataset.version}" in block
    assert "Execution accuracy" in block
    assert "Gate 2 pass rate" in block
    assert "Authorship" in block, "Q-D — the number carries its own caveat"


def test_the_block_reports_per_tier_and_the_breakdown(dataset, gold):
    provider = ScriptedProvider(dataset, {"easy-001": "SELECT count(*) FROM artist"})
    block = format_report([run_pass(dataset, provider, gold)], dataset)

    assert "| easy (" in block and "| medium (" in block and "| hard (" in block
    assert "wrong_result" in block
    assert "easy-001" in block, "AC18 — the failing case is in the record"
    assert "SELECT count(*) FROM artist" in block


def test_the_block_reports_the_spread_only_when_repeated(dataset, gold):
    reports = [run_pass(dataset, ScriptedProvider(dataset), gold)]
    assert "spread" not in format_report(reports, dataset)
    assert "spread" in format_report(reports * 3, dataset)


def test_ac25_recording_appends_and_never_rewrites(tmp_path):
    """Bad numbers stay. A regression quietly deleted destroys the value of the
    entire record — and the record is the only reason any later accuracy claim
    means anything."""
    path = tmp_path / "EVALS.md"

    append_to_evals("## first run\n\naccuracy 10%\n", path)
    append_to_evals("## second run\n\naccuracy 5%\n", path)

    content = path.read_text(encoding="utf-8")
    assert "first run" in content, "the worse earlier number must survive"
    assert "second run" in content
    assert content.index("first run") < content.index("second run")


def test_ac25_the_first_entry_gets_no_leading_separator(tmp_path):
    path = tmp_path / "EVALS.md"
    append_to_evals("## first\n", path)
    assert not path.read_text(encoding="utf-8").startswith("\n---")


def test_recording_into_an_empty_file_does_not_add_a_separator(tmp_path):
    path = tmp_path / "EVALS.md"
    path.write_text("", encoding="utf-8")
    append_to_evals("## first\n", path)
    assert not path.read_text(encoding="utf-8").startswith("\n---")


def test_nothing_in_the_runner_reads_evals_md():
    """There is no code path that can drop an earlier entry, because nothing
    opens the file for anything but appending."""
    source = pathlib.Path("evals/run_evals.py").read_text(encoding="utf-8")
    assert 'path.open("a"' in source
    assert 'write_text' not in source


# --- D-1: the CLI -----------------------------------------------------------


def test_d1_recording_is_off_by_default():
    """Resolved D-1. The value of EVALS.md is that every entry is a real
    measurement; a development run appending to it destroys exactly that."""
    from evals.run_evals import build_parser

    assert build_parser().parse_args([]).record is False
    assert build_parser().parse_args(["--record"]).record is True


def test_the_default_is_a_single_pass():
    from evals.run_evals import build_parser

    assert build_parser().parse_args([]).repeat == 1
    assert build_parser().parse_args(["--repeat", "3"]).repeat == 3


def test_a_nonsense_repeat_is_refused():
    from evals.run_evals import main

    assert main(["--repeat", "0"]) == 2


def test_a_missing_dataset_is_reported_rather_than_traced(tmp_path):
    from evals.run_evals import main

    assert main(["--dataset", str(tmp_path / "nope.yaml")]) == 2


# --- Q-E / Q-A: strategy and schema flags (Iteration 4) --------------------


def test_qe_the_default_strategy_is_the_loop():
    """Resolved Q-E. The loop is what ships from Iteration 4 on, so it is what
    an unqualified run measures."""
    from evals.run_evals import STRATEGY_LOOP, build_parser

    assert build_parser().parse_args([]).strategy == STRATEGY_LOOP


def test_qe_single_shot_is_still_selectable():
    """The Iteration 3 baseline has to stay recomputable rather than becoming a
    number nobody can reproduce."""
    from evals.run_evals import STRATEGY_SINGLE_SHOT, build_parser

    args = build_parser().parse_args(["--strategy", "single-shot"])
    assert args.strategy == STRATEGY_SINGLE_SHOT


def test_qa_the_default_schema_mode_is_full():
    from api.agent.prompts import SCHEMA_FULL
    from evals.run_evals import build_parser

    assert build_parser().parse_args([]).schema_mode == SCHEMA_FULL


def test_d1_the_single_shot_fingerprint_is_unchanged_from_iteration_3():
    """**The reason `fingerprint_for` special-cases single-shot.**

    The entries already in `EVALS.md` record `f971d8787f0c`. If Iteration 4 had
    changed how that strategy is fingerprinted, every one of those numbers would
    read as having been taken under a different prompt, and the history this
    project is built on would stop being comparable.
    """
    from evals.run_evals import STRATEGY_SINGLE_SHOT, fingerprint_for, prompt_fingerprint

    assert fingerprint_for(STRATEGY_SINGLE_SHOT) == prompt_fingerprint()


def test_d1_the_loop_fingerprint_covers_the_feedback_format():
    """Resolved D-1. The loop's behaviour depends on how failures are fed back
    as much as on the instructions, so a template-only hash would let a change
    to `render_transcript` produce a different number under an identical
    fingerprint — exactly the silent mismatch AC24 exists to prevent."""
    import inspect

    from api.agent.prompts import LOOP_SYSTEM_TEMPLATE, render_transcript
    from evals.run_evals import STRATEGY_LOOP, fingerprint_for, prompt_fingerprint

    assert fingerprint_for(STRATEGY_LOOP) == prompt_fingerprint(
        LOOP_SYSTEM_TEMPLATE + inspect.getsource(render_transcript)
    )


def test_d1_the_two_strategies_do_not_share_a_fingerprint():
    from evals.run_evals import STRATEGY_LOOP, STRATEGY_SINGLE_SHOT, fingerprint_for

    assert fingerprint_for(STRATEGY_LOOP) != fingerprint_for(STRATEGY_SINGLE_SHOT)


def test_single_shot_refuses_to_run_with_the_schema_withheld():
    """`answer_question` renders the schema unconditionally and offers no
    injection point, and AC1 keeps it untouched. Refusing loudly beats silently
    running it with the schema present and filing the result as blind."""
    from api.agent.prompts import SCHEMA_WITHHELD
    from evals.run_evals import STRATEGY_SINGLE_SHOT, build_strategy

    with pytest.raises(ValueError, match="cannot run with the schema withheld"):
        build_strategy(STRATEGY_SINGLE_SHOT, SCHEMA_WITHHELD)


def test_the_cli_reports_that_combination_rather_than_tracing():
    from evals.run_evals import main

    assert main(["--strategy", "single-shot", "--schema", "withheld"]) == 2


def test_the_one_call_control_is_expressible():
    """The blind baseline: same prompt, same protocol, budget as the only
    variable."""
    from evals.run_evals import build_parser

    args = build_parser().parse_args(
        ["--strategy", "loop", "--schema", "withheld", "--max-calls", "1"]
    )
    assert (args.strategy, args.schema_mode, args.max_calls) == ("loop", "withheld", 1)


def test_the_record_states_the_strategy_and_schema(dataset, gold):
    """A number is only comparable to another taken the same way, so both go in
    the record alongside the model and the prompt."""
    from api.agent.prompts import SCHEMA_WITHHELD
    from evals.run_evals import STRATEGY_LOOP

    report = run_pass(dataset, ScriptedProvider(dataset), gold)
    block = format_report(
        [report], dataset, strategy=STRATEGY_LOOP, schema_mode=SCHEMA_WITHHELD, max_calls=1
    )

    assert "| Strategy | `loop` |" in block
    assert "| Schema | `withheld` |" in block
    assert "| Provider-call budget | 1 |" in block
    assert "loop, schema withheld" in block


def test_the_record_defaults_to_the_full_loop_budget(dataset, gold):
    from evals.run_evals import STRATEGY_LOOP

    report = run_pass(dataset, ScriptedProvider(dataset), gold)
    block = format_report([report], dataset, strategy=STRATEGY_LOOP)

    from api.agent.orchestrator import MAX_PROVIDER_CALLS

    assert f"| Provider-call budget | {MAX_PROVIDER_CALLS} |" in block


def test_the_loop_strategy_scores_through_the_same_code_path(dataset, gold):
    """`AgentResult` and `AnswerResult` share field names precisely so the
    runner needs one scoring path, not two that could disagree."""
    from evals.run_evals import STRATEGY_LOOP

    report = run_pass(dataset, LoopProvider(dataset), gold, strategy=STRATEGY_LOOP)
    assert report.accuracy == 1.0
    assert report.correct == len(dataset.questions)


def test_the_loop_strategy_records_a_wrong_result(dataset, gold):
    from evals.run_evals import STRATEGY_LOOP

    provider = LoopProvider(dataset, {"easy-001": "SELECT count(*) FROM artist"})
    report = run_pass(dataset, provider, gold, strategy=STRATEGY_LOOP)

    case = next(c for c in report.cases if c.id == "easy-001")
    assert case.category == CATEGORY_WRONG_RESULT
    assert case.executed is True


def test_no_flag_can_filter_the_dataset():
    """AC22 — same dataset, same order, no sampling. A `--only` flag would be
    convenient for debugging and would also make it possible to record a partial
    run as though it were a whole one."""
    from evals.run_evals import build_parser

    flags = {action.dest for action in build_parser()._actions}
    assert not flags & {"only", "limit", "sample", "shuffle", "tier"}
