"""Tests for single-shot generation — specs/005-single-shot-generation.md.

Tasks T4 and T5. **No network and no API key**: a fake provider drives every
path, which is what keeps the bulk of the suite hermetic (AC25). The live tests
live in `test_llm_live.py` and skip without a key.

The fake responses are not invented. Each is a shape measured from a real model:
clean SQL and a prose refusal from `openai/gpt-oss-120b`, an inline `<think>`
block from `qwen/qwen3.6-27b`, a fence from qwen once.
"""

from __future__ import annotations

import pytest

from api.agent.single_shot import (
    CATEGORY_NO_SQL,
    CATEGORY_PROVIDER_ERROR,
    AnswerResult,
    answer_question,
    extract_sql,
)
from api.llm.base import LLMError, LLMProvider


class FakeProvider:
    """Returns a canned response and counts calls."""

    def __init__(self, response: str = "SELECT 1", raises: Exception | None = None):
        self.response = response
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.raises is not None:
            raise self.raises
        return self.response


def test_fake_satisfies_the_protocol():
    assert isinstance(FakeProvider(), LLMProvider)


# --- T4: extraction (AC17-AC19) --------------------------------------------


def test_extracts_clean_sql():
    """The default model's measured behaviour: no fence, no inline trace."""
    assert extract_sql("SELECT COUNT(*) FROM track;") == "SELECT COUNT(*) FROM track;"


def test_strips_surrounding_whitespace():
    assert extract_sql("\n\n  SELECT 1  \n\n") == "SELECT 1"


@pytest.mark.parametrize(
    "response",
    [
        "```sql\nSELECT 1\n```",
        "```\nSELECT 1\n```",
        "```SQL\nSELECT 1\n```",
        "```postgresql\nSELECT 1\n```",
    ],
)
def test_ac17_strips_markdown_fences(response):
    assert extract_sql(response) == "SELECT 1"


def test_ac17_strips_inline_think_block():
    """The measured failure that mattered. `qwen/qwen3.6-27b` prefixes every
    response with one of these and scores 0/4 without stripping — not because
    its SQL is wrong, but because prose precedes it."""
    response = (
        "<think>\nThe user wants the number of tracks.\n"
        "I need to query the track table.\n</think>\n\n"
        "SELECT COUNT(*) FROM track;"
    )
    assert extract_sql(response) == "SELECT COUNT(*) FROM track;"


def test_ac17_strips_think_block_and_fence_together():
    response = "<think>reasoning</think>\n```sql\nSELECT 1\n```"
    assert extract_sql(response) == "SELECT 1"


def test_ac17_multiline_think_block_with_tags_inside():
    response = "<think>\nfirst\n\nsecond\n</think>SELECT 2"
    assert extract_sql(response) == "SELECT 2"


@pytest.mark.parametrize("response", ["", "   ", "\n\n", "<think>only reasoning</think>"])
def test_ac18_nothing_extractable_returns_empty(response):
    assert extract_sql(response) == ""


def test_ac18_non_string_returns_empty():
    for value in (None, 42, [], {"sql": "SELECT 1"}):
        assert extract_sql(value) == ""


def test_ac19_prose_is_returned_unchanged_not_searched():
    """**Deliberately does not hunt for a SELECT.**

    Extraction slices; it does not guess which part of the text is meant to run.
    Searching for the first `SELECT` keyword would be pattern-matching on model
    output to decide what executes — one step from the keyword-blocklist mistake
    `002` exists to avoid. Prose reaches Gate 2 and is rejected with a reason,
    which is the correct outcome.
    """
    refusal = "I'm sorry, but I can't help with that."
    assert extract_sql(refusal) == refusal

    prose_then_sql = "Here is the query you asked for:\nSELECT 1"
    assert extract_sql(prose_then_sql) == prose_then_sql


# --- T5: orchestration (AC16, AC20-AC25) -----------------------------------


def test_ac16_exactly_one_llm_call(configured_database):
    provider = FakeProvider("SELECT 1")
    answer_question("how many tracks?", provider=provider)
    assert len(provider.calls) == 1


def test_ac16_no_retry_on_rejection(configured_database):
    """A rejected query is **not** regenerated. Retry policy is Iteration 4's,
    and the rate at which rejection happens is a baseline number worth having
    before anything starts fixing it."""
    provider = FakeProvider("DROP TABLE track")
    result = answer_question("drop everything", provider=provider)
    assert len(provider.calls) == 1
    assert result.category == "rejected"


def test_ac16_no_retry_on_provider_failure(configured_database):
    provider = FakeProvider(raises=LLMError("rate limited"))
    result = answer_question("q", provider=provider)
    assert len(provider.calls) == 1
    assert result.category == CATEGORY_PROVIDER_ERROR


def test_successful_question_executes(configured_database):
    provider = FakeProvider("SELECT COUNT(*) AS n FROM track")
    result = answer_question("how many tracks?", provider=provider)
    assert result.ok is True
    assert result.result.rows == ((3503,),)
    assert result.category == "" and result.error == ""


def test_ac22_result_carries_question_and_sql(configured_database):
    provider = FakeProvider("SELECT 1")
    result = answer_question("my question", provider=provider)
    assert result.question == "my question"
    assert result.sql == "SELECT 1"
    assert result.result is not None


def test_ac21_rejection_keeps_the_validator_reason(configured_database):
    """The reason is exactly what Iteration 4 will feed back, so it must survive
    the trip unchanged."""
    from api.safety.validator import validate_sql

    sql = "WITH d AS (DELETE FROM track RETURNING 1 AS x) SELECT x FROM d"
    _, expected = validate_sql(sql)

    result = answer_question("q", provider=FakeProvider(sql))
    assert result.ok is False
    assert result.category == "rejected"
    assert result.error == expected
    assert result.sql == sql, "the rejected SQL must stay inspectable"


def test_ac18_refusal_becomes_no_sql_or_rejected(configured_database):
    """A refusal is the ordinary way a response contains no SQL — measured, not
    hypothetical. Either categorisation is defensible; what matters is that it
    is a categorised failure rather than a crash."""
    result = answer_question("q", provider=FakeProvider("I'm sorry, I can't."))
    assert result.ok is False
    assert result.category in {CATEGORY_NO_SQL, "rejected"}
    assert result.error


@pytest.mark.parametrize("response", ["", "   ", "<think>only thinking</think>"])
def test_ac18_empty_response_is_no_sql_returned(configured_database, response):
    result = answer_question("q", provider=FakeProvider(response))
    assert result.category == CATEGORY_NO_SQL
    assert result.result is None


def test_ac24_provider_failure_is_its_own_category(configured_database):
    """Distinct from a SQL failure: the agent's response to "the model is
    unreachable" differs from its response to "that column does not exist"."""
    result = answer_question("q", provider=FakeProvider(raises=LLMError("boom")))
    assert result.category == CATEGORY_PROVIDER_ERROR
    assert result.result is None


def test_ac23_never_raises_on_bad_input(configured_database):
    for question in (None, 42, "", "   ", []):
        result = answer_question(question, provider=FakeProvider("SELECT 1"))
        assert isinstance(result, AnswerResult)
        assert result.ok is False


def test_raw_response_captured_on_failure_only(configured_database):
    """Resolved Q-E: diagnostic on a failed eval case, noise otherwise."""
    ok_result = answer_question("q", provider=FakeProvider("SELECT 1"))
    assert ok_result.ok is True
    assert ok_result.raw_response == ""

    bad = answer_question("q", provider=FakeProvider("DROP TABLE track"))
    assert bad.raw_response == "DROP TABLE track"


def test_ac20_generated_sql_goes_through_execute_sql():
    """Structural. This module must not reach the engine, and must not call the
    validator itself to "pre-check" — one path, one gate ordering."""
    import ast
    import inspect

    from api.agent import single_shot

    source = inspect.getsource(single_shot)
    assert "execute_sql(sql)" in source

    # Identifiers via AST, not raw text. An earlier version grepped the source
    # and failed on this module's own docstring, which states that it never
    # calls validate_sql -- the same trap `003`'s bypass test hit. A test that
    # forbids *documenting* a design rule is the wrong test, twice over.
    tree = ast.parse(source)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            imported.update(alias.name for alias in node.names)

    assert "validate_sql" not in called, "must not pre-check; execute_sql owns the gate"
    assert "get_engine" not in called and "get_engine" not in imported
    assert "sqlalchemy" not in imported


def test_ac2_no_vendor_sdk_outside_the_llm_package():
    """**The test that keeps the swappability commitment honest.**

    `specs/000-project.md` §5 says provider code is reachable only through one
    abstraction. Three lines, and without them the promise is aspirational.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "api"
    offenders = []
    for path in root.rglob("*.py"):
        if "llm" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import groq", "from groq")):
                offenders.append(f"{path.relative_to(root)}: {stripped}")
    assert not offenders, f"vendor SDK imported outside api/llm/: {offenders}"


def test_single_shot_survives_the_arrival_of_the_loop():
    """`007` AC1, and the successor to this file's "orchestrator is still empty"
    guard.

    That guard held from Iteration 2 until Iteration 4 landed, and its purpose
    was to keep single-shot logic out of the loop's file so the loop would be a
    replacement rather than a rewrite. Now that `orchestrator.py` has content,
    the invariant worth protecting is the other direction: **single-shot must
    still work**, because the Iteration 3 baseline in `EVALS.md` is only
    reproducible while the code that produced it still runs.
    """
    import inspect

    from api.agent import single_shot

    assert callable(single_shot.answer_question)
    assert list(inspect.signature(single_shot.answer_question).parameters) == [
        "question",
        "provider",
    ]


def test_the_loop_is_a_replacement_not_a_wrapper():
    """The loop must not delegate to `answer_question`.

    If it did, the two strategies would share a code path and `--strategy`
    would compare a thing against itself — the delta would be measuring
    nothing.
    """
    import ast
    import pathlib

    tree = ast.parse(
        (
            pathlib.Path(__file__).resolve().parent.parent
            / "api" / "agent" / "orchestrator.py"
        ).read_text(encoding="utf-8")
    )
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "answer_question" not in imported
