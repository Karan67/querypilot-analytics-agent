"""Scorer tests — `specs/006-evals.md` AC7-AC10, AC13-AC17.

**Pure.** No database, no model, no API key. Every measurement in `006-evals.md`
§2 becomes a case here, because those measurements are the reason the scorer has
the shape it does and a rule with no test is a rule that will be simplified away
by someone who does not know what it cost to find.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

import pytest

from evals.scoring import (
    SIX_PLACES,
    CaseResult,
    aggregate,
    normalise_rows,
    normalise_value,
    results_match,
)


def case(**kwargs) -> CaseResult:
    """A CaseResult with only the fields a test cares about."""
    defaults = {"id": "x", "tier": "easy", "question": "q"}
    return CaseResult(**{**defaults, **kwargs})


# --- AC8: column names are ignored -----------------------------------------


def test_ac8_only_rows_are_compared():
    """Measured §2: `count(*)` names its column `count`, `count(t.track_id) AS n`
    names it `n`, and the rows are identical. The scorer never receives names —
    `results_match` takes rows and nothing else, which is a stronger guarantee
    than remembering to drop them."""
    import inspect

    parameters = list(inspect.signature(results_match).parameters)
    assert parameters == ["gold_rows", "candidate_rows", "ordered"], (
        "column names must not be reachable by the comparison at all"
    )


def test_ac8_aliased_gold_still_matches():
    assert results_match([(3503,)], [(3503,)], ordered=False)


# --- AC9: numeric normalisation --------------------------------------------


def test_ac9_decimal_matches_the_float_cast_of_itself():
    """The §2 measurement, exactly: `sum(total)` gives Decimal('2328.60') and
    `sum(total)::float` gives 2328.6, and `==` says False. A cast the model chose
    freely must not decide correctness."""
    assert Decimal("2328.60") != 2328.6, "the trap this test exists for"
    assert results_match([(Decimal("2328.60"),)], [(2328.6,)], ordered=False)


def test_ac9_integer_and_decimal_forms_match():
    assert results_match([(3503,)], [(Decimal("3503.00"),)], ordered=False)


def test_ac9_decimal_is_built_from_str_not_from_the_float():
    """`Decimal(2328.6)` carries the binary representation —
    2328.5999999999999090... — and quantising that is a coin flip at the
    boundary. Going through `str` first is the whole fix.

    **The value matters.** The obvious candidate, `0.1 + 0.2`, does *not*
    discriminate: quantising to six places absorbs the difference and both
    constructions give `0.300000`. A mutation run caught that — the test passed
    against `Decimal(value)` and proved nothing.

    `4.0000005` is one of 388 values under 400 where the two genuinely disagree,
    because its decimal repr sits exactly on the six-place rounding boundary
    while its binary value sits just below it::

        Decimal(str(4.0000005)) -> 4.000001    (ROUND_HALF_UP on the .5)
        Decimal(4.0000005)      -> 4.000000    (the binary value is 4.00000049…)
    """
    assert normalise_value(4.0000005) == Decimal("4.000001")
    assert Decimal(4.0000005).quantize(SIX_PLACES, rounding=ROUND_HALF_UP) == Decimal(
        "4.000000"
    ), "the construction this test rules out"

    # And the ordinary case still behaves.
    assert normalise_value(0.1 + 0.2) == normalise_value(Decimal("0.3"))


def test_ac9_a_genuinely_wrong_aggregate_still_fails():
    """Tolerance absorbs representation, not error. Six places is chosen so a
    wrong number stays wrong."""
    assert not results_match([(Decimal("2328.60"),)], [(2328.61,)], ordered=False)


def test_ac9_difference_below_the_tolerance_is_absorbed():
    assert results_match(
        [(Decimal("1.0000001"),)], [(Decimal("1.0000002"),)], ordered=False
    )


def test_ac9_quantum_is_six_places():
    assert SIX_PLACES == Decimal("0.000001")


# --- the bool/int trap ------------------------------------------------------


def test_bool_does_not_match_the_integer_one():
    """`isinstance(True, int)` is True in Python, so a bool reaching the numeric
    branch becomes Decimal('1.000000') and compares equal to 1.

    `EXISTS` and `CASE` produce booleans, so a hard-tier question is exactly
    where this would first bite. Moving the bool check below the numeric one
    makes this test go red.
    """
    assert isinstance(True, int), "the language fact this test guards against"
    assert not results_match([(True,)], [(1,)], ordered=False)
    assert not results_match([(False,)], [(0,)], ordered=False)


def test_ordering_the_bool_check_first_is_not_enough_on_its_own():
    """The finding that made `_Bool` necessary, recorded as a test.

    Returning the bool untouched still lets it match, because the collision is
    on the *other* side of the comparison and `Counter` hashes them together::

        Decimal("1.000000") == True             -> True
        hash(Decimal("1.000000")) == hash(True)  -> True

    So the wrapper is doing the work, not the check order. Unwrap it — return
    `value` from the bool branch — and the test above goes green while the
    scorer is wrong.
    """
    from collections import Counter

    assert Decimal("1.000000") == True  # noqa: E712 — the point of the test
    assert hash(Decimal("1.000000")) == hash(True)
    assert Counter([Decimal("1.000000")]) == Counter([True])

    assert normalise_value(True) != normalise_value(1)
    assert Counter([normalise_value(True)]) != Counter([normalise_value(1)])


def test_bool_matches_itself():
    assert results_match([(True,), (False,)], [(True,), (False,)], ordered=True)


# --- NULL -------------------------------------------------------------------


def test_null_matches_null():
    assert results_match([(None,)], [(None,)], ordered=False)


def test_null_does_not_match_zero():
    """A NULL aggregate and a zero aggregate are different answers — an artist
    with no invoices versus an artist with invoices summing to nothing."""
    assert not results_match([(None,)], [(0,)], ordered=False)
    assert not results_match([(None,)], [(Decimal("0.00"),)], ordered=False)


def test_null_does_not_match_empty_string():
    assert not results_match([(None,)], [("",)], ordered=False)


# --- AC10: row order --------------------------------------------------------


def test_ac10_ordered_question_rejects_a_permuted_order():
    """A "top 3 by sales" answer that returns the right three rows in the wrong
    order has not answered the question."""
    gold = [("Rock",), ("Latin",), ("Metal",)]
    permuted = [("Latin",), ("Rock",), ("Metal",)]
    assert not results_match(gold, permuted, ordered=True)


def test_ac10_unordered_question_accepts_a_permuted_order():
    gold = [("Rock",), ("Latin",), ("Metal",)]
    permuted = [("Latin",), ("Metal",), ("Rock",)]
    assert results_match(gold, permuted, ordered=False)


def test_ac10_ordered_question_accepts_the_same_order():
    gold = [("Rock",), ("Latin",), ("Metal",)]
    assert results_match(gold, list(gold), ordered=True)


def test_ac10_ordered_flag_is_required():
    """Not a default. Whether order matters is a property of the question, and a
    default would silently apply one policy to every question that forgot to
    say."""
    with pytest.raises(TypeError):
        results_match([(1,)], [(1,)])  # type: ignore[call-arg]


# --- Q-B: column order is significant --------------------------------------


def test_qb_permuted_columns_do_not_match():
    """Measured §2: `(name, count)` and `(count, name)` carry the same
    information and are unequal as tuples. Resolved Q-B keeps them unequal.

    This under-credits correct answers, which is accepted: it biases the
    baseline down, and down is the safe direction for a number every later
    iteration is a delta from.
    """
    named_first = [("Rock", 1297), ("Latin", 579)]
    count_first = [(1297, "Rock"), (579, "Latin")]
    assert not results_match(named_first, count_first, ordered=True)
    assert not results_match(named_first, count_first, ordered=False)


def test_qb_the_rejected_set_of_values_comparison_would_have_lost_duplicates():
    """Why Q-B did not go the other way: comparing sets of values within a row
    makes `(5, 5)` match `(5,)`. Losing duplicate detection is a worse error
    than being strict about presentation."""
    assert {5, 5} == {5}, "the trap the rejected approach would have walked into"
    assert not results_match([(5, 5)], [(5,)], ordered=False)


def test_different_column_counts_never_match():
    assert not results_match([(1, 2)], [(1,)], ordered=False)
    assert not results_match([(1,)], [(1, 2)], ordered=True)


# --- unordered comparison mechanics ----------------------------------------


def test_unordered_comparison_survives_mixed_unorderable_types():
    """`sorted()` raises TypeError on rows mixing None, str and Decimal — Python
    3 refuses to order those. `Counter` does not care.

    A sort-based implementation would crash on the first question with a
    nullable text column, which in Chinook is most of them.
    """
    with pytest.raises(TypeError):
        sorted([(None,), ("a",), (Decimal("1"),)])

    rows = [(None,), ("a",), (Decimal("1"),)]
    assert results_match(rows, list(reversed(rows)), ordered=False)


def test_unordered_comparison_preserves_duplicates():
    """A multiset, not a set. `[(1,), (1,)]` is a different answer from
    `[(1,)]` — swapping `Counter` for `set` makes this go red."""
    assert not results_match([(1,), (1,)], [(1,)], ordered=False)
    assert results_match([(1,), (1,)], [(1,), (1,)], ordered=False)


def test_empty_results_match_each_other():
    assert results_match([], [], ordered=False)
    assert results_match([], [], ordered=True)


def test_empty_does_not_match_non_empty():
    assert not results_match([], [(1,)], ordered=False)


def test_rows_may_be_lists_or_tuples():
    """`execute_sql` returns tuples, but a test or a future caller may hand over
    lists. The shape of the container is not part of the answer."""
    assert results_match([[1, "a"]], [(1, "a")], ordered=True)


def test_normalise_rows_returns_tuples_all_the_way_down():
    assert normalise_rows([[1, "a"]]) == ((Decimal("1.000000"), "a"),)


def test_postgres_arrays_stay_hashable():
    """A list cell would raise `TypeError: unhashable type` inside `Counter`.
    Chinook has no array column, but converting is one line and the failure it
    prevents is a crashed run rather than a wrong score."""
    assert results_match([([1, 2],)], [((1, 2),)], ordered=False)


# --- dates are deliberately strict -----------------------------------------


def test_dates_are_not_coerced_to_datetimes():
    """A documented under-credit, not an oversight. Coercing would be a lenience
    §2 never measured; the spec's policy is that strictness biases the baseline
    down, which is the safe direction."""
    assert date(2009, 1, 1) != datetime(2009, 1, 1)
    assert not results_match(
        [(date(2009, 1, 1),)], [(datetime(2009, 1, 1),)], ordered=False
    )


def test_identical_timestamps_match():
    stamp = datetime(2009, 1, 1, 12, 30)
    assert results_match([(stamp,)], [(datetime(2009, 1, 1, 12, 30),)], ordered=True)


def test_strings_are_compared_exactly():
    """No case folding, no whitespace trimming. 'Rock' and 'rock' are different
    values in the database and must be different answers here."""
    assert not results_match([("Rock",)], [("rock",)], ordered=False)
    assert not results_match([("Rock",)], [(" Rock",)], ordered=False)


# --- AC13-AC17: aggregation -------------------------------------------------


def test_ac13_accuracy_is_correct_over_total():
    cases = [case(id=str(i), correct=i < 3) for i in range(4)]
    assert aggregate(cases).accuracy == 0.75


def test_ac14_gate2_pass_rate_counts_validated_sql():
    cases = [
        case(id="a", passed_validation=True),
        case(id="b", passed_validation=True),
        case(id="c", passed_validation=False),
        case(id="d", passed_validation=False),
    ]
    assert aggregate(cases).gate2_pass_rate == 0.5


def test_ac15_execution_rate_counts_queries_that_ran():
    cases = [
        case(id="a", passed_validation=True, executed=True),
        case(id="b", passed_validation=True, executed=False),
    ]
    assert aggregate(cases).execution_rate == 0.5


def test_all_three_rates_share_one_denominator():
    """The invariant `accuracy <= execution_rate <= gate2_pass_rate` only holds
    if they do. Per-metric denominators would make gate 2 pass rate *rise* when
    the model started refusing more often, which is backwards."""
    cases = [
        case(id="a", correct=True, passed_validation=True, executed=True),
        case(id="b", passed_validation=True, executed=True),
        case(id="c", passed_validation=True),
        case(id="d"),
    ]
    report = aggregate(cases)
    assert report.accuracy == 0.25
    assert report.execution_rate == 0.5
    assert report.gate2_pass_rate == 0.75
    assert report.accuracy <= report.execution_rate <= report.gate2_pass_rate


def test_ac16_breakdown_counts_failures_by_category():
    cases = [
        case(id="a", correct=True),
        case(id="b", category="wrong_result"),
        case(id="c", category="wrong_result"),
        case(id="d", category="rejected"),
    ]
    assert aggregate(cases).breakdown == {"wrong_result": 2, "rejected": 1}


def test_ac16_breakdown_excludes_correct_cases():
    """A correct case carries no category, but even if one leaked in it must not
    be counted — the breakdown is the backlog, not a second copy of the score."""
    cases = [case(id="a", correct=True, category="wrong_result")]
    assert aggregate(cases).breakdown == {}


def test_ac16_breakdown_is_ordered_by_size():
    """The largest bucket is the thing to fix first, so it is the first line
    read."""
    cases = (
        [case(id=f"a{i}", category="rejected") for i in range(1)]
        + [case(id=f"b{i}", category="wrong_result") for i in range(3)]
        + [case(id=f"c{i}", category="database_error") for i in range(2)]
    )
    assert list(aggregate(cases).breakdown) == [
        "wrong_result",
        "database_error",
        "rejected",
    ]


def test_ac17_per_tier_accuracy_is_reported_separately():
    """"89%" must not be able to hide 100% easy and 20% hard."""
    cases = [
        case(id="e1", tier="easy", correct=True),
        case(id="e2", tier="easy", correct=True),
        case(id="h1", tier="hard", correct=False),
        case(id="h2", tier="hard", correct=False),
    ]
    report = aggregate(cases)
    assert report.accuracy == 0.5
    assert report.per_tier == {"easy": 1.0, "hard": 0.0}


def test_ac18_failures_are_retained_with_their_sql():
    cases = [
        case(id="a", correct=True),
        case(id="b", generated_sql="SELECT bogus", category="database_error"),
    ]
    failures = aggregate(cases).failures
    assert [f.id for f in failures] == ["b"]
    assert failures[0].generated_sql == "SELECT bogus"


def test_metadata_is_carried_into_the_report():
    report = aggregate(
        [case(id="a")], model="m", prompt_fingerprint="abc123", temperature=0.0
    )
    assert (report.model, report.prompt_fingerprint, report.temperature) == (
        "m",
        "abc123",
        0.0,
    )


def test_empty_case_list_reports_zero_rather_than_dividing():
    report = aggregate([])
    assert report.accuracy == 0.0
    assert report.total == 0


def test_case_result_is_frozen():
    """A report that could be edited after the fact is not a record."""
    with pytest.raises(Exception):
        case(id="a").correct = True  # type: ignore[misc]


def test_reports_with_identical_cases_are_equal():
    """Structural equality is how AC22 (determinism) is asserted."""
    cases = [case(id="a", correct=True)]
    assert aggregate(cases) == aggregate(list(cases))
