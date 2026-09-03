"""Dataset loader tests — `specs/006-evals.md` AC1-AC6.

**Pure.** Everything here goes through `parse_dataset`, so no temporary files
and no database. The corpus itself — gold queries, coverage, the fingerprint —
is tested in `test_eval_questions.py`, which needs both.

Every rule below exists because a dataset error is silent. Code that is wrong
raises; a dataset that is wrong just changes what the number means, and nothing
downstream can tell.
"""

from __future__ import annotations

import pytest
import yaml

from evals.dataset import (
    MAX_QUESTIONS,
    MIN_QUESTIONS,
    TIERS,
    DatasetError,
    count_query,
    parse_dataset,
)


def question(index: int, **overrides) -> dict:
    base = {
        "id": f"easy-{index:03d}",
        "tier": "easy",
        "question": "How many tracks are there?",
        "gold_sql": "SELECT count(*) FROM track",
        "ordered": False,
        "covers": ["track"],
        # Required since Iteration 5 T4. `dev` here is an arbitrary valid value
        # for fixtures that are testing something else; the real corpus's
        # membership is pinned in `tests/test_splits.py`.
        "split": "dev",
    }
    base.update(overrides)
    return base


def document(questions=None, **overrides) -> str:
    """A valid dataset, as YAML text, ready to be broken one field at a time."""
    body = {
        "version": 1,
        "dataset": "chinook",
        "fingerprint": {"track": 3503},
        "questions": questions
        if questions is not None
        else [question(i) for i in range(MIN_QUESTIONS)],
    }
    body.update(overrides)
    return yaml.safe_dump(body, sort_keys=False)


def test_a_well_formed_dataset_loads():
    dataset = parse_dataset(document())
    assert dataset.version == 1
    assert dataset.name == "chinook"
    assert dataset.fingerprint == {"track": 3503}
    assert len(dataset.questions) == MIN_QUESTIONS


def test_questions_keep_file_order():
    """AC22 — determinism. No shuffling, no sampling, anywhere in the pipeline,
    and it starts here."""
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    loaded = parse_dataset(document(questions))
    assert [q.id for q in loaded.questions] == [q["id"] for q in questions]


def test_fields_survive_the_round_trip():
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[0] = question(
        0,
        tier="hard",
        question="Which artist sold most?",
        gold_sql="SELECT 1",
        ordered=True,
        covers=["artist", "album"],
    )
    first = parse_dataset(document(questions)).questions[0]
    assert first.tier == "hard"
    assert first.ordered is True
    assert first.covers == ("artist", "album")


def test_by_tier_selects_one_tier():
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[0] = question(0, tier="hard")
    dataset = parse_dataset(document(questions))
    assert len(dataset.by_tier("hard")) == 1
    assert len(dataset.by_tier("easy")) == MIN_QUESTIONS - 1


# --- AC6: loading fails loudly ---------------------------------------------


def test_ac6_missing_field_is_rejected():
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    del questions[3]["gold_sql"]
    with pytest.raises(DatasetError, match="gold_sql"):
        parse_dataset(document(questions))


def test_ac6_unknown_tier_is_rejected():
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[0]["tier"] = "trivial"
    with pytest.raises(DatasetError, match="unknown tier"):
        parse_dataset(document(questions))


def test_ac6_duplicate_id_is_rejected():
    """Ids are the join key between a run and every earlier one (AC2). Two
    questions sharing an id makes `EVALS.md` history incoherent, and the
    duplicate would otherwise just be scored twice."""
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[5]["id"] = questions[2]["id"]
    with pytest.raises(DatasetError, match="duplicate id"):
        parse_dataset(document(questions))


def test_ac6_duplicate_message_names_both_positions():
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[5]["id"] = questions[2]["id"]
    with pytest.raises(DatasetError) as caught:
        parse_dataset(document(questions))
    assert "3 and 6" in str(caught.value), "the message must say where to look"


def test_ac6_unknown_key_is_rejected():
    """**The rule this loader exists for.**

    `orderd: true` would otherwise load cleanly, leave `ordered` at its YAML
    default of absent-and-therefore-False, and score every top-N question
    order-insensitively for the rest of the project. Nothing downstream could
    detect it — the run would just be quietly more generous than it claims.
    """
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[0]["orderd"] = True
    with pytest.raises(DatasetError, match="unknown keys"):
        parse_dataset(document(questions))


def test_ac6_a_quoted_boolean_is_rejected():
    """YAML turns bare `true` into a bool and `"true"` into a string, and a
    non-empty string is truthy. A quoted value would silently make an unordered
    question order-sensitive."""
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[0]["ordered"] = "false"
    with pytest.raises(DatasetError, match="non-boolean"):
        parse_dataset(document(questions))
    assert bool("false") is True, "the truthiness trap this guards"


def test_ac6_empty_covers_is_rejected():
    """`covers` is what AC5's coverage assertion is computed from. An empty one
    would silently shrink the denominator of the thing meant to catch gaps."""
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[0]["covers"] = []
    with pytest.raises(DatasetError, match="covers"):
        parse_dataset(document(questions))


def test_ac6_empty_gold_sql_is_rejected():
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[0]["gold_sql"] = "   "
    with pytest.raises(DatasetError, match="empty gold_sql"):
        parse_dataset(document(questions))


def test_ac6_empty_question_text_is_rejected():
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[0]["question"] = ""
    with pytest.raises(DatasetError, match="empty question"):
        parse_dataset(document(questions))


def test_ac6_missing_id_is_rejected():
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    del questions[0]["id"]
    with pytest.raises(DatasetError, match="no usable id"):
        parse_dataset(document(questions))


def test_ac6_invalid_yaml_is_rejected():
    with pytest.raises(DatasetError, match="not valid YAML"):
        parse_dataset("questions: [unclosed")


def test_ac6_top_level_must_be_a_mapping():
    with pytest.raises(DatasetError, match="mapping at the top level"):
        parse_dataset("- just\n- a list\n")


@pytest.mark.parametrize("key", ["version", "dataset", "fingerprint", "questions"])
def test_ac6_missing_top_level_key_is_rejected(key):
    body = yaml.safe_load(document())
    del body[key]
    with pytest.raises(DatasetError, match=key):
        parse_dataset(yaml.safe_dump(body))


def test_ac6_empty_fingerprint_is_rejected():
    """AC12 cannot assert anything against an empty fingerprint, and a check
    that silently verifies nothing is worse than no check."""
    with pytest.raises(DatasetError, match="empty fingerprint"):
        parse_dataset(document(fingerprint={}))


def test_ac6_non_integer_fingerprint_count_is_rejected():
    with pytest.raises(DatasetError, match="not an integer row count"):
        parse_dataset(document(fingerprint={"track": "3503"}))


# --- AC1: the dataset is the right size ------------------------------------


def test_ac1_too_few_questions_is_rejected():
    """A dataset that lost half its questions to a bad merge must not produce a
    confident-looking accuracy over what remains."""
    questions = [question(i) for i in range(MIN_QUESTIONS - 1)]
    with pytest.raises(DatasetError, match=f"{MIN_QUESTIONS}-{MAX_QUESTIONS}"):
        parse_dataset(document(questions))


def test_ac1_too_many_questions_is_rejected():
    questions = [question(i) for i in range(MAX_QUESTIONS + 1)]
    with pytest.raises(DatasetError, match=f"{MIN_QUESTIONS}-{MAX_QUESTIONS}"):
        parse_dataset(document(questions))


def test_ac1_tiers_are_fixed():
    """Per-tier accuracy is only comparable across runs if the tiers are."""
    assert TIERS == ("easy", "medium", "hard", "expert")


# --- D-2: `expect` is an easy-tier-only sanity check -----------------------


def test_d2_expect_is_accepted_on_the_easy_tier():
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[0]["expect"] = {"rows": 1, "value": 3503}
    first = parse_dataset(document(questions)).questions[0]
    assert first.expect_rows == 1
    assert first.expect_value == 3503
    assert first.has_expect_value is True


def test_d2_expect_is_rejected_on_medium_and_hard():
    """Resolved D-2. On a joined or aggregated question the answer is not
    knowable by inspection, so an `expect` there could only be copied from a run
    of the gold query — which verifies nothing, and is how the literal row
    snapshots Q-A rejected would creep back in one question at a time."""
    for tier in ("medium", "hard"):
        questions = [question(i) for i in range(MIN_QUESTIONS)]
        questions[0] = question(0, tier=tier, expect={"rows": 1})
        with pytest.raises(DatasetError, match="`easy` only"):
            parse_dataset(document(questions))


def test_d2_unknown_expect_key_is_rejected():
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[0]["expect"] = {"row_count": 1}
    with pytest.raises(DatasetError, match="unknown `expect` keys"):
        parse_dataset(document(questions))


def test_d2_empty_expect_is_rejected():
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[0]["expect"] = {}
    with pytest.raises(DatasetError, match="empty `expect`"):
        parse_dataset(document(questions))


def test_d2_expect_rows_must_be_an_integer():
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[0]["expect"] = {"rows": True}
    with pytest.raises(DatasetError, match="non-integer `expect.rows`"):
        parse_dataset(document(questions))


def test_d2_a_null_expect_value_is_distinguishable_from_an_absent_one():
    """`expect: {value: null}` is a real assertion — the gold must return NULL —
    and must not be confused with no assertion at all. That is what
    `has_expect_value` is for; a bare `is None` check could not tell them
    apart."""
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[0]["expect"] = {"value": None}
    first = parse_dataset(document(questions)).questions[0]
    assert first.has_expect_value is True
    assert first.expect_value is None
    assert parse_dataset(document()).questions[0].has_expect_value is False


# --- AC2: retirement is enforced, not merely stated -------------------------


def test_a_retired_id_cannot_come_back():
    """**The rule AC2 states and this makes enforceable.**

    A retired id has already been scored and published in `EVALS.md` under one
    meaning. Letting it return under another would silently corrupt every
    comparison against those entries — and silently is the operative word: the
    run would succeed, the number would look fine, and only someone rereading
    two entries side by side would ever notice.

    A comment in the YAML would have been a convention. Conventions do not
    survive a year of edits.
    """
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    with pytest.raises(DatasetError, match="reuses retired id"):
        parse_dataset(document(questions, retired={questions[0]["id"]: "was wrong"}))


def test_the_offending_id_is_named(monkeypatch):
    questions = [question(i) for i in range(MIN_QUESTIONS)]
    with pytest.raises(DatasetError) as caught:
        parse_dataset(document(questions, retired={"easy-003": "was wrong"}))
    assert "easy-003" in str(caught.value)
    assert "new id" in str(caught.value), "the message must say what to do"


def test_retiring_an_id_that_is_gone_is_fine():
    dataset = parse_dataset(document(retired={"medium-999": "never existed here"}))
    assert dataset.retired == {"medium-999": "never existed here"}


def test_retired_is_optional():
    assert parse_dataset(document()).retired == {}


def test_a_retirement_must_carry_a_reason():
    """The reason is the useful part. A retirement with no reason is a deletion
    with extra steps, and the record of how the benchmark was wrong is exactly
    what gets lost otherwise."""
    with pytest.raises(DatasetError, match="no reason"):
        parse_dataset(document(retired={"medium-008": "  "}))


def test_retired_questions_are_not_scored():
    """They are metadata, not cases. A retired question that still ran would be
    scored against a gold nobody maintains."""
    dataset = parse_dataset(document(retired={"medium-999": "gone"}))
    assert "medium-999" not in {q.id for q in dataset.questions}


def test_an_unknown_top_level_key_is_rejected():
    """Same reasoning as unknown question keys: a misspelled `retierd:` would
    load cleanly and protect nothing at all."""
    with pytest.raises(DatasetError, match="unknown top-level keys"):
        parse_dataset(document(retierd={"medium-008": "typo"}))


# --- the fingerprint query --------------------------------------------------


def test_count_query_is_built_as_an_ast():
    """Rendered from a sqlglot AST, never formatted into a string — the same
    discipline `api/db/sampling.py` applies. A project with one safe way to build
    a query and one convenient way will eventually use the convenient one."""
    assert count_query("track") == 'SELECT COUNT(*) FROM "track"'


def test_count_query_contains_a_hostile_identifier():
    """The relation names come from a committed file, not from a model. This is
    defence in depth: the AST quotes and escapes rather than concatenating, so
    the identifier stays an identifier whatever it holds."""
    rendered = count_query('track" ; DROP TABLE track --')
    assert rendered.startswith('SELECT COUNT(*) FROM "track""')
    assert rendered.count('"') % 2 == 0


def test_count_query_passes_gate_2():
    """It is generated SQL like any other, and it goes through the validator on
    its way to the database (AC20)."""
    from api.safety.validator import validate_sql

    ok, reason = validate_sql(count_query("track"))
    assert ok, reason
