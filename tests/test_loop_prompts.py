"""Loop prompt tests — `specs/007-agent-loop.md` AC3, AC16, resolved Q-C and D-2.

**Pure.** Rendering is a function of its arguments, so none of this needs a
database or a provider.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from api.agent.prompts import (
    SCHEMA_COMPACT,
    SCHEMA_DDL,
    ACTION_DESCRIPTIONS,
    EXCLUDED_ACTIONS,
    LOOP_ACTIONS,
    LOOP_SYSTEM_TEMPLATE,
    MAX_CELL_WIDTH,
    SCHEMA_FULL,
    SCHEMA_MODES,
    SCHEMA_WITHHELD,
    SYSTEM_TEMPLATE,
    build_loop_system,
    render_action_list,
    render_rows,
    render_transcript,
)


@dataclass(frozen=True)
class FakeStep:
    attempt: int
    action: str
    sql: str
    observation: str


# --- the action list stays in step with the registry ------------------------


def test_ac4_every_registry_tool_is_either_offered_or_explicitly_excluded():
    """**The rule that stops the prompt and the registry drifting apart.**

    A tool added to `TOOLS` later must be classified — offered to the model, or
    excluded with a reason. Neither outcome can happen by accident, and a tool
    the model is never told about is a tool that does not exist.

    **Containment, not equality, since Iteration 5 T1.** The equality held only
    while every excluded action was still registered. `sample_rows` is now
    excluded *and* unregistered, so `EXCLUDED_ACTIONS` may name retired
    capabilities absent from `TOOLS`. This half keeps the original meaning —
    nothing in the registry is silently unavailable — and `AC4a` below carries
    the other half, which the equality used to cover for free.
    """
    from api.agent.tools import TOOLS

    unclassified = set(TOOLS) - set(LOOP_ACTIONS) - set(EXCLUDED_ACTIONS)
    assert not unclassified, (
        f"registry tools neither offered nor excluded: {sorted(unclassified)}"
    )


def test_ac4a_every_offered_action_can_actually_dispatch():
    """**The failure splitting the equality newly makes possible.**

    An action left in `LOOP_ACTIONS` whose `TOOLS` entry has been removed would
    be described to the model, chosen, and then rejected as unknown — spending
    a call out of three on a capability the prompt advertised. That is strictly
    worse than never offering it, and it is exactly what a half-finished
    removal produces.
    """
    from api.agent.tools import TOOLS

    undispatchable = set(LOOP_ACTIONS) - set(TOOLS)
    assert not undispatchable, (
        f"offered to the model but not dispatchable: {sorted(undispatchable)}"
    )


def test_offered_and_excluded_do_not_overlap():
    assert not set(LOOP_ACTIONS) & set(EXCLUDED_ACTIONS)


def test_every_offered_action_has_a_description():
    for name in LOOP_ACTIONS:
        assert name in ACTION_DESCRIPTIONS


def test_validate_sql_is_excluded_with_a_reason():
    """Strictly dominated by `execute_sql`, which runs Gate 2 first and returns
    the identical reason on rejection. With three calls total — and a schema
    lookup already costing one in the withheld harness — a turn spent learning
    what the next turn would say for free is a turn wasted."""
    assert "validate_sql" in EXCLUDED_ACTIONS
    assert len(EXCLUDED_ACTIONS["validate_sql"].split()) >= 8


def test_ac1_sample_rows_is_excluded_with_its_measured_reason():
    """Retired on evidence, and the evidence is not the token argument.

    Spec §2.1 measures the saving at 19 tokens — 2.0% of the prompt — so cost
    is *not* why it went. It went because it was chosen zero times across 24
    probe calls and every recorded Iteration 4 run. The reason string has to
    carry that, because someone deciding whether to restore it needs the
    evidence rather than the conclusion.
    """
    assert "sample_rows" in EXCLUDED_ACTIONS
    assert len(EXCLUDED_ACTIONS["sample_rows"].split()) >= 8


def test_ac1_the_retired_action_is_not_described_to_the_model():
    from api.agent.prompts import ACTION_DESCRIPTIONS

    assert "sample_rows" not in LOOP_ACTIONS
    assert "sample_rows" not in ACTION_DESCRIPTIONS


def test_the_action_list_renders_in_registry_order():
    rendered = render_action_list(LOOP_ACTIONS)
    positions = [rendered.index(f"ACTION: {name}") for name in LOOP_ACTIONS]
    assert positions == sorted(positions)


def test_an_undescribed_action_fails_loudly():
    """Not defended against on purpose. Discovering it at import beats
    discovering it as an accuracy number nobody can explain."""
    with pytest.raises(KeyError):
        render_action_list(["no_such_tool"])


# --- AC3: the system prompt -------------------------------------------------


def test_the_system_prompt_offers_exactly_the_loop_actions(schema):
    system = build_loop_system(schema, SCHEMA_FULL)
    for name in LOOP_ACTIONS:
        assert f"ACTION: {name}" in system
    for name in EXCLUDED_ACTIONS:
        assert f"ACTION: {name}" not in system, f"{name} is excluded but described"


def test_ac1_the_retired_action_is_absent_from_the_rendered_prompt(schema):
    """Absent, not merely undescribed — in both schema modes."""
    for mode in SCHEMA_MODES:
        assert "sample_rows" not in build_loop_system(schema, mode)


def test_ac1_the_measured_saving_is_19_tokens(schema):
    """**The number in spec AC1, asserted with a real tokenizer.**

    This test exists because the figure AC1 originally carried — 22 — was not a
    measurement. It was `len(text) // 4`, and it went unchallenged into the
    spec, the plan, and a task report. Counting the prompt for real is three
    lines, so there was never a reason to estimate it.

    `o200k_base` is the encoding of the deployed `openai/gpt-oss-*` family.
    Pinned as an exact equality rather than a bound: a *drift* in this number
    is the signal worth catching, and "fewer than 25" would have accepted the
    wrong value it replaced.
    """
    tiktoken = pytest.importorskip("tiktoken")
    encoding = tiktoken.get_encoding("o200k_base")

    def count(text: str) -> int:
        return len(encoding.encode(text))

    retired = (
        "ACTION: sample_rows\n"
        "    <relation name> Returns a few example rows from that relation."
    )
    with_retired = render_action_list(LOOP_ACTIONS) + "\n\n" + retired

    assert count(with_retired) - count(render_action_list(LOOP_ACTIONS)) == 19

    # Explicitly glossary-off *and* explicitly DDL. T3 made the glossary the
    # default and T7 made `compact` the default, so an unqualified call now
    # measures the 916-token deployment prompt -- a different quantity from
    # the 925 this line was written to pin. The pin is unchanged; what it is
    # taken over is stated rather than inherited from a default.
    assert count(build_loop_system(schema, SCHEMA_FULL, SCHEMA_DDL, glossary=False)) == 925

    # The other three corners of the same 2x2, measured at T2, T3 and T7.
    # Pinned together so a change to any default shows up as a moved number
    # rather than as a test that quietly measures something else.
    assert count(build_loop_system(schema, SCHEMA_FULL, SCHEMA_COMPACT, glossary=False)) == 737
    assert count(build_loop_system(schema, SCHEMA_FULL, SCHEMA_DDL, glossary=True)) == 1104
    assert count(build_loop_system(schema, SCHEMA_FULL, SCHEMA_COMPACT, glossary=True)) == 916


def test_full_mode_includes_the_schema(schema):
    """Asserted in both renderings, because "the schema is in there" is the
    contract and DDL keywords are only how one of them says it."""
    ddl = build_loop_system(schema, SCHEMA_FULL, SCHEMA_DDL)
    assert "CREATE TABLE track" in ddl
    assert "FOREIGN KEY" in ddl

    default = build_loop_system(schema, SCHEMA_FULL)
    assert "track(" in default
    assert "FK[" in default


def test_withheld_mode_omits_the_schema_and_says_so(schema):
    """The secondary benchmark of resolved Q-A. The note is what turns a
    hopeless guess into a `get_schema` call — measured at 10 of 12."""
    system = build_loop_system(schema, SCHEMA_WITHHELD)
    assert "CREATE TABLE track" not in system
    assert "NOT been shown the schema" in system
    assert "get_schema" in system


def test_withheld_mode_ignores_a_schema_it_was_handed(schema):
    """Passing a schema must not leak it into a withheld run, or the secondary
    benchmark would quietly measure the primary one."""
    assert "CREATE TABLE" not in build_loop_system(schema, SCHEMA_WITHHELD)


def test_a_missing_schema_is_treated_as_withheld():
    assert "NOT been shown the schema" in build_loop_system(None, SCHEMA_FULL)


def test_an_unknown_schema_mode_is_refused(schema):
    with pytest.raises(ValueError, match="unknown schema mode"):
        build_loop_system(schema, "partial")


def test_the_modes_are_the_two_the_harness_uses():
    assert SCHEMA_MODES == ("full", "withheld")


def test_the_loop_template_forbids_writes():
    """The same instruction the single-shot template carries. Not a safety
    control — Gate 2 is — but there is no reason to invite the failure."""
    for keyword in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "GRANT"):
        assert keyword in LOOP_SYSTEM_TEMPLATE


def test_the_single_shot_template_is_untouched():
    """AC1. The Iteration 3 baseline has to stay reproducible, and it is
    reproducible only if the prompt it was measured against still exists."""
    assert SYSTEM_TEMPLATE.startswith("You are a PostgreSQL query generator.")
    assert "ACTION:" not in SYSTEM_TEMPLATE


# --- Q-C: the transcript ----------------------------------------------------


def test_the_transcript_opens_with_the_question():
    assert render_transcript("How many tracks?", [], 3).startswith(
        "Question: How many tracks?"
    )


def test_a_failed_attempt_is_rendered_with_its_sql_and_reason():
    step = FakeStep(
        attempt=1,
        action="execute_sql",
        sql="SELECT count(*) FROM Tracks",
        observation='Failed (database_error): relation "tracks" does not exist',
    )
    rendered = render_transcript("How many tracks?", [step], 2)

    assert "Attempt 1:" in rendered
    assert "ACTION: execute_sql" in rendered
    assert "SELECT count(*) FROM Tracks" in rendered
    assert 'relation "tracks" does not exist' in rendered


def test_the_verbatim_reason_survives_rendering():
    """`002` and `003` both spent their design budget making these messages
    actionable. Summarising them here would throw that away."""
    reason = (
        'column "nope" does not exist\n'
        "Hint: Perhaps you meant to reference the column \"track.name\"."
    )
    step = FakeStep(1, "execute_sql", "SELECT nope FROM track", reason)
    assert reason in render_transcript("q", [step], 1)


def test_several_attempts_render_in_order():
    steps = [
        FakeStep(1, "get_schema", "", "CREATE TABLE track (...)"),
        FakeStep(2, "execute_sql", "SELECT 1", "Failed (rejected): nope"),
    ]
    rendered = render_transcript("q", steps, 1)
    assert rendered.index("Attempt 1:") < rendered.index("Attempt 2:")


def test_an_action_with_no_sql_omits_the_sql_line():
    rendered = render_transcript(
        "q", [FakeStep(1, "get_schema", "", "CREATE TABLE track (...)")], 2
    )
    assert "ACTION: get_schema" in rendered
    assert "\n\n\n" not in rendered, "no empty gap where the SQL would be"


def test_rendering_is_deterministic():
    steps = [FakeStep(1, "execute_sql", "SELECT 1", "Failed (rejected): nope")]
    assert render_transcript("q", steps, 2) == render_transcript("q", steps, 2)


# --- D-2: the remaining budget is a fact, not a command ---------------------


def test_the_remaining_budget_is_stated():
    """Resolved D-2. In the withheld harness a schema lookup costs one of three
    calls, and a model that does not know its budget can spend the last one
    exploring."""
    assert "Attempts remaining: 2" in render_transcript("q", [], 2)
    assert "Attempts remaining: 1" in render_transcript("q", [], 1)


def test_the_remaining_budget_is_not_phrased_as_pressure():
    """A plain fact, deliberately. Pressure wording is prompt tuning and that is
    Iteration 5's — and this is the same file where AC13 refuses to lean on the
    model changing its mind."""
    rendered = render_transcript("q", [], 1).lower()
    for pushy in ("you must", "hurry", "final chance", "last chance", "!"):
        assert pushy not in rendered


# The two AC16 framing tests were deleted at Iteration 5 T1, with
# `frame_sample_rows` itself. They asserted that sampled database rows were
# labelled as DATA VALUES before reaching the model -- a real defence, but
# one that only meant something while an action existed to produce such
# rows. With that action retired, the tests would have asserted that a pure
# string function still formats a string.
#
# **The threat did not go away; the surface did.** If row inspection ever
# returns, the frame and these two tests must return with it -- restoring
# the capability alone would put untrusted database content back into the
# prompt with nothing marking it as data. See `specs/004-sample-rows.md`.


def test_row_rendering_shows_columns_and_values():
    rendered = render_rows(("a", "b"), ((1, "x"), (2, "y")))
    assert rendered.splitlines()[0] == "a | b"
    assert "1 | x" in rendered


def test_null_renders_distinguishably():
    """`NULL` and the empty string are different answers, and a renderer that
    showed both as nothing would teach the model they are the same."""
    assert "NULL" in render_rows(("a",), ((None,),))
    assert render_rows(("a",), ((None,),)) != render_rows(("a",), (("",),))


def test_an_empty_result_says_so():
    assert "(no rows)" in render_rows(("a",), ())


def test_an_oversized_cell_is_truncated_and_marked():
    """One unbounded value could crowd the schema out of the context window.
    Marking it means the model is never told a value is complete when it is
    not."""
    rendered = render_rows(("a",), (("x" * (MAX_CELL_WIDTH + 50),),))
    assert "(truncated)" in rendered
    assert len(rendered) < MAX_CELL_WIDTH + 100
