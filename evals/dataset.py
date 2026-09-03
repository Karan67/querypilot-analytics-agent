"""Loading and shape validation for the eval dataset (`specs/006-evals.md` AC1-AC6, AC12).

Loading is **strict**, and deliberately so. This file is the definition of
correctness for everything the project measures from here on, and a dataset
error does not announce itself the way a code error does — it silently changes
what the number means.

The rule that earns its keep is unknown-key rejection. A question written with
`orderd: true` would otherwise load cleanly, quietly default `ordered` to
`False`, and score a top-N question order-insensitively for the rest of the
project's life. Nothing downstream could detect that. Rejecting the key at load
time is the only place it can be caught.

`yaml.safe_load` throughout: `yaml.load` can construct arbitrary Python objects,
and while this file is committed rather than untrusted, a benchmark harness is
not the place to keep a code-execution primitive alive out of convenience.
"""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass, field

import yaml
from sqlglot import exp

#: The tiers (AC1). Fixed, because per-tier accuracy (AC17) is only comparable
#: across runs if the tiers themselves are.
#:
#: `expert` arrived at Iteration 5 T6 (`008-prompt-tuning.md` AC14). It is not
#: "harder SQL" -- the `hard` tier already scores 100% and more window functions
#: would measure nothing new (AC15). It is **harder interpretation**: every
#: expert question has a naive reading that a competent analyst would produce
#: without the glossary, and a different one they would produce with it.
TIERS = ("easy", "medium", "hard", "expert")

#: The tier on which `naive_sql` is required, and the only one that permits it.
#:
#: Strict in both directions for the same reason `expect` is easy-only: a field
#: that may appear anywhere gets used somewhere it means nothing, and a field
#: that may be omitted where it matters silently stops protecting anything.
TIER_EXPERT = "expert"

#: AC1 — 30 to 50 questions. Enforced at load so a dataset that lost half its
#: questions to a bad merge fails immediately rather than producing a
#: confident-looking accuracy over 19 of them.
MIN_QUESTIONS = 30
MAX_QUESTIONS = 50

#: Dialect for the fingerprint queries. Matches `api/safety/validator.py`.
DIALECT = "postgres"

_REQUIRED_KEYS = frozenset(
    {"id", "tier", "question", "gold_sql", "ordered", "covers", "split"}
)

#: Which half of the corpus a question belongs to (`008-prompt-tuning.md` Q-A).
#:
#: `dev` is the only split tuning may look at. `test` is held out and read once,
#: at the end. Required rather than defaulted, for the same reason `ordered` is:
#: a default would silently assign whichever questions forgot to say, and the
#: whole value of a held-out split is that its membership was decided before any
#: number was seen.
SPLIT_DEV = "dev"
SPLIT_TEST = "test"
SPLITS = (SPLIT_DEV, SPLIT_TEST)

#: Optional question keys.
#:
#: `expect` is permitted only on the `easy` tier (resolved D-2). Allowing it
#: everywhere would recreate literal row snapshots — the option Q-A rejected —
#: one question at a time.
#:
#: `glossary` names the business terms a question depends on
#: (`008-prompt-tuning.md` AC10), added at Iteration 5 T3. It is **not** what
#: puts the glossary in the prompt: resolved Q-D injects every term on every
#: call, because a block that appeared only for the questions needing it would
#: tell the model which questions are the ambiguous ones. It exists so a test
#: can assert the declared terms are actually defined, and so a reader of the
#: dataset can see which convention a question turns on.
#: `naive_sql` is the reading a competent analyst produces *without* the
#: glossary. **It is never scored** — it exists so a test can prove the question
#: discriminates (AC12), the direct analogue of `006`'s duplicate-row check. A
#: question whose two readings return the same rows is a free point: it inflates
#: the number while measuring nothing.
_OPTIONAL_KEYS = frozenset({"expect", "glossary", "naive_sql"})

#: Recognised sub-keys of `expect`. `rows` is an exact row count; `value` is the
#: first cell of the first row. Both are things a human can verify by looking at
#: the database, which is the entire point: they check the *gold*, not the model.
_EXPECT_KEYS = frozenset({"rows", "value"})

#: Top-level keys. `retired` is optional; everything else is required.
#:
#: Checked strictly for the same reason question keys are: a misspelled
#: `retierd:` would load cleanly and silently protect nothing.
_REQUIRED_TOP_LEVEL = frozenset({"version", "dataset", "fingerprint", "questions"})
_OPTIONAL_TOP_LEVEL = frozenset({"retired"})

#: Where the dataset lives, resolved relative to this file rather than the
#: working directory so `pytest` and `python -m evals.run_evals` agree.
DEFAULT_PATH = pathlib.Path(__file__).resolve().parent / "questions.yaml"


class DatasetError(ValueError):
    """The dataset is malformed, or no longer describes the live database.

    A distinct type so the runner can tell "your dataset is broken" — which
    nothing about the model can fix — from a question that merely failed.
    """


@dataclass(frozen=True)
class Question:
    """One evaluation case.

    `id` is stable and never re-used for a different question (AC2). A case that
    gets harder becomes a new id; editing one in place makes every earlier
    `EVALS.md` entry describe something that no longer exists.

    `ordered` says whether row order is part of the answer (AC4). Required on
    every question — there is no default, because a default would silently apply
    one policy to whichever questions forgot to say.

    `covers` names the relations the reference query touches, and is what AC5's
    coverage test is computed from.

    `glossary` names the business terms the question turns on, and is empty for
    a question that turns on none. Declaring a term does not change the prompt —
    every term is injected on every call (resolved Q-D) — it declares a
    dependency, so `tests/test_glossary.py` can prove the term is defined.

    `split` is `dev` or `test`. Frozen at Iteration 5 T4, before any tuning ran
    against it, and pinned by `tests/test_splits.py`. Moving a question between
    splits fails that test the way reusing a retired id fails the loader.
    """

    id: str
    tier: str
    question: str
    gold_sql: str
    ordered: bool
    covers: tuple[str, ...]
    split: str
    glossary: tuple[str, ...] = ()
    #: The reading a competent analyst produces *without* the glossary.
    #: **Never scored** (AC12); present on the `expert` tier only.
    naive_sql: str = ""
    expect_rows: int | None = None
    expect_value: object = None
    has_expect_value: bool = False


@dataclass(frozen=True)
class Dataset:
    """The whole file, validated.

    `retired` maps a withdrawn question id to the reason it was withdrawn. It
    exists to make AC2 *enforceable* rather than merely stated: an id that has
    ever been scored must never come back meaning something else, and the only
    reliable way to guarantee that is to refuse the collision at load time.
    A comment in the YAML would have been a convention, and conventions do not
    survive a year of edits.

    Retired questions are not scored and are not part of coverage. They stay in
    the file because the reason is the useful part -- it is a record of how the
    benchmark was wrong, which is exactly the sort of thing that gets quietly
    lost otherwise.
    """

    version: int
    name: str
    fingerprint: dict[str, int]
    questions: tuple[Question, ...]
    retired: dict[str, str] = field(default_factory=dict)

    def by_tier(self, tier: str) -> tuple[Question, ...]:
        return tuple(q for q in self.questions if q.tier == tier)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DatasetError(message)


def _parse_question(raw, index: int) -> Question:
    """Validate one question mapping (AC6)."""
    where = f"question #{index + 1}"
    _require(isinstance(raw, dict), f"{where} is not a mapping")

    identifier = raw.get("id")
    _require(
        isinstance(identifier, str) and identifier.strip() != "",
        f"{where} has no usable id",
    )
    where = f"question {identifier!r}"

    keys = set(raw)
    missing = _REQUIRED_KEYS - keys
    _require(not missing, f"{where} is missing {sorted(missing)}")

    # The rule this loader exists for. `orderd: true` would otherwise load and
    # score every top-N question order-insensitively, undetectably.
    unknown = keys - _REQUIRED_KEYS - _OPTIONAL_KEYS
    _require(
        not unknown,
        f"{where} has unknown keys {sorted(unknown)} — a misspelled field would "
        f"otherwise take a silent default",
    )

    tier = raw["tier"]
    _require(tier in TIERS, f"{where} has unknown tier {tier!r}; expected one of {list(TIERS)}")

    question_text = raw["question"]
    _require(
        isinstance(question_text, str) and question_text.strip() != "",
        f"{where} has an empty question",
    )

    gold_sql = raw["gold_sql"]
    _require(
        isinstance(gold_sql, str) and gold_sql.strip() != "",
        f"{where} has an empty gold_sql",
    )

    ordered = raw["ordered"]
    # A real bool, not something truthy. YAML turns `yes` and `true` into bools
    # but leaves `"true"` a string, and a string is truthy — so a quoted value
    # would make an unordered question order-sensitive without a word of
    # complaint.
    _require(
        isinstance(ordered, bool),
        f"{where} has a non-boolean `ordered` ({ordered!r}); quote-free true or false",
    )

    covers = raw["covers"]
    _require(
        isinstance(covers, list)
        and covers
        and all(isinstance(c, str) and c.strip() for c in covers),
        f"{where} has an empty or non-string `covers` list",
    )

    split = raw["split"]
    _require(
        split in SPLITS,
        f"{where} has unknown split {split!r}; expected one of {list(SPLITS)}",
    )

    # AC12. Required on `expert` and refused everywhere else: a naive reading is
    # only meaningful where the question turns on interpretation, and a question
    # on this tier without one has nothing proving it is not a free point.
    naive_sql = ""
    if tier == TIER_EXPERT:
        _require(
            "naive_sql" in raw,
            f"{where} is on the {TIER_EXPERT!r} tier and has no `naive_sql`; "
            f"without one nothing proves the question discriminates (AC12)",
        )
        naive_sql = raw["naive_sql"]
        _require(
            isinstance(naive_sql, str) and naive_sql.strip() != "",
            f"{where} has an empty `naive_sql`",
        )
        _require(
            naive_sql.strip() != gold_sql.strip(),
            f"{where} has `naive_sql` identical to `gold_sql`, so the question "
            f"is a free point under either reading (AC12)",
        )
        _require(
            bool(raw.get("glossary")),
            f"{where} is on the {TIER_EXPERT!r} tier and declares no glossary "
            f"term; an expert question is hard because of a stated convention",
        )
    else:
        _require(
            "naive_sql" not in raw,
            f"{where} has `naive_sql` on the {tier!r} tier; it is permitted on "
            f"{TIER_EXPERT!r} only, where a naive reading is what is being tested",
        )

    # Validated as strictly as `covers`, and for the same reason: a declared
    # term that is a typo would silently declare nothing, and the forward test
    # in `tests/test_glossary.py` would then pass by having nothing to check.
    glossary_terms: tuple[str, ...] = ()
    if "glossary" in raw:
        declared = raw["glossary"]
        _require(
            isinstance(declared, list)
            and declared
            and all(isinstance(t, str) and t.strip() for t in declared),
            f"{where} has an empty or non-string `glossary` list; omit the key "
            f"entirely if the question turns on no business term",
        )
        _require(
            len(set(declared)) == len(declared),
            f"{where} declares a glossary term twice",
        )
        glossary_terms = tuple(declared)

    expect_rows: int | None = None
    expect_value: object = None
    has_expect_value = False

    if "expect" in raw:
        # Resolved D-2: easy tier only. On `medium` and `hard` the answer is not
        # knowable by inspection, so an `expect` there would be copied from a
        # run of the gold query — which checks nothing, and is how literal
        # snapshots would creep back in.
        _require(
            tier == "easy",
            f"{where} has `expect` on the {tier!r} tier; it is permitted on `easy` "
            f"only, where the answer is verifiable by inspection",
        )
        expect = raw["expect"]
        _require(isinstance(expect, dict) and expect, f"{where} has an empty `expect`")
        unknown_expect = set(expect) - _EXPECT_KEYS
        _require(
            not unknown_expect,
            f"{where} has unknown `expect` keys {sorted(unknown_expect)}",
        )
        if "rows" in expect:
            expect_rows = expect["rows"]
            _require(
                isinstance(expect_rows, int) and not isinstance(expect_rows, bool),
                f"{where} has a non-integer `expect.rows`",
            )
        if "value" in expect:
            expect_value = expect["value"]
            has_expect_value = True

    return Question(
        id=identifier,
        tier=tier,
        question=question_text,
        gold_sql=gold_sql,
        ordered=ordered,
        covers=tuple(covers),
        split=split,
        glossary=glossary_terms,
        naive_sql=naive_sql,
        expect_rows=expect_rows,
        expect_value=expect_value,
        has_expect_value=has_expect_value,
    )


def split_fingerprint(dataset: Dataset) -> str:
    """A hash of the frozen split membership (`008-prompt-tuning.md` §3, guard 2).

    **Derived, not declared**, exactly like the prompt fingerprint and for the
    same reason: a version constant somebody bumps fails in the situation that
    matters, where the split changes and the constant does not.

    Recorded in `EVALS.md` beside every number, so a figure taken under a
    different split is *visibly* a different figure rather than one that merely
    looks comparable. It will change once more when T6 adds the expert tier —
    expected, and before any tuning, which is the whole point of the ordering.

    Hashes the sorted `(id, split)` pairs and nothing else. Question wording is
    deliberately excluded: this identifies the partition, and `006`'s dataset
    fingerprint already identifies the data.
    """
    material = "\n".join(
        f"{question.id}:{question.split}"
        for question in sorted(dataset.questions, key=lambda q: q.id)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def questions_in_split(dataset: Dataset, split: str | None) -> tuple[Question, ...]:
    """The questions belonging to one split, in dataset order (`006` AC22).

    `None` means every question — the `all` option of `--split`, which exists so
    a full-corpus number stays reproducible rather than requiring two runs to be
    added together.

    Raises:
        DatasetError: on an unknown split name. A typo must not silently select
            the whole corpus and be recorded as a dev number.
    """
    if split is None:
        return dataset.questions
    if split not in SPLITS:
        raise DatasetError(f"unknown split {split!r}; expected one of {list(SPLITS)}")
    return tuple(q for q in dataset.questions if q.split == split)


def parse_dataset(raw_text: str, source: str = "<string>") -> Dataset:
    """Parse and validate dataset YAML (AC6).

    Separate from `load_dataset` so the malformed-dataset tests need no
    temporary files, and so every rule below is exercised directly rather than
    through the filesystem.
    """
    try:
        document = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise DatasetError(f"{source} is not valid YAML: {exc}") from exc

    _require(isinstance(document, dict), f"{source} must contain a mapping at the top level")

    for key in sorted(_REQUIRED_TOP_LEVEL):
        _require(key in document, f"{source} is missing the {key!r} key")

    unknown_top = set(document) - _REQUIRED_TOP_LEVEL - _OPTIONAL_TOP_LEVEL
    _require(
        not unknown_top,
        f"{source} has unknown top-level keys {sorted(unknown_top)} — a "
        f"misspelled `retired` would silently protect nothing",
    )

    version = document["version"]
    _require(
        isinstance(version, int) and not isinstance(version, bool),
        f"{source} has a non-integer version",
    )

    fingerprint = document["fingerprint"]
    _require(
        isinstance(fingerprint, dict) and fingerprint,
        f"{source} has an empty fingerprint; AC12 needs at least one relation",
    )
    for relation, count in fingerprint.items():
        _require(
            isinstance(count, int) and not isinstance(count, bool),
            f"{source} fingerprint entry {relation!r} is not an integer row count",
        )

    raw_questions = document["questions"]
    _require(isinstance(raw_questions, list), f"{source} has a non-list `questions`")
    _require(
        MIN_QUESTIONS <= len(raw_questions) <= MAX_QUESTIONS,
        f"{source} holds {len(raw_questions)} questions; AC1 requires "
        f"{MIN_QUESTIONS}-{MAX_QUESTIONS}",
    )

    questions = tuple(
        _parse_question(raw, index) for index, raw in enumerate(raw_questions)
    )

    seen: dict[str, int] = {}
    for position, question in enumerate(questions):
        if question.id in seen:
            raise DatasetError(
                f"{source} has duplicate id {question.id!r} at positions "
                f"{seen[question.id] + 1} and {position + 1}; ids must be unique "
                f"or `EVALS.md` history stops being comparable"
            )
        seen[question.id] = position

    retired = document.get("retired") or {}
    _require(isinstance(retired, dict), f"{source} has a non-mapping `retired`")
    for identifier, reason in retired.items():
        _require(
            isinstance(reason, str) and reason.strip() != "",
            f"{source} retires {identifier!r} with no reason; the reason is the "
            f"useful part of a retirement",
        )

    # AC2, made enforceable. A retired id has already been scored under one
    # meaning, and letting it return under another would silently corrupt every
    # comparison against the `EVALS.md` entries that used it.
    resurrected = sorted(set(retired) & set(seen))
    _require(
        not resurrected,
        f"{source} reuses retired id(s) {resurrected}. A retired id has already "
        f"been scored and published; give the new question a new id instead",
    )

    return Dataset(
        version=version,
        name=str(document["dataset"]),
        fingerprint=dict(fingerprint),
        questions=questions,
        retired=dict(retired),
    )


def load_dataset(path: pathlib.Path | str | None = None) -> Dataset:
    """Read and validate `evals/questions.yaml`."""
    resolved = pathlib.Path(path) if path is not None else DEFAULT_PATH
    try:
        raw_text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatasetError(f"Could not read the dataset at {resolved}: {exc}") from exc
    return parse_dataset(raw_text, source=str(resolved))


def count_query(relation: str) -> str:
    """`SELECT COUNT(*) FROM "<relation>"` for the fingerprint check.

    Built as a sqlglot AST and rendered, never formatted into a string — the
    same discipline `api/db/sampling.py` applies, for the same reason: the AST
    is what guarantees the identifier stays an identifier no matter what it
    contains. The relation names here come from a committed file rather than
    from a model, but a project with one safe way to build a query and one
    convenient way is a project that will eventually use the convenient one.
    """
    return (
        exp.select(exp.Count(this=exp.Star()))
        .from_(exp.to_identifier(relation, quoted=True))
        .sql(dialect=DIALECT)
    )


def verify_fingerprint(dataset: Dataset) -> None:
    """Assert the database still holds the data the dataset was written against (AC12).

    Raises:
        DatasetError: if a count differs, or the relation is gone.

    Raising rather than warning is the point. A reseeded or drifted database
    produces a number that is *not comparable* to previous ones, and an
    incomparable number appended to `EVALS.md` alongside comparable ones is
    worse than a run that refused to finish — it is wrong in a way nobody can
    detect later.

    Goes through `execute_sql()` like everything else (AC20). The rule in
    `000-project.md` §4 names `run_evals.py` explicitly, and reaching around
    the validator to count rows would be exactly the "it's only a helper"
    reasoning that rule exists to refuse.
    """
    from api.db.execution import execute_sql

    for relation in sorted(dataset.fingerprint):
        expected = dataset.fingerprint[relation]
        result = execute_sql(count_query(relation))
        if not result.ok:
            raise DatasetError(
                f"Fingerprint check failed: could not count {relation!r} "
                f"({result.category}: {result.error})"
            )
        actual = result.rows[0][0]
        if actual != expected:
            raise DatasetError(
                f"Fingerprint mismatch: {relation!r} holds {actual} rows, the "
                f"dataset expects {expected}. The database underneath has "
                f"changed, so this run would not be comparable to earlier "
                f"entries in EVALS.md. Reseed it, or bump the dataset version "
                f"and record why."
            )
