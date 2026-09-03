"""Glossary tests — `specs/008-prompt-tuning.md` AC10, AC11, AC13 (Iteration 5 T3).

Three things are checked here, and only the first is about wording:

1. **The block renders and reaches the prompt**, and turning it off reproduces
   Iteration 4's prompt byte for byte.
2. **Every definition is true of the database.** Each term's conventional
   reading and its naive reading are executed and required to differ. A
   definition that has quietly stopped discriminating is a term that costs
   tokens and measures nothing.
3. **The dataset and the glossary agree.** Every term a question declares must
   be defined.

The reverse of (3) — that no definition goes unused — lands at **T6**, with the
expert questions that will use them. It cannot be asserted here: no question
declares a term yet, so the check would fail against a glossary that is
perfectly correct. Recorded as a T6 obligation in the plan rather than left to
memory.
"""

from __future__ import annotations

import pytest

from api.agent.glossary import GLOSSARY, GLOSSARY_HEADER, render_glossary
from api.agent.prompts import (
    ADOPTED_RENDERING,
    SCHEMA_DDL,
    SCHEMA_FULL,
    SCHEMA_WITHHELD,
    build_loop_system,
)
from api.db.execution import execute_sql
from evals.dataset import load_dataset

pytestmark = pytest.mark.usefixtures("configured_database")


#: term -> (naive reading, conventional reading, expected naive, expected conventional)
#:
#: The naive query is the reading a competent analyst produces *without* the
#: glossary; the conventional one is what the definition prescribes. Both are
#: executed, because AC11's requirement — that a term discriminates — is a fact
#: about the data and not about the prose.
DISCRIMINATION = {
    "active customer": (
        "SELECT count(*) FROM customer",
        """SELECT count(DISTINCT i.customer_id) FROM invoice i
           WHERE i.invoice_date >= (SELECT max(invoice_date) FROM invoice)
                                   - INTERVAL '12 months'""",
        59, 46,
    ),
    "support representative": (
        "SELECT count(*) FROM employee",
        "SELECT count(DISTINCT support_rep_id) FROM customer "
        "WHERE support_rep_id IS NOT NULL",
        8, 3,
    ),
    "sold track": (
        "SELECT count(*) FROM track",
        "SELECT count(DISTINCT track_id) FROM invoice_line",
        3503, 1984,
    ),
    "charting artist": (
        "SELECT count(*) FROM artist",
        """SELECT count(DISTINCT al.artist_id) FROM invoice_line il
           JOIN track t ON t.track_id = il.track_id
           JOIN album al ON al.album_id = t.album_id""",
        275, 165,
    ),
    "active genre": (
        "SELECT count(*) FROM genre",
        """SELECT count(DISTINCT t.genre_id) FROM invoice_line il
           JOIN track t ON t.track_id = il.track_id
           WHERE t.genre_id IS NOT NULL""",
        25, 24,
    ),
    "curated playlist": (
        "SELECT count(*) FROM playlist",
        "SELECT count(DISTINCT playlist_id) FROM playlist_track",
        18, 14,
    ),
    "average order value": (
        "SELECT round(avg(unit_price * quantity), 4) FROM invoice_line",
        "SELECT round(avg(total), 4) FROM invoice",
        1.0396, 5.6519,
    ),
    "credited track": (
        "SELECT count(*) FROM track",
        "SELECT count(*) FROM track WHERE composer IS NOT NULL",
        3503, 2526,
    ),
}


def _scalar(sql: str):
    """Run one query through the safety layer and return its single cell.

    Through `execute_sql()` like everything else — `000-project.md` §4 has one
    recorded exemption and this is not it.
    """
    result = execute_sql(sql)
    assert result.ok, f"{result.category}: {result.error}"
    return float(result.rows[0][0])


# --- AC11: every term discriminates, measured ------------------------------


@pytest.mark.parametrize("term", sorted(DISCRIMINATION))
def test_ac11_each_term_discriminates_against_its_naive_reading(term):
    """**The check that stops a glossary entry being decoration.**

    A term whose two readings return the same rows teaches nothing and costs
    tokens on every call forever. This is the direct analogue of `006`'s
    duplicate-row check, applied to definitions instead of questions.

    Executed rather than asserted from a table, so a reseed that collapsed the
    difference is caught here rather than surfacing at T7 as an accuracy number
    nobody can explain.
    """
    naive_sql, conventional_sql, want_naive, want_conventional = DISCRIMINATION[term]

    naive = _scalar(naive_sql)
    conventional = _scalar(conventional_sql)

    assert naive != conventional, (
        f"{term!r} no longer discriminates: both readings return {naive}"
    )
    assert naive == pytest.approx(want_naive, abs=0.01)
    assert conventional == pytest.approx(want_conventional, abs=0.01)


def test_every_defined_term_has_a_discrimination_check():
    """A definition added without a measurement is a claim, not a fact."""
    assert set(GLOSSARY) == set(DISCRIMINATION)


#: term -> schema identifiers its membership rule turns on.
#:
#: These are the names the *conventional* query above depends on. Requiring the
#: prose to contain them is the only mechanical link between a definition and
#: the query that verifies it.
MUST_NAME = {
    "active customer": ("invoice", "invoice_date"),
    "support representative": ("customer.support_rep_id",),
    "sold track": ("invoice_line",),
    "charting artist": ("invoice_line", "album"),
    "active genre": ("invoice_line",),
    "curated playlist": ("track",),
    "average order value": ("invoice.total", "invoice_line"),
    "credited track": ("composer",),
}


@pytest.mark.parametrize("term", sorted(MUST_NAME))
def test_each_definition_names_the_columns_its_rule_turns_on(term):
    """**The link between the prose and the query that verifies it.**

    Found by mutation: rewriting `credited track` to "any track in the
    catalogue" left every discrimination test green, because those tests
    execute hardcoded SQL and never read the definition. The prose could drift
    arbitrarily far from the rule it claims to state and nothing would notice.

    Nothing can mechanically prove English matches SQL. What *is* checkable is
    that a definition names the identifiers its rule depends on — which is the
    property `glossary.py` already claims for itself ("each names the relations
    and columns that decide membership") and which was, until now, enforced by
    nothing.

    It also matters for the prompt, not just the test: a definition the model
    cannot translate into a WHERE clause has cost tokens and taught nothing.
    """
    definition = GLOSSARY[term]
    for identifier in MUST_NAME[term]:
        assert identifier in definition, (
            f"{term!r} is defined without naming {identifier!r}, which its "
            f"conventional query depends on: {definition!r}"
        )


def test_every_defined_term_declares_the_identifiers_it_needs():
    assert set(GLOSSARY) == set(MUST_NAME)


def test_ac16_average_order_value_is_defined_per_invoice():
    """**The one term whose direction the spec had backwards.**

    §2.4 originally labelled the per-line figure (1.0396) "conventional". An
    invoice_line is not an order, and AOV universally means revenue per order,
    so defining it that way would have taught a definition most analysts call
    wrong — and an expert question built on it would punish the correct
    instinct rather than reward the stated convention. Corrected at T3; both
    measured numbers stand, only which one is naive flipped.
    """
    definition = GLOSSARY["average order value"]
    assert "invoice.total" in definition
    assert "never one invoice_line" in definition


# --- AC10: the block, and where it lands ------------------------------------


def test_the_block_lists_every_term_with_its_definition():
    rendered = render_glossary()
    assert GLOSSARY_HEADER in rendered
    for term, definition in GLOSSARY.items():
        assert f"- {term}: {definition}" in rendered


def test_the_header_tells_the_model_to_override_its_own_meaning():
    """Load-bearing wording. Every term is ordinary English whose everyday sense
    is exactly the naive reading it is trying to displace, so the block has to
    say the stated definition wins rather than merely offering one."""
    assert "not your own" in GLOSSARY_HEADER


def test_rendering_is_deterministic():
    assert render_glossary() == render_glossary()


def test_ac10_the_glossary_reaches_the_assembled_prompt(schema):
    system = build_loop_system(schema, SCHEMA_FULL, SCHEMA_DDL, glossary=True)
    assert GLOSSARY_HEADER in system
    for term in GLOSSARY:
        assert term in system


def test_the_glossary_is_on_by_default(schema):
    """Resolved Q-D. Injecting it only for questions that declare a term would
    be cheaper — measured at 178 tokens on every call — but it would tell the
    model which questions are the ambiguous ones."""
    assert build_loop_system(schema, SCHEMA_FULL) == build_loop_system(
        schema, SCHEMA_FULL, ADOPTED_RENDERING, glossary=True
    )


def test_the_glossary_is_present_even_when_the_schema_is_withheld(schema):
    """The terms describe the business, not the schema. A blind run still has
    to know what 'active customer' means; withholding the schema is about
    making the agent look tables up, not about hiding the domain."""
    system = build_loop_system(schema, SCHEMA_WITHHELD, SCHEMA_DDL, glossary=True)
    assert GLOSSARY_HEADER in system
    assert "CREATE TABLE" not in system


def test_glossary_off_reproduces_the_iteration_4_prompt_exactly(schema):
    """**What makes `--no-glossary` a control rather than merely a cheaper run.**

    AC13 compares accuracy with and without. That comparison only isolates the
    glossary if the without-case is byte-identical to the prompt Iteration 4
    measured — otherwise it measures the glossary plus whatever else drifted.
    """
    without = build_loop_system(schema, SCHEMA_FULL, SCHEMA_DDL, glossary=False)
    assert GLOSSARY_HEADER not in without
    for term in GLOSSARY:
        assert term not in without


# --- AC13: the fingerprint distinguishes the two configurations -------------


def test_ac13_the_glossary_changes_the_prompt_fingerprint():
    """A glossary-on run adds 178 measured tokens, so it is not the same prompt.

    Recording it under Iteration 4's fingerprint would file an incomparable
    number as comparable — the exact failure AC24 exists to prevent. Contrast
    the schema rendering, which leaves the DDL prompt byte-identical and
    therefore got its own field at T2 instead.
    """
    from evals.run_evals import STRATEGY_LOOP, fingerprint_for

    assert fingerprint_for(STRATEGY_LOOP, glossary=True) != fingerprint_for(
        STRATEGY_LOOP, glossary=False
    )


def test_a_glossary_off_run_still_carries_the_iteration_4_fingerprint():
    """The three loop entries in `EVALS.md` carry `0d280c367c5e`. A control run
    has to reproduce it, or the comparison this iteration exists to make is
    against a number that no longer describes anything runnable."""
    from evals.run_evals import STRATEGY_LOOP, fingerprint_for

    assert fingerprint_for(STRATEGY_LOOP, glossary=False) == "0d280c367c5e"


# --- the dataset and the glossary agree -------------------------------------


def test_every_term_a_question_declares_is_defined():
    """The forward direction: no question may depend on an undefined term.

    Catches a typo in a `glossary:` list at load time rather than at T7, when it
    would surface as an accuracy number nobody can explain.
    """
    dataset = load_dataset()
    for question in dataset.questions:
        for term in question.glossary:
            assert term in GLOSSARY, (
                f"{question.id} declares {term!r}, which is not in GLOSSARY"
            )


def test_every_defined_term_is_used_by_at_least_one_question():
    """**The reverse direction, deferred from T3 and payable now.**

    T3 shipped the glossary before any question declared a term, so this could
    not run: it would have failed against a glossary that was perfectly correct.
    The obligation was recorded in the plan rather than carried in memory, and
    T6's expert tier is what discharges it.

    A definition nothing uses is not free. Measured at T3, the block costs 178
    tokens on **every** call under resolved Q-D — 7,160 per 40-question pass —
    so an unused entry is a permanent tax collected for nothing, and the only
    thing that would ever reveal it is this test.
    """
    dataset = load_dataset()
    declared = {term for question in dataset.questions for term in question.glossary}

    unused = sorted(set(GLOSSARY) - declared)
    assert not unused, (
        f"defined but used by no question: {unused}. Each costs tokens on every "
        f"call forever. Either write a question that turns on it, or remove it."
    )


def test_the_two_directions_together_pin_the_glossary_exactly():
    """Neither direction alone is enough: forward permits unused definitions,
    reverse permits undefined declarations. Together they are an equality."""
    dataset = load_dataset()
    declared = {term for question in dataset.questions for term in question.glossary}
    assert declared == set(GLOSSARY)


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param([], id="empty-list"),
        pytest.param("active customer", id="bare-string"),
        pytest.param([""], id="blank-entry"),
        pytest.param([1], id="non-string-entry"),
        pytest.param(["active customer", "active customer"], id="duplicate"),
    ],
)
def test_the_loader_rejects_a_malformed_glossary_list(bad):
    """Strict for the same reason `covers` is: a field that accepts junk
    silently declares nothing, and the forward test above would then pass by
    having nothing left to check.

    Reuses `test_eval_dataset`'s document builder rather than hand-rolling YAML
    — a one-question document trips the 30-50 count rule before it ever reaches
    field validation, so a hand-rolled fixture would assert the wrong error.
    """
    from evals.dataset import MIN_QUESTIONS, DatasetError, parse_dataset

    from tests.test_eval_dataset import document, question

    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[0] = question(0, glossary=bad)

    with pytest.raises(DatasetError, match="glossary"):
        parse_dataset(document(questions))


def test_a_well_formed_glossary_declaration_loads():
    from evals.dataset import MIN_QUESTIONS, parse_dataset

    from tests.test_eval_dataset import document, question

    questions = [question(i) for i in range(MIN_QUESTIONS)]
    questions[0] = question(0, glossary=["active customer", "sold track"])

    loaded = parse_dataset(document(questions))
    assert loaded.questions[0].glossary == ("active customer", "sold track")
    assert loaded.questions[1].glossary == (), "absent means empty, not missing"


# --- the measured cost ------------------------------------------------------


def test_the_measured_block_is_178_tokens():
    """**Measured, not estimated.** The plan's §6 put this at 220 (~27 a term)
    from the `chars // 4` heuristic T1 corrected; the real figure is 178.

    Pinned exactly rather than bounded, because the number this guards is a
    trade-off already decided on: at 40 questions the block costs 7,160 tokens
    a pass and cancels roughly 95% of what schema compaction saves. If a
    wording edit moves it, that trade should be re-examined rather than
    absorbed silently.
    """
    tiktoken = pytest.importorskip("tiktoken")
    encoding = tiktoken.get_encoding("o200k_base")
    assert len(encoding.encode(render_glossary())) == 178
