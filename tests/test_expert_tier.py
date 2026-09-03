"""Expert tier tests — `specs/008-prompt-tuning.md` AC12, AC14-AC16 (T6).

The tier this benchmark is most able to fool itself with, so the checks are
executed rather than asserted.

**AC12 is the one that matters.** A question whose naive and conventional
readings return the same rows is a free point: it scores correct under either
interpretation, inflating the number while measuring nothing. That is the exact
failure `006`'s duplicate-row check exists to prevent, applied to meaning
instead of to rows.

Every query here goes through `execute_sql()` — `000-project.md` §4 names the
eval harness explicitly, and there is no exemption for a test that is only
reading.
"""

from __future__ import annotations

import pytest

from api.db.execution import execute_sql
from evals.dataset import TIER_EXPERT, DatasetError, load_dataset, parse_dataset
from evals.scoring import results_match

pytestmark = pytest.mark.usefixtures("configured_database")


@pytest.fixture(scope="module")
def dataset():
    return load_dataset()


@pytest.fixture(scope="module")
def expert_questions(dataset):
    questions = [q for q in dataset.questions if q.tier == TIER_EXPERT]
    assert questions, "the expert tier is missing"
    return questions


def _rows(sql: str):
    result = execute_sql(sql)
    assert result.ok, f"{result.category}: {result.error}"
    return result.rows


# --- AC12: every expert question provably discriminates ---------------------


def test_ac12_the_naive_and_conventional_readings_differ(expert_questions):
    """**The check that stops an expert question being a free point.**

    Executed, not tabulated. A table of expected numbers would go stale the
    moment the database was reseeded, and would then assert that two queries
    which now agree still disagree.
    """
    for question in expert_questions:
        gold = _rows(question.gold_sql)
        naive = _rows(question.naive_sql)

        assert not results_match(gold, naive, ordered=question.ordered), (
            f"{question.id} is a free point: its naive reading returns the same "
            f"result as the gold, so it scores correct without the glossary"
        )


def test_every_expert_question_has_a_naive_reading(expert_questions):
    for question in expert_questions:
        assert question.naive_sql.strip(), question.id


def test_naive_sql_is_never_scored():
    """It is evidence about the *question*, not about the model.

    Asserted structurally: the runner scores `gold_sql` and must not reference
    `naive_sql` at all. Against the parsed AST rather than the source text,
    because this project has twice written a grep that matched its own
    docstring.
    """
    import ast
    import pathlib

    tree = ast.parse(
        pathlib.Path("evals/run_evals.py").read_text(encoding="utf-8")
    )
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert "naive_sql" not in attributes, (
        "the runner reads naive_sql; it exists to validate questions, never to "
        "score them"
    )


# --- AC15: hard by interpretation, not by syntax ----------------------------


def test_ac15_expert_questions_are_not_merely_harder_sql(expert_questions):
    """The `hard` tier already scores 100%, so more window functions would
    measure nothing new. What distinguishes this tier is that the *naive* query
    is usually the simpler one — the difficulty is knowing it is wrong."""
    for question in expert_questions:
        assert question.glossary, (
            f"{question.id} declares no glossary term, so nothing makes it an "
            f"interpretation question rather than a syntax one"
        )


def test_ac16_every_expert_question_states_its_convention(expert_questions):
    """AC16 — ambiguity with no single right answer stays banned.

    `006` retired `medium-008` for exactly that. The glossary is what converts
    the same trap into a fair question: given the definition, one answer is
    defensible; without it, none is. So every expert question must name the
    term that resolves it.
    """
    from api.agent.glossary import GLOSSARY

    for question in expert_questions:
        for term in question.glossary:
            assert term in GLOSSARY, f"{question.id}: {term!r} is undefined"


def test_the_combined_questions_defeat_partial_credit(expert_questions):
    """**Two terms means two chances to be wrong, and both must count.**

    A model that applies one convention and ignores the other must not score
    correct. Measured at T6: `expert-009` returns 5.6631 with both terms, 5.6519
    with only the metric definition, and 1.0396 with neither; `expert-010`
    returns 132, 165 (charting only), 168 (credited only) and 275 (neither).
    Four distinct readings each, so there is no partial credit to collect.
    """
    combined = [q for q in expert_questions if len(q.glossary) > 1]
    assert len(combined) == 2, "expected two multi-term questions"

    partials = {
        "expert-009": "SELECT round(avg(total), 4) FROM invoice",
        "expert-010": (
            "SELECT count(DISTINCT al.artist_id) FROM invoice_line il "
            "JOIN track t ON t.track_id = il.track_id "
            "JOIN album al ON al.album_id = t.album_id"
        ),
    }

    for question in combined:
        gold = _rows(question.gold_sql)
        partial = _rows(partials[question.id])
        assert not results_match(gold, partial, ordered=question.ordered), (
            f"{question.id} scores correct while applying only one of its two "
            f"conventions"
        )


# --- AC14: the tier arrived without disturbing anything -------------------


def test_ac14_the_expert_tier_is_ten_new_questions(expert_questions):
    assert len(expert_questions) == 10
    assert {q.id for q in expert_questions} == {
        f"expert-{n:03d}" for n in range(1, 11)
    }


def test_ac14_the_dataset_version_was_raised(dataset):
    """`006` AC2: a corpus that changed shape is a different corpus, and every
    earlier `EVALS.md` entry describes the old one."""
    assert dataset.version == 3


def test_ac17_the_corpus_fits_the_declared_bounds(dataset):
    from evals.dataset import MAX_QUESTIONS, MIN_QUESTIONS

    assert len(dataset.questions) == 50
    assert MIN_QUESTIONS <= len(dataset.questions) <= MAX_QUESTIONS


# --- the loader enforces the tier's contract --------------------------------


def _document(**overrides):
    from evals.dataset import MIN_QUESTIONS

    from tests.test_eval_dataset import document, question

    expert = {
        "id": "expert-001",
        "tier": "expert",
        "split": "dev",
        "question": "How many active customers are there?",
        "gold_sql": "SELECT count(DISTINCT customer_id) FROM invoice",
        "naive_sql": "SELECT count(*) FROM customer",
        "glossary": ["active customer"],
        "ordered": False,
        "covers": ["invoice"],
    }
    expert.update(overrides)
    for key, value in list(expert.items()):
        if value is None:
            del expert[key]

    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions.append(expert)
    return document(questions)


def test_an_expert_question_without_naive_sql_is_rejected():
    with pytest.raises(DatasetError, match="naive_sql"):
        parse_dataset(_document(naive_sql=None))


def test_a_naive_sql_identical_to_the_gold_is_rejected():
    """The free point, refused at load time as well as by the executed check.

    Two guards because they fail differently: this one catches a copy-paste at
    authoring time with no database, and AC12's executed test catches two
    *different* queries that happen to return the same rows.
    """
    gold = "SELECT count(DISTINCT customer_id) FROM invoice"
    with pytest.raises(DatasetError, match="identical"):
        parse_dataset(_document(gold_sql=gold, naive_sql=gold))


def test_an_expert_question_without_a_glossary_term_is_rejected():
    with pytest.raises(DatasetError, match="glossary term"):
        parse_dataset(_document(glossary=None))


def test_naive_sql_is_rejected_outside_the_expert_tier():
    """Strict in both directions, like `expect` on the easy tier: a field that
    may appear anywhere gets used where it means nothing."""
    from evals.dataset import MIN_QUESTIONS

    from tests.test_eval_dataset import document, question

    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[0]["naive_sql"] = "SELECT count(*) FROM track"

    with pytest.raises(DatasetError, match="naive_sql"):
        parse_dataset(document(questions))
