"""Result comparison and metric aggregation — pure, no database, no model.

Implements the matching half of `specs/006-evals.md` (AC7-AC10, AC13-AC17).
Nothing in this module performs I/O, so every rule it encodes is testable
without a container and without an API key.

The three normalisation rules below are not refinements. Each was measured
against the live database in `006-evals.md` §2, and without them the suite would
score *correct* answers as wrong -- which would make the baseline meaningless in
the pessimistic direction, and every later delta a comparison against a number
that was never true.

Deviation from the `006-evals.md` §5 contract worth naming: `CaseResult` and
`EvalReport` are defined here rather than in `run_evals.py`, because aggregation
needs them and the runner needs aggregation. `run_evals.py` re-exports both, so
the contract's import path still resolves.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

#: Six decimal places (resolved Q-C).
#:
#: Enough to absorb the float representation a free cast introduces --
#: `SELECT sum(total)::float` returns 2328.6 where the NUMERIC column returns
#: Decimal('2328.60') -- and tight enough that a genuinely wrong aggregate still
#: fails. A tolerance loose enough to make wrong answers pass is a knob that can
#: be turned until the number looks good, which `006-evals.md` §4 rules out.
SIX_PLACES = Decimal("0.000001")

# --- Failure categories the scorer assigns ---------------------------------

#: Everything worked mechanically and the rows were still wrong (AC16).
#:
#: The only category here that means the model *reasoned* incorrectly. Kept
#: distinct from `database_error` because the fixes differ completely: a wrong
#: result is a prompting or reasoning problem, a database error usually means
#: the model did not know a column exists.
CATEGORY_WRONG_RESULT = "wrong_result"

#: The reference query itself failed to run. Not a score against the model at
#: all -- it is a broken question, and the run says so rather than quietly
#: counting it as a model failure.
CATEGORY_BROKEN_GOLD = "broken_gold"


@dataclass(frozen=True)
class _Bool:
    """A boolean, wrapped so it cannot compare equal to a number.

    Keeping the `isinstance(value, bool)` check above the numeric one is
    necessary but **not sufficient**, which is only obvious once measured::

        Decimal("1.000000") == True                    -> True
        hash(Decimal("1.000000")) == hash(True)         -> True

    So returning the bool untouched still lets `True` match the integer 1
    through the *other* side of the comparison, and because the hashes agree,
    `Counter` merges them into one bucket as well. Wrapping is what actually
    separates the domains: this class is equal only to another `_Bool`, and
    frozen so it stays hashable.

    Chinook has no boolean column, so this costs nothing today. `EXISTS` and
    `CASE` produce booleans, and a hard-tier question is where it would first
    have mattered.
    """

    value: bool


def normalise_value(value):
    """Make one cell comparable across the type choices SQL leaves free.

    Order of the checks is load-bearing:

    **`bool` before `int`, and wrapped.** `isinstance(True, int)` is `True` in
    Python, so a boolean reaching the numeric branch would become
    `Decimal('1.000000')`. Ordering the checks alone does not fix it, because
    `Decimal('1.000000') == True` is also `True` — see `_Bool`.

    **`Decimal(str(value))`, never `Decimal(value)`.** Constructed from a float
    directly, `Decimal(2328.6)` is `2328.5999999999999090505298227071762084...`
    -- the binary representation, exactly. Quantising that to six places is a
    coin flip at the boundary. Going through `str` takes the repr Python already
    rounds sensibly.

    Lists become tuples so a Postgres array stays hashable; `Counter` needs that
    for the unordered comparison below. Everything else -- `None`, `str`,
    `date`, `datetime` -- passes through untouched.

    **Dates are deliberately not coerced.** `date(2009, 1, 1)` does not equal
    `datetime(2009, 1, 1)` in Python, so a model that casts a DATE to TIMESTAMP
    scores wrong on that question. Making them equal would be a lenience §2 did
    not measure and the spec did not authorise; like column-order strictness it
    biases the baseline down, which is the safe direction.
    """
    if isinstance(value, bool):
        return _Bool(value)
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)).quantize(SIX_PLACES, rounding=ROUND_HALF_UP)
    if isinstance(value, list):
        return tuple(normalise_value(item) for item in value)
    return value


def normalise_rows(rows) -> tuple[tuple, ...]:
    """Normalise every cell of every row, preserving both orders.

    Column names are not an input here at all -- they are dropped by the caller
    before this point (AC8). Measured in §2: `count(*)` yields a column named
    `count` and `count(t.track_id) AS n` yields `n`, for identical rows.
    Comparing names would fail correct answers on aliasing alone.
    """
    return tuple(tuple(normalise_value(cell) for cell in row) for row in rows)


def results_match(gold_rows, candidate_rows, *, ordered: bool) -> bool:
    """Do two result sets carry the same answer (AC7-AC10)?

    Args:
        gold_rows: rows from the reference query.
        candidate_rows: rows from the generated query.
        ordered: whether row order is part of the answer (AC4, AC10). `ORDER BY
            genre_id LIMIT 3` and `ORDER BY name LIMIT 3` return genuinely
            different rows, so a "top N" question cannot be scored
            order-insensitively -- but "list every genre" can. That is a
            per-question property, which is why it is a required keyword
            argument rather than something with a default.

    **Column order is significant** (resolved Q-B): rows are compared as
    positional tuples. The rejected alternative, comparing sets of values within
    a row, would make `(5, 5)` match `(5,)` -- and losing duplicate detection is
    a worse error than being slightly strict about presentation.

    Unordered comparison uses `Counter`, not `sorted`. Rows mix `None`, `str`
    and `Decimal`, and Python 3 raises `TypeError` rather than ordering those
    against each other; a sort-based implementation would crash on the first
    question with a nullable text column. Multiset comparison also preserves
    duplicates, so `[(1,), (1,)]` does not match `[(1,)]`.
    """
    gold = normalise_rows(gold_rows)
    candidate = normalise_rows(candidate_rows)

    if ordered:
        return gold == candidate
    return Counter(gold) == Counter(candidate)


@dataclass(frozen=True)
class CaseResult:
    """One question's outcome, and the raw material for Iteration 5's backlog.

    Frozen, so a report cannot be edited after the fact and so equality is
    structural -- the property AC22 (determinism) is asserted with.

    `passed_validation` and `executed` are recorded by the runner at the moment
    each is known, rather than reverse-engineered from `category` afterwards.
    Deriving them from the category string would need this module to know that
    `timeout` implies validation passed while `no_sql_returned` implies it was
    never reached -- a mapping that would silently rot the first time a category
    is added in `api/db/execution.py`.

    `generated_sql` is kept even when the case failed (AC18). A failure you
    cannot see the SQL for is one nobody can fix.
    """

    id: str
    tier: str
    question: str
    generated_sql: str = ""
    correct: bool = False
    passed_validation: bool = False
    executed: bool = False
    category: str = ""
    error: str = ""


@dataclass(frozen=True)
class EvalReport:
    """The aggregate of one full pass over the dataset."""

    accuracy: float
    gate2_pass_rate: float
    execution_rate: float
    per_tier: dict[str, float]
    breakdown: dict[str, int]
    cases: tuple[CaseResult, ...] = ()
    model: str = ""
    prompt_fingerprint: str = ""
    temperature: float = 0.0
    total: int = 0
    correct: int = 0

    @property
    def failures(self) -> tuple[CaseResult, ...]:
        """Every case that did not score correct, in dataset order (AC18)."""
        return tuple(case for case in self.cases if not case.correct)


def _rate(numerator: int, denominator: int) -> float:
    """A proportion, with an empty dataset scoring 0.0 rather than dividing.

    An empty run is a broken run and `dataset.py` refuses one -- but a scorer
    that raised `ZeroDivisionError` on the way to reporting that would replace a
    clear message with a traceback.
    """
    return (numerator / denominator) if denominator else 0.0


def aggregate(
    cases,
    *,
    model: str = "",
    prompt_fingerprint: str = "",
    temperature: float = 0.0,
) -> EvalReport:
    """Turn per-case results into the metrics of AC13-AC17.

    All three headline rates share one denominator -- the total number of
    questions -- which makes them directly comparable and gives the report an
    invariant worth having::

        accuracy <= execution_rate <= gate2_pass_rate

    Any run violating it has a bug in the runner, and a test asserts it.
    Per-metric denominators would have broken that: a "gate 2 pass rate" counted
    only over questions that produced SQL would *rise* when the model started
    refusing more often, which is precisely backwards.
    """
    cases = tuple(cases)
    total = len(cases)

    correct = sum(1 for c in cases if c.correct)
    validated = sum(1 for c in cases if c.passed_validation)
    executed = sum(1 for c in cases if c.executed)

    # Only failures are counted (AC16): the breakdown is a backlog, and a
    # "correct" row in it would just be the accuracy figure written twice.
    breakdown = Counter(c.category for c in cases if not c.correct and c.category)

    per_tier: dict[str, float] = {}
    for tier in sorted({c.tier for c in cases}):
        in_tier = [c for c in cases if c.tier == tier]
        per_tier[tier] = _rate(sum(1 for c in in_tier if c.correct), len(in_tier))

    return EvalReport(
        accuracy=_rate(correct, total),
        gate2_pass_rate=_rate(validated, total),
        execution_rate=_rate(executed, total),
        per_tier=per_tier,
        # Sorted by count descending, then name, so the largest bucket -- the
        # thing to fix first -- is the first line read.
        breakdown=dict(sorted(breakdown.items(), key=lambda kv: (-kv[1], kv[0]))),
        cases=cases,
        model=model,
        prompt_fingerprint=prompt_fingerprint,
        temperature=temperature,
        total=total,
        correct=correct,
    )
