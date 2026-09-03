"""Held-out split tests — `specs/008-prompt-tuning.md` Q-A, plan §3 (T4).

The split is the one piece of this iteration that must be decided *before* any
number is seen, and the only thing that makes that credible afterwards is that
moving a question between splits breaks a test.

Three guards, because one is not enough:

1. **Pinning** — the exact test-split membership is named here. Moving a
   question fails this, the way reusing a retired id fails the loader.
2. **A fingerprint** — derived from the `(id, split)` pairs and recorded in
   `EVALS.md`, so a number taken under a different partition is visibly a
   different number.
3. **`--split` defaults to `dev`** — a tuning run cannot reach the held-out
   questions by forgetting a flag.

None of the three prevents overfitting to `dev`. That is what the final
test-split number measures, and it is the reason this file exists rather than a
comment in the YAML.
"""

from __future__ import annotations

from collections import Counter

import pytest

from evals.dataset import (
    SPLIT_DEV,
    SPLIT_TEST,
    SPLITS,
    DatasetError,
    load_dataset,
    questions_in_split,
    split_fingerprint,
)

pytestmark = pytest.mark.usefixtures("configured_database")


#: The frozen held-out set, named question by question (guard 1).
#:
#: Assigned at T4 by the plan's rule — within each tier, sorted by id, 60/40 —
#: **plus one deliberate swap**: `easy-012` into dev and `easy-005` into test.
#:
#: The swap exists because `invoice_totals` is covered by exactly two questions
#: and the unmodified rule put both in test, leaving the dev split — the only
#: split tuning may look at — never exercising the view. `compact-abbrev`'s
#: known failure mode is losing NUMERIC scale, and `medium-018` is the precision
#: question the plan itself nominated as the canary for it. A canary that only
#: sings in the room nobody is allowed to enter is not a canary.
#:
#: **Extended at T6** with the expert tier, 6 dev and 4 test. That changed the
#: fingerprint once, before any tuning — which is exactly what the
#: T4-then-T6-then-T7 ordering exists to allow.
#:
#: The expert assignment carries **its own deviation**, for the same reason the
#: easy one does. The plan's rule (001-006 dev, 007-010 test) left two glossary
#: terms — `average order value` and `credited track` — exercised only in the
#: held-out set, so tuning could never detect a badly worded definition for
#: either. Swapping `expert-005`/`expert-006` with `expert-007`/`expert-008`
#: puts every term used by two questions into both splits; the four terms with
#: only one question each divide 2 dev / 2 test, which is the best available —
#: a term with a single question cannot be in both.
FROZEN_TEST_SPLIT = frozenset(
    {
        "easy-005",
        "easy-009",
        "easy-010",
        "easy-011",
        "easy-013",
        "easy-014",
        "medium-012",
        "medium-013",
        "medium-014",
        "medium-015",
        "medium-017",
        "medium-018",
        "hard-007",
        "hard-008",
        "hard-009",
        "hard-010",
        "expert-005",
        "expert-006",
        "expert-009",
        "expert-010",
    }
)

#: Per-tier 60/40, as the plan's table specifies.
EXPECTED_COUNTS = {
    "easy": (8, 6),
    "medium": (10, 6),
    "hard": (6, 4),
    "expert": (6, 4),
}


@pytest.fixture(scope="module")
def dataset():
    return load_dataset()


# --- guard 1: pinning -------------------------------------------------------


def test_the_test_split_membership_is_exactly_what_was_frozen(dataset):
    """**The guard that makes the freeze mean something.**

    Without this, "the split was fixed before tuning" is a claim about what
    somebody remembers doing. With it, moving one question is a failing test
    and a deliberate act.
    """
    actual = {q.id for q in dataset.questions if q.split == SPLIT_TEST}
    assert actual == set(FROZEN_TEST_SPLIT), {
        "moved into test": sorted(actual - FROZEN_TEST_SPLIT),
        "moved out of test": sorted(FROZEN_TEST_SPLIT - actual),
    }


def test_every_question_declares_a_split(dataset):
    for question in dataset.questions:
        assert question.split in SPLITS, question.id


def test_a_question_without_a_split_is_rejected():
    """**Required, never defaulted** — the same rule `ordered` follows.

    A default would silently assign whichever questions forgot to say, and the
    entire value of a held-out split is that its membership was decided
    deliberately before any number was seen. A question that quietly landed in
    `dev` because nobody wrote a line would undo that without a word.
    """
    from evals.dataset import MIN_QUESTIONS

    from tests.test_eval_dataset import document, question

    questions = [question(i) for i in range(MIN_QUESTIONS)]
    del questions[3]["split"]

    with pytest.raises(DatasetError, match="split"):
        from evals.dataset import parse_dataset

        parse_dataset(document(questions))


def test_a_question_with_an_unknown_split_is_rejected():
    from evals.dataset import MIN_QUESTIONS, parse_dataset

    from tests.test_eval_dataset import document, question

    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[3]["split"] = "holdout"

    with pytest.raises(DatasetError, match="unknown split"):
        parse_dataset(document(questions))


def test_the_dev_split_is_everything_else(dataset):
    dev = {q.id for q in dataset.questions if q.split == SPLIT_DEV}
    assert dev.isdisjoint(FROZEN_TEST_SPLIT)
    assert len(dev) + len(FROZEN_TEST_SPLIT) == len(dataset.questions)


# --- stratification ---------------------------------------------------------


def test_the_split_is_stratified_by_tier(dataset):
    """An unstratified test set is not a test set.

    The plan measured the alternative: hashing the id gave medium a 13/3 split,
    where one question would have been 33% of a tier. Three questions cannot
    support a per-tier number.
    """
    counts = Counter((q.tier, q.split) for q in dataset.questions)
    for tier, (want_dev, want_test) in EXPECTED_COUNTS.items():
        assert counts[(tier, SPLIT_DEV)] == want_dev, tier
        assert counts[(tier, SPLIT_TEST)] == want_test, tier


def test_the_overall_ratio_is_60_40(dataset):
    dev = sum(1 for q in dataset.questions if q.split == SPLIT_DEV)
    test = sum(1 for q in dataset.questions if q.split == SPLIT_TEST)
    assert (dev, test) == (30, 20)
    assert dev / (dev + test) == pytest.approx(0.6, abs=0.01)


def test_one_test_question_is_now_five_percent(dataset):
    """The plan's §3 resolution limit, finally true.

    It said one question is 5%, assuming 20 held-out questions across 50. Before
    T6 there were 16, so a question was 6.2%. It is worth pinning because the
    number bounds what any comparison can resolve: **a tuning change worth less
    than one question is not measurable on this split**, and `--repeat` reduces
    noise without improving granularity.
    """
    test = sum(1 for q in dataset.questions if q.split == SPLIT_TEST)
    assert test == 20
    assert 100 / test == 5.0


def test_both_splits_exercise_every_glossary_term_that_has_two_questions(dataset):
    """**Why the expert assignment deviates from the plan's rule.**

    Tuning happens on dev. A term exercised only in the held-out set is one
    whose definition tuning cannot check, which is the same blind spot the
    `invoice_totals` swap fixed for relations.

    Terms with a single question are exempt because they cannot satisfy this —
    stated as an explicit carve-out rather than left as a silent gap.
    """
    from collections import Counter

    usage = Counter(
        term for question in dataset.questions for term in question.glossary
    )
    for term, count in usage.items():
        if count < 2:
            continue
        splits = {
            question.split
            for question in dataset.questions
            if term in question.glossary
        }
        assert splits == set(SPLITS), (
            f"{term!r} has {count} questions but appears only in {sorted(splits)}"
        )


def test_both_splits_exercise_every_relation(dataset):
    """**Why the assignment deviates from the plan's rule by two questions.**

    Stratifying by tier balances difficulty and says nothing about coverage.
    Measured at T4: the unmodified rule left `invoice_totals` absent from dev
    and `media_type` absent from test. Tuning that cannot see the view cannot
    notice a rendering that mishandles it.

    Asserted for both splits rather than just dev, because the same argument
    runs the other way: a held-out set that never touches a relation cannot
    detect a regression in it either.
    """
    everything = {r for q in dataset.questions for r in q.covers}
    for split in SPLITS:
        covered = {
            relation
            for question in dataset.questions
            if question.split == split
            for relation in question.covers
        }
        assert covered == everything, {
            "split": split,
            "missing": sorted(everything - covered),
        }


# --- guard 2: the fingerprint ----------------------------------------------


def test_the_split_fingerprint_is_stable(dataset):
    assert split_fingerprint(dataset) == split_fingerprint(dataset)


def test_moving_a_question_changes_the_fingerprint(dataset):
    """Derived, not declared. A version constant somebody bumps fails in
    exactly the situation that matters."""
    import dataclasses

    from evals.dataset import Dataset

    moved = dataclasses.replace(
        dataset,
        questions=tuple(
            dataclasses.replace(q, split=SPLIT_DEV) if q.id == "hard-010" else q
            for q in dataset.questions
        ),
    )
    assert isinstance(moved, Dataset)
    assert split_fingerprint(moved) != split_fingerprint(dataset)


def test_the_fingerprint_ignores_question_wording(dataset):
    """It identifies the *partition*. `006`'s dataset fingerprint already
    identifies the data, and folding the two together would make a typo fix
    look like a re-partition."""
    import dataclasses

    reworded = dataclasses.replace(
        dataset,
        questions=tuple(
            dataclasses.replace(q, question=q.question + " (reworded)")
            for q in dataset.questions
        ),
    )
    assert split_fingerprint(reworded) == split_fingerprint(dataset)


# --- guard 3: selecting a split --------------------------------------------


def test_questions_in_split_returns_only_that_split(dataset):
    for split in SPLITS:
        selected = questions_in_split(dataset, split)
        assert selected
        assert all(q.split == split for q in selected)


def test_none_selects_the_whole_corpus(dataset):
    assert questions_in_split(dataset, None) == dataset.questions


def test_selection_preserves_dataset_order(dataset):
    """`006` AC22 — cases are reported in dataset order. Filtering must not
    quietly reorder them, or two runs of the same split become undiffable."""
    for split in SPLITS:
        selected = questions_in_split(dataset, split)
        expected = [q.id for q in dataset.questions if q.split == split]
        assert [q.id for q in selected] == expected


def test_an_unknown_split_is_refused(dataset):
    """A typo must not silently select the whole corpus and be recorded as a
    dev number."""
    with pytest.raises(DatasetError, match="unknown split"):
        questions_in_split(dataset, "devv")


def test_the_runner_defaults_to_dev():
    """A tuning run cannot reach the held-out questions by omission. Reaching
    them takes typing `--split test`."""
    from evals.run_evals import build_parser

    assert build_parser().parse_args([]).split == SPLIT_DEV


def test_all_is_selectable_so_full_corpus_numbers_stay_reproducible():
    """Earlier iterations' numbers were taken over all 40 questions. Without
    `all`, reproducing one would mean adding two runs together."""
    from evals.run_evals import build_parser

    assert build_parser().parse_args(["--split", "all"]).split == "all"


# --- D-3: the audit trail ---------------------------------------------------


def test_reveal_test_failures_is_off_by_default():
    from evals.run_evals import build_parser

    assert build_parser().parse_args([]).reveal_test_failures is False


def test_test_split_failures_are_withheld_without_the_flag(capsys):
    """Aggregate numbers always print; only the case list is gated.

    Question-level overfitting needs to know *which* held-out questions failed,
    so obtaining that is an auditable act rather than an invisible one.
    """
    from evals.run_evals import print_report

    report = _report_with_a_failure(split=SPLIT_TEST)
    print_report([report], _FakeDataset(), verbose=True, reveal_test_failures=False)

    out = capsys.readouterr().out
    assert "EXECUTION ACCURACY" in out, "the number itself is never withheld"
    assert "hard-009" not in out, "the failing id is withheld"
    assert "--reveal-test-failures" in out, "and the reader is told how to get it"


def test_the_flag_reveals_them(capsys):
    from evals.run_evals import print_report

    report = _report_with_a_failure(split=SPLIT_TEST)
    print_report([report], _FakeDataset(), verbose=True, reveal_test_failures=True)

    assert "hard-009" in capsys.readouterr().out


def test_dev_failures_are_never_withheld(capsys):
    """The gate is on the held-out split only. Tuning against dev is the
    intended activity, and hiding its failures would obstruct the work without
    protecting anything."""
    from evals.run_evals import print_report

    report = _report_with_a_failure(split=SPLIT_DEV)
    print_report([report], _FakeDataset(), verbose=True, reveal_test_failures=False)

    assert "hard-009" in capsys.readouterr().out


class _FakeDataset:
    version = 2


def _report_with_a_failure(split: str):
    from evals.scoring import CaseResult, aggregate

    cases = [
        CaseResult(
            id="hard-009",
            tier="hard",
            question="a question",
            correct=False,
            category="wrong_result",
            generated_sql="SELECT 1",
            error="mismatch",
        ),
        CaseResult(
            id="hard-007",
            tier="hard",
            question="another",
            correct=True,
            category="",
            generated_sql="SELECT 2",
            error="",
        ),
    ]
    return aggregate(cases, split=split, split_fingerprint="abc123abc123")
