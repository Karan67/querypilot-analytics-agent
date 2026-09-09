"""Iteration 6 T4 — every failure category reaches the caller as something.

`test_every_category_in_the_project_is_accounted_for` is the one that matters,
and it is written against the trap `HANDOFF.md` §6 records twice over:

- **It discovers modules rather than listing them.** The plan said "eleven
  categories across three modules" and that was wrong in both numbers: there
  are twelve constants across four, because `api/db/sampling.py` still defines
  `unknown_relation`. A hand-maintained module list would have reproduced the
  plan's own blind spot.
- **It exempts a category, never a module.** `RETRY_POLICY`'s completeness test
  excludes `api/db/sampling.py` wholesale, which means anything else added to
  that file would be invisible to it. The first category that check ever missed
  was one added in the very file it was testing.
- **It reads values at runtime, not source text.** A structural test that greps
  will match its own docstring; this project has written that bug twice.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import api
from api.agent.orchestrator import CATEGORY_BUDGET_EXHAUSTED
from api.agent.single_shot import CATEGORY_NO_SQL, CATEGORY_RATE_LIMITED
from api.db.execution import (
    CATEGORY_CONNECTION_ERROR,
    CATEGORY_GATE_VIOLATION,
    CATEGORY_REJECTED,
)
from api.db.sampling import CATEGORY_UNKNOWN_RELATION
from api.http.errors import (
    RESPONSES,
    STATUS_ANSWERED,
    STATUS_UNAVAILABLE,
    UNREACHABLE,
    failure_for,
)

#: Modules the host test suite cannot import. **Empty, and that is the point.**
#:
#: At T4 this held `api.main`: FastAPI was a runtime-only dependency, so the
#: discovery walk below was blind to any category defined in the API module and
#: the completeness guarantee had a hole in it. Rather than tolerate that
#: silently, the hole was pinned as an exact set.
#:
#: T5 added `fastapi` to `requirements-dev.txt` and the pin failed — which is
#: precisely what a pinned gap is for. It closed, the assertion noticed, and the
#: set is now empty. Every module under `api/` is imported and walked.
#:
#: Leave it empty. A module added here is a module whose categories nothing
#: checks, and it needs the same justification the original entry carried.
KNOWN_UNIMPORTABLE: frozenset[str] = frozenset()


def _discover_categories() -> tuple[dict[str, set[str]], set[str]]:
    """Every `CATEGORY_*` constant defined anywhere under `api/`.

    Returns the values found, mapped to the modules defining them, and the
    modules that could not be imported.
    """
    found: dict[str, set[str]] = {}
    unimportable: set[str] = set()
    for info in pkgutil.walk_packages(api.__path__, prefix="api."):
        try:
            module = importlib.import_module(info.name)
        except ImportError:
            unimportable.add(info.name)
            continue
        for name, value in vars(module).items():
            if name.startswith("CATEGORY_") and isinstance(value, str):
                found.setdefault(value, set()).add(info.name)
    return found, unimportable


def test_every_category_in_the_project_is_accounted_for():
    """Mapped, or explicitly declared unreachable with a reason. Never neither."""
    found, unimportable = _discover_categories()

    assert unimportable == KNOWN_UNIMPORTABLE, (
        "the set of modules the host suite cannot import has changed, so the "
        "completeness walk may be blind to categories defined in them"
    )

    unaccounted = {
        value: sorted(modules)
        for value, modules in found.items()
        if value not in RESPONSES and value not in UNREACHABLE
    }
    assert not unaccounted, (
        f"categories with no HTTP response and no unreachable declaration: "
        f"{unaccounted}"
    )


def test_the_walk_actually_finds_something():
    """Guards the completeness test against passing vacuously.

    If `walk_packages` silently returned nothing -- a renamed package, a
    changed `__path__` -- the assertion above would pass over an empty set and
    guarantee nothing at all.
    """
    found, _ = _discover_categories()
    assert len(found) >= 12
    assert CATEGORY_REJECTED in found
    assert CATEGORY_UNKNOWN_RELATION in found


def test_sampling_is_walked_rather_than_skipped():
    """The distinction this suite exists to preserve.

    `unknown_relation` is exempted **as a category**, so `api/db/sampling.py`
    is still walked. Were a new, reachable category added to that module, the
    completeness test would catch it -- which is not true of `RETRY_POLICY`'s
    approach of excluding the file.
    """
    found, _ = _discover_categories()
    assert "api.db.sampling" in found[CATEGORY_UNKNOWN_RELATION]
    assert CATEGORY_UNKNOWN_RELATION in UNREACHABLE
    assert CATEGORY_UNKNOWN_RELATION not in RESPONSES


def test_nothing_is_both_mapped_and_unreachable():
    assert not (set(RESPONSES) & UNREACHABLE)


# --- D-2, the status split ---------------------------------------------------


def test_a_failed_question_is_not_an_http_error():
    """D-2. The request was understood and processed; the answer is negative."""
    assert failure_for(CATEGORY_NO_SQL).status == STATUS_ANSWERED
    assert failure_for(CATEGORY_BUDGET_EXHAUSTED).status == STATUS_ANSWERED
    assert failure_for(CATEGORY_REJECTED).status == STATUS_ANSWERED


def test_the_service_failing_is_an_http_error():
    assert failure_for(CATEGORY_CONNECTION_ERROR).status == STATUS_UNAVAILABLE


def test_a_rate_limit_is_reported_as_the_service_being_unavailable():
    """The question was answerable; the service could not try.

    Reporting it as a negative answer would be a lie about the data, which is
    why it does not follow the D-2 default.
    """
    assert failure_for(CATEGORY_RATE_LIMITED).status == STATUS_UNAVAILABLE
    assert failure_for(CATEGORY_RATE_LIMITED).retryable


# --- B-1: a billing condition must not read as a security event --------------


def test_a_rate_limit_never_reads_as_a_rejection():
    """`B-1` in the charter: for two iterations a rate-limited run failed red as
    a prompt-injection alarm. The category is distinct; the wording must be too.
    """
    message = failure_for(CATEGORY_RATE_LIMITED).message.lower()
    for alarming in ("reject", "block", "denied", "injection", "unsafe", "forbidden"):
        assert alarming not in message, f"{alarming!r} appears in a quota message"
    assert "limit" in message


def test_safety_wording_appears_only_in_the_safety_categories():
    """Safety vocabulary should identify Gate 2 and the read-only guard, and
    nothing else -- otherwise the words stop carrying information and a quota
    or a timeout starts sounding like an attack.

    Asserted as containment rather than equality: the invariant is that no
    *other* category borrows this language, not that both safety categories use
    the same verb. The first version of this test required the exact word
    "blocked" in both and failed on a message that said "stopped by the
    read-only guard", which is a wording preference masquerading as a rule.
    """
    safety_words = ("blocked", "stopped by", "safety", "read-only guard")
    for category, failure in RESPONSES.items():
        lowered = failure.message.lower()
        if any(word in lowered for word in safety_words):
            assert category in (CATEGORY_REJECTED, CATEGORY_GATE_VIOLATION), (
                f"{category} borrows safety wording: {failure.message!r}"
            )

    # ... and both safety categories do say *something* that identifies them.
    for category in (CATEGORY_REJECTED, CATEGORY_GATE_VIOLATION):
        lowered = failure_for(category).message.lower()
        assert any(word in lowered for word in safety_words), category


def test_no_message_leaks_sql_or_internals():
    for category, failure in RESPONSES.items():
        lowered = failure.message.lower()
        for leak in ("select ", "sqlstate", "traceback", "groq", "postgres"):
            assert leak not in lowered, f"{category}: {leak!r} leaks into the UI"


def test_every_message_is_a_sentence_a_non_technical_reader_can_act_on():
    for category, failure in RESPONSES.items():
        assert failure.message.strip(), category
        assert failure.message.strip()[0].isupper(), category
        assert failure.message.strip().endswith("."), category


# --- the refusal to invent a response ----------------------------------------


def test_an_unmapped_category_raises_rather_than_guessing():
    """A generic fallback would let a category added upstream reach users as
    "something went wrong" with nothing ever noticing."""
    with pytest.raises(KeyError, match="no HTTP response is mapped"):
        failure_for("a_category_that_does_not_exist")


def test_the_unreachable_category_has_no_response():
    """Asking for one is a programming error, not a user-facing state."""
    with pytest.raises(KeyError):
        failure_for(CATEGORY_UNKNOWN_RELATION)
