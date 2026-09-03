"""The corpus itself — `specs/006-evals.md` AC1-AC5, AC12, AC26.

Needs the database; the loader's own rules are tested purely in
`test_eval_dataset.py`.

**This file is where the benchmark is held to a standard, rather than the code
that runs it.** A dataset defect does not announce itself — it silently changes
what the number means, and everything downstream inherits it. AC26 in particular
resolves the deliberate skip that has been visible in `test_validator.py` since
Iteration 1: *if Gate 2 rejects a query the benchmark depends on, the gate is
wrong, not the benchmark.*

Nothing here calls a model. The questions were authored and verified before the
runner existed, which is the ordering discipline `006-evals.md` §7 Q-D asks for,
and these tests are the part of it that keeps holding after today.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

import pytest

from api.db.execution import MAX_ROWS, execute_sql
from evals.dataset import MAX_QUESTIONS, MIN_QUESTIONS, TIERS, load_dataset
from evals.scoring import normalise_value


@pytest.fixture(scope="module")
def dataset():
    return load_dataset()


@pytest.fixture(scope="module")
def gold_results(dataset, configured_database):
    """Every gold query, executed once. Module-scoped: 40 queries is a few
    seconds, and every test below wants the same answers."""
    return {q.id: execute_sql(q.gold_sql) for q in dataset.questions}


def tables_in(sql: str) -> set[str]:
    """Relations a query actually references, per sqlglot.

    Derived rather than trusted. A hand-maintained `covers` list drifts the
    first time a gold query is edited, and it drifts *silently* — the coverage
    assertion would keep passing against relations the query no longer touches.
    """
    parsed = sqlglot.parse_one(sql, dialect="postgres")
    return {table.name for table in parsed.find_all(exp.Table)}


# --- AC26 / 002 AC19: Gate 2 accepts the whole benchmark -------------------


def test_ac26_every_gold_query_passes_gate_2(dataset):
    """**The criterion that has been skipped since Iteration 1.**

    A gold query Gate 2 rejects is a Gate 2 defect, not a broken question, and
    until this dataset existed nothing in the project could detect one. The
    validator was built against hostile input; this is the first time it is held
    to the opposite standard — that it lets legitimate analytics through.

    The corpus is not gentle about it: correlated subqueries, a `UNION`, a window
    function over a derived table, `CASE`, `DISTINCT` inside an aggregate, a
    four-table join and a scalar subquery in a `HAVING` clause.
    """
    from api.safety.validator import validate_sql

    rejected = []
    for question in dataset.questions:
        ok, reason = validate_sql(question.gold_sql)
        if not ok:
            rejected.append(f"{question.id}: {reason}")

    assert not rejected, (
        "Gate 2 rejected reference queries. Per AC26 the gate is wrong, not the "
        "benchmark:\n" + "\n".join(rejected)
    )


def test_ac26_the_union_question_exercises_recursive_branch_validation(dataset):
    """`002` AC25 end to end. Both branches of a set operation must be inspected,
    and `hard-005` is a real `UNION` rather than a synthetic one."""
    from api.safety.validator import validate_sql

    union = next(q for q in dataset.questions if q.id == "hard-005")
    assert "UNION" in union.gold_sql.upper()
    assert validate_sql(union.gold_sql)[0]


# --- §6: a gold query that does not work defines wrong truth ---------------


def test_every_gold_query_executes(dataset, gold_results):
    failures = [
        f"{q.id}: {gold_results[q.id].category}: {gold_results[q.id].error}"
        for q in dataset.questions
        if not gold_results[q.id].ok
    ]
    assert not failures, "reference queries must run:\n" + "\n".join(failures)


def test_every_gold_query_returns_at_least_one_row(dataset, gold_results):
    """**A zero-row gold is a broken question, not a hard one.** It makes every
    wrong answer that also returns nothing score as correct — and "returns
    nothing" is exactly what a confused model does."""
    empty = [q.id for q in dataset.questions if gold_results[q.id].row_count == 0]
    assert not empty, f"reference queries returning no rows: {empty}"


def test_no_gold_query_is_truncated(dataset, gold_results):
    """A gold at the 1000-row cap makes scoring arbitrary: two correct queries
    with different unspecified orderings would be truncated to different
    thousand-row subsets and compare unequal. Not in `006-evals.md` §6's list,
    but the same class of defect as a zero-row gold."""
    truncated = [
        q.id
        for q in dataset.questions
        if gold_results[q.id].truncated or gold_results[q.id].row_count >= MAX_ROWS
    ]
    assert not truncated, f"reference queries hitting the row cap: {truncated}"


def test_no_gold_query_returns_duplicate_identical_rows(dataset, gold_results):
    """Identical duplicate rows in a gold almost always mean an ambiguous
    grouping, and this check is in the suite because one did.

    An earlier `medium-009` asked which playlists had the most tracks. Chinook
    has four duplicated playlist *names*, and playlists 1 and 8 are both called
    Music with 3290 tracks each — so grouping by id gave two identical
    `('Music', 3290)` rows and grouping by name gave one `('Music', 6580)`.
    Both readings are correct; the question was not. No model could have known
    which was meant, and the duplicate rows were the visible symptom.
    """
    offenders = []
    for question in dataset.questions:
        rows = gold_results[question.id].rows
        normalised = [tuple(normalise_value(c) for c in row) for row in rows]
        if len(set(normalised)) != len(normalised):
            offenders.append(question.id)
    assert not offenders, (
        f"reference queries returning duplicate identical rows, which usually "
        f"means the question is ambiguously grouped: {offenders}"
    )


# --- D-2: the sanity check on the gold itself ------------------------------


def test_d2_every_easy_question_carries_an_expect(dataset):
    """Resolved D-2. On the easy tier the answer is knowable by inspection, so
    there is no excuse for not asserting it — and `expect` is the only thing in
    the dataset that checks the gold against something other than itself."""
    without = [q.id for q in dataset.by_tier("easy") if q.expect_rows is None and not q.has_expect_value]
    assert not without, f"easy questions with no `expect` sanity check: {without}"


def test_d2_expect_holds_against_the_live_database(dataset, gold_results):
    """The check that would catch a typo in a gold query — a `WHERE` that
    silently filters everything, a join that fans rows out."""
    problems = []
    for question in dataset.questions:
        result = gold_results[question.id]
        if not result.ok:
            continue
        if question.expect_rows is not None and result.row_count != question.expect_rows:
            problems.append(
                f"{question.id}: expected {question.expect_rows} rows, got {result.row_count}"
            )
        if question.has_expect_value:
            actual = result.rows[0][0] if result.rows else None
            if normalise_value(actual) != normalise_value(question.expect_value):
                problems.append(
                    f"{question.id}: expected {question.expect_value!r}, got {actual!r}"
                )
    assert not problems, "\n".join(problems)


# --- AC5: coverage is derived from the schema, not from taste --------------


def test_ac5_every_relation_is_covered(dataset, schema):
    """Coverage is computed against `get_schema()`, so a relation added to the
    database breaks this test until someone writes a question for it. That
    pressure is the point — a benchmark that silently stops covering part of the
    schema still reports a confident number."""
    covered = {relation for q in dataset.questions for relation in q.covers}
    missing = {table.name for table in schema.tables} - covered
    assert not missing, f"relations with no question: {sorted(missing)}"


def test_ac5_the_view_is_covered(dataset, schema):
    """Called out separately because a view is where schema grounding fails: it
    has no primary key, no NOT NULL and no foreign keys, so a model
    pattern-matching on table shape rather than reading the schema will avoid
    it."""
    from api.db.introspection import KIND_VIEW

    views = {t.name for t in schema.tables if t.kind == KIND_VIEW}
    covered = {relation for q in dataset.questions for relation in q.covers}
    assert views and views <= covered, f"uncovered views: {sorted(views - covered)}"


def test_ac5_every_foreign_key_path_is_covered(dataset, schema):
    """Every FK path needs a question that touches both of its endpoints.

    The self-referencing path is excluded here and asserted separately below:
    "both endpoints present" is trivially true for it and would pass without any
    question actually joining the table to itself.
    """
    covered = [set(q.covers) for q in dataset.questions]

    uncovered = []
    for table in schema.tables:
        for foreign_key in table.foreign_keys:
            if foreign_key.referred_table == table.name:
                continue
            endpoints = {table.name, foreign_key.referred_table}
            if not any(endpoints <= c for c in covered):
                uncovered.append(f"{table.name} -> {foreign_key.referred_table}")

    assert not uncovered, f"foreign-key paths with no question: {uncovered}"


def test_ac5_the_self_referencing_path_has_a_real_self_join(dataset, schema):
    """`employee.reports_to -> employee` is the one path a coverage rule stated
    in terms of relation names cannot check, because both endpoints are the same
    relation. So this asserts the shape instead: some gold query must reference
    `employee` twice."""
    self_referencing = {
        table.name
        for table in schema.tables
        for fk in table.foreign_keys
        if fk.referred_table == table.name
    }
    assert self_referencing == {"employee"}, "the schema's self-reference moved"

    def self_join_count(sql: str) -> int:
        parsed = sqlglot.parse_one(sql, dialect="postgres")
        return sum(1 for t in parsed.find_all(exp.Table) if t.name == "employee")

    assert any(self_join_count(q.gold_sql) >= 2 for q in dataset.questions), (
        "no reference query joins employee to itself, so the "
        "employee.reports_to -> employee path is untested"
    )


def test_covers_matches_the_relations_the_gold_query_uses(dataset):
    """Anti-drift. `covers` is what AC5 is computed from, and a hand-maintained
    list would rot silently the first time a gold query was edited — the
    coverage assertion would keep passing against relations the query no longer
    touches. Deriving the truth from the SQL makes that impossible."""
    mismatches = []
    for question in dataset.questions:
        declared = set(question.covers)
        actual = tables_in(question.gold_sql)
        if declared != actual:
            mismatches.append(
                f"{question.id}: covers={sorted(declared)} but the query uses "
                f"{sorted(actual)}"
            )
    assert not mismatches, "\n".join(mismatches)


def test_every_covered_relation_exists(dataset, schema):
    known = {table.name for table in schema.tables}
    unknown = {r for q in dataset.questions for r in q.covers} - known
    assert not unknown, f"`covers` names relations that do not exist: {sorted(unknown)}"


# --- AC1, AC2: the shape of the corpus -------------------------------------


def test_ac1_the_dataset_is_the_declared_size(dataset):
    assert MIN_QUESTIONS <= len(dataset.questions) <= MAX_QUESTIONS
    # 40 through Iteration 4; the expert tier added 10 at Iteration 5 T6.
    assert len(dataset.questions) == 50


def test_ac1_all_three_tiers_are_populated(dataset):
    counts = {tier: len(dataset.by_tier(tier)) for tier in TIERS}
    assert counts == {"easy": 14, "medium": 16, "hard": 10, "expert": 10}


def test_ac2_the_retired_questions_stay_retired(dataset):
    """The two ids withdrawn after the v1 baseline. Pinned by name so that
    bringing either back — under any meaning — fails here as well as in the
    loader."""
    assert set(dataset.retired) == {"medium-008", "medium-016"}
    active = {q.id for q in dataset.questions}
    assert not active & set(dataset.retired)
    assert {"medium-017", "medium-018"} <= active, "the replacements are present"


def test_ac2_every_retirement_explains_itself(dataset):
    for identifier, reason in dataset.retired.items():
        assert len(reason.split()) >= 10, (
            f"{identifier} is retired without a usable explanation; the reason "
            f"is the record of how the benchmark was wrong"
        )


def test_the_dataset_version_was_bumped_when_the_corpus_changed(dataset):
    """`EVALS.md` records the dataset version against every number (AC23), so a
    corpus change that kept the version would make two incomparable runs look
    comparable."""
    # 1 -> 2 when two questions were retired and replaced; 2 -> 3 when the
    # expert tier arrived (AC14). A corpus that changed shape is a different
    # corpus, and every earlier EVALS.md entry describes the old one.
    assert dataset.version == 3, "changing the corpus is a new version"


def test_ac2_ids_are_unique_and_name_their_tier(dataset):
    """A convention, not a requirement — but an id that says its tier makes an
    `EVALS.md` failure list readable without opening the dataset."""
    assert len({q.id for q in dataset.questions}) == len(dataset.questions)
    for question in dataset.questions:
        assert question.id.startswith(question.tier + "-"), question.id


def test_the_hard_tier_actually_exercises_distinct_sql_features(dataset):
    """A tier called `hard` whose questions are all three-table joins is not
    measuring difficulty, it is measuring one thing ten times. Each feature is
    asserted present somewhere in the tier."""
    hard = " ".join(q.gold_sql.upper() for q in dataset.by_tier("hard"))
    for feature in ("OVER (", "UNION", "CASE ", "COUNT(DISTINCT", "HAVING", "EXTRACT("):
        assert feature in hard, f"no hard question uses {feature!r}"


def test_easy_questions_touch_exactly_one_relation(dataset):
    for question in dataset.by_tier("easy"):
        assert len(tables_in(question.gold_sql)) == 1, question.id


def hard_features(sql: str) -> set[str]:
    """Which non-trivial SQL constructs a query uses.

    A boolean "is this interesting" predicate was the first attempt and it was
    wrong twice: it classified the self-join as trivial (one distinct table
    name, referenced twice) and then the `CASE` and `EXTRACT` questions as
    trivial too, because both are genuinely single-table. Difficulty here is
    about the construct, not the join count.
    """
    parsed = sqlglot.parse_one(sql, dialect="postgres")
    features = set()
    # Table *references*, not distinct names, so a self-join counts as two.
    if len(list(parsed.find_all(exp.Table))) >= 2:
        features.add("multi-table")
    if list(parsed.find_all(exp.Subquery)):
        features.add("subquery")
    if list(parsed.find_all(exp.Window)):
        features.add("window")
    if list(parsed.find_all(exp.SetOperation)):
        features.add("set-operation")
    if list(parsed.find_all(exp.Case)):
        features.add("case")
    if list(parsed.find_all(exp.Having)):
        features.add("having")
    if list(parsed.find_all(exp.Extract)):
        features.add("extract")
    # sqlglot models `count(DISTINCT x)` as `Count(this=Distinct(...))`, not as
    # a `distinct=True` argument — checking `args.get("distinct")` silently
    # found nothing. Scoped to inside an aggregate on purpose: a bare
    # `SELECT DISTINCT` also produces an `exp.Distinct` node and is not the same
    # construct.
    if any(isinstance(a.this, exp.Distinct) for a in parsed.find_all(exp.AggFunc)):
        features.add("distinct-aggregate")
    return features


def test_the_feature_detector_detects():
    """A silently broken detector would make every tiering assertion above
    vacuous, which is how `distinct-aggregate` went unnoticed the first time."""
    assert hard_features("SELECT count(DISTINCT x) FROM t") == {"distinct-aggregate"}
    assert hard_features("SELECT DISTINCT x FROM t") == set()
    assert hard_features("SELECT a FROM t UNION SELECT a FROM u") == {
        "multi-table",
        "set-operation",
    }
    assert "window" in hard_features("SELECT row_number() OVER (ORDER BY a) FROM t")
    assert "case" in hard_features("SELECT CASE WHEN a THEN 1 ELSE 2 END FROM t")
    assert "extract" in hard_features("SELECT extract(year FROM d) FROM t")
    assert hard_features("SELECT a FROM t") == set()


def test_every_hard_question_uses_a_hard_feature(dataset):
    """Guards the tiering itself. A tier called `hard` whose questions are all
    ordinary joins is not measuring difficulty, it is measuring one thing ten
    times — and its accuracy would then be indistinguishable from `medium`,
    which is exactly what AC17 exists to prevent."""
    plain = [q.id for q in dataset.by_tier("hard") if not hard_features(q.gold_sql)]
    assert not plain, f"hard questions using no hard construct: {plain}"


def test_medium_questions_are_not_single_table_lookups(dataset):
    """A `medium` question that drifted into a single-table lookup would inflate
    that tier's accuracy and hide the drift. Three are deliberately single-table
    — a date-range aggregate, a filtered projection and a year filter — so the
    rule is about the tier, not each question."""
    joined = [q for q in dataset.by_tier("medium") if len(tables_in(q.gold_sql)) >= 2]
    assert len(joined) >= 12, f"only {len(joined)} of 16 medium questions join"


# --- AC4 / AC10: the ordered flag ------------------------------------------


def test_ordered_questions_say_so_in_the_question_text(dataset):
    """If order is scored, the question has to ask for it. Scoring order against
    a question that never mentioned it would fail correct answers for a
    requirement the model was never given."""
    hints = ("first", "order", "alphabetical", "ascending", "descending", "highest", "lowest")
    for question in dataset.questions:
        if question.ordered:
            text = question.question.lower()
            assert any(h in text for h in hints), (
                f"{question.id} is scored on row order but does not ask for an "
                f"order: {question.question!r}"
            )


def test_unordered_questions_are_the_majority(dataset):
    """Not a rule, a smell check. Order-sensitivity is the strictest thing this
    scorer does, and a corpus that used it everywhere would depress the baseline
    for presentation rather than correctness."""
    ordered = sum(1 for q in dataset.questions if q.ordered)
    assert ordered < len(dataset.questions) / 2


# --- AC12: the fingerprint --------------------------------------------------


def test_ac12_the_fingerprint_matches_the_live_database(dataset, configured_database):
    from evals.dataset import verify_fingerprint

    verify_fingerprint(dataset)


def test_ac12_a_drifted_fingerprint_aborts(dataset, configured_database):
    """The failure mode this exists for: a reseeded database producing a number
    that is not comparable to the entries above it in `EVALS.md`. Aborting is
    the correct outcome — an incomparable number filed alongside comparable ones
    is wrong in a way nobody can detect later."""
    import dataclasses

    from evals.dataset import DatasetError, verify_fingerprint

    drifted = dataclasses.replace(dataset, fingerprint={"track": 1})
    with pytest.raises(DatasetError, match="Fingerprint mismatch"):
        verify_fingerprint(drifted)


def test_ac12_the_fingerprint_covers_the_tables_the_corpus_leans_on(dataset):
    for relation in ("track", "invoice", "invoice_line", "customer"):
        assert relation in dataset.fingerprint
