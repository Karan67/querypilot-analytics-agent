"""The daily spend ceiling (Iteration 12 T9, AC18 AC19 AC20).

Hermetic. `history.reserve_question` and `history.spend_status` take a `path`
argument for exactly this reason, on the same terms as `record_ask`: a test
redirects it to a temp file rather than touching `/data`.

This reverses a decision. `specs/010-hardening.md` deliberately rejected an
inbound limiter, reasoned against a localhost service with one authenticated
operator. B-11's first blocker -- *"a public URL in front of that key is a
denial-of-service surface with a bill attached"* -- is a different premise, and
Iteration 10's authentication answered *who* may spend without touching *how
much*. This closes that gap. The reversal is recorded here rather than done
quietly: `010`'s reasoning was sound for what it was reasoning about, and
stopped applying the moment a public URL became the subject.
"""

from __future__ import annotations

import pathlib
import sqlite3
from datetime import datetime, timezone

import pytest

from api.http import errors
from api.store import history

UTC_DAY = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
NEXT_DAY = datetime(2026, 9, 16, 0, 30, tzinfo=timezone.utc)


@pytest.fixture
def ledger(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "ceiling.db"


def _reserve(ledger, identity="alice", *, now=UTC_DAY):
    return history.reserve_question(identity, ledger, now=now)


# --- the basic shape ----------------------------------------------------------


def test_a_first_question_is_allowed(ledger: pathlib.Path) -> None:
    decision = _reserve(ledger)
    assert decision.allowed is True
    assert decision.identity_count == 1
    assert decision.global_count == 1
    assert decision.category == ""


def test_counts_accumulate_across_calls(ledger: pathlib.Path) -> None:
    for _ in range(3):
        decision = _reserve(ledger)
    assert decision.identity_count == 3
    assert decision.global_count == 3


# --- AC18/AC19: the two ceilings, and their defaults ---------------------------


def test_the_identity_ceiling_refuses_the_fifty_first_question(
    ledger: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(history.IDENTITY_LIMIT_ENV, "3")
    monkeypatch.setenv(history.GLOBAL_LIMIT_ENV, "1000")
    for _ in range(3):
        assert _reserve(ledger).allowed is True
    refused = _reserve(ledger)
    assert refused.allowed is False
    assert refused.category == history.CATEGORY_IDENTITY_DAILY_LIMIT


def test_the_global_ceiling_refuses_even_a_caller_under_their_own_limit(
    ledger: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three identities at their own limit reach the global one first.

    This is the risk the spec's §6 names explicitly: the caller who happens to
    ask last is refused for a reason that is not their own doing, and the
    category on the result is what keeps that legible rather than looking like
    a bug.
    """
    monkeypatch.setenv(history.IDENTITY_LIMIT_ENV, "50")
    monkeypatch.setenv(history.GLOBAL_LIMIT_ENV, "2")
    assert _reserve(ledger, "alice").allowed is True
    assert _reserve(ledger, "bob").allowed is True
    refused = _reserve(ledger, "carol")
    assert refused.allowed is False
    assert refused.category == history.CATEGORY_GLOBAL_DAILY_LIMIT
    # And crucially: carol's own count never reached her identity limit.
    assert refused.identity_count == 0


def test_identity_is_checked_before_global(
    ledger: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller is told 'you' before being told 'everyone'.

    If the same request would breach both ceilings, the identity category
    fires -- so nobody is told the deployment is out of quota when the truth
    is narrower and more actionable: they specifically are.
    """
    monkeypatch.setenv(history.IDENTITY_LIMIT_ENV, "1")
    monkeypatch.setenv(history.GLOBAL_LIMIT_ENV, "1")
    assert _reserve(ledger, "alice").allowed is True
    refused = _reserve(ledger, "alice")
    assert refused.category == history.CATEGORY_IDENTITY_DAILY_LIMIT


def test_the_defaults_are_fifty_and_one_hundred_fifty() -> None:
    """Spec section 7 Q-G, against the key's measured ~180 questions/day."""
    assert history.DEFAULT_IDENTITY_LIMIT == 50
    assert history.DEFAULT_GLOBAL_LIMIT == 150


def test_limits_are_configuration(
    ledger: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(history.IDENTITY_LIMIT_ENV, "2")
    assert _reserve(ledger).allowed is True
    assert _reserve(ledger).allowed is True
    assert _reserve(ledger).allowed is False


def test_an_unset_limit_falls_back_to_the_default(
    ledger: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(history.IDENTITY_LIMIT_ENV, raising=False)
    monkeypatch.delenv(history.GLOBAL_LIMIT_ENV, raising=False)
    decision = _reserve(ledger)
    assert decision.identity_limit == history.DEFAULT_IDENTITY_LIMIT
    assert decision.global_limit == history.DEFAULT_GLOBAL_LIMIT


@pytest.mark.parametrize("garbage", ["not-a-number", "0", "-5", "  "])
def test_a_nonsensical_limit_falls_back_to_the_default(
    ledger: pathlib.Path, monkeypatch: pytest.MonkeyPatch, garbage: str
) -> None:
    """A limit of zero or less would refuse every question, silently, which is
    indistinguishable from the service being down."""
    monkeypatch.setenv(history.IDENTITY_LIMIT_ENV, garbage)
    assert _reserve(ledger).identity_limit == history.DEFAULT_IDENTITY_LIMIT


# --- a refusal costs the caller nothing ----------------------------------------


def test_a_refused_question_is_not_counted(
    ledger: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing property. Counting a refusal would let a caller near
    the limit tip over it purely by retrying to see the message again."""
    monkeypatch.setenv(history.IDENTITY_LIMIT_ENV, "1")
    monkeypatch.setenv(history.GLOBAL_LIMIT_ENV, "1000")
    assert _reserve(ledger).allowed is True
    for _ in range(5):
        refused = _reserve(ledger)
        assert refused.allowed is False
        assert refused.identity_count == 1, "a refusal must not increment the count"


# --- the day boundary -----------------------------------------------------------


def test_a_new_utc_day_resets_the_counter(
    ledger: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(history.IDENTITY_LIMIT_ENV, "1")
    monkeypatch.setenv(history.GLOBAL_LIMIT_ENV, "1000")
    assert _reserve(ledger, now=UTC_DAY).allowed is True
    assert _reserve(ledger, now=UTC_DAY).allowed is False
    assert _reserve(ledger, now=NEXT_DAY).allowed is True


def test_yesterdays_rows_are_never_touched_again(
    ledger: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(history.GLOBAL_LIMIT_ENV, "1000")
    _reserve(ledger, now=UTC_DAY)
    _reserve(ledger, now=NEXT_DAY)
    with sqlite3.connect(ledger) as conn:
        rows = conn.execute(
            "SELECT usage_date, question_count FROM daily_usage ORDER BY usage_date"
        ).fetchall()
    assert rows == [("2026-09-15", 1), ("2026-09-16", 1)]


# --- AC20: the categories are mapped, and mapped distinctly --------------------


def test_the_identity_category_is_a_429_with_its_own_message() -> None:
    failure = errors.failure_for(history.CATEGORY_IDENTITY_DAILY_LIMIT)
    assert failure.status == errors.STATUS_CALLER_THROTTLED
    assert "identity" in failure.message.lower()


def test_the_global_category_is_a_429_with_a_different_message() -> None:
    """The risk the spec names: two categories reading identically would look
    like a bug the first time the 'wrong' one fired."""
    identity_failure = errors.failure_for(history.CATEGORY_IDENTITY_DAILY_LIMIT)
    global_failure = errors.failure_for(history.CATEGORY_GLOBAL_DAILY_LIMIT)
    assert global_failure.status == errors.STATUS_CALLER_THROTTLED
    assert global_failure.message != identity_failure.message
    assert "deployment" in global_failure.message.lower()


def test_a_ledger_failure_is_a_503_not_a_429() -> None:
    """Not the caller's fault, and not evidence the ceiling was not exceeded --
    D-2's reasoning for an unreachable database, applied here."""
    failure = errors.failure_for(history.CATEGORY_LEDGER_UNAVAILABLE)
    assert failure.status == errors.STATUS_UNAVAILABLE


def test_a_broken_ledger_path_refuses_rather_than_allows(
    tmp_path: pathlib.Path,
) -> None:
    """The fail-closed half. A directory where the database file is expected
    can never be opened by sqlite3, which is the cheapest real failure to
    induce."""
    broken = tmp_path / "ceiling.db"
    broken.mkdir()
    decision = history.reserve_question("alice", broken)
    assert decision.allowed is False
    assert decision.category == history.CATEGORY_LEDGER_UNAVAILABLE


# --- /health, read-only ---------------------------------------------------------


def test_spend_status_reports_without_writing(
    ledger: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(history.GLOBAL_LIMIT_ENV, "150")
    _reserve(ledger)
    _reserve(ledger)
    before = history.spend_status(ledger, now=UTC_DAY)
    after = history.spend_status(ledger, now=UTC_DAY)
    assert before == after == {
        "available": True,
        "global_count": 2,
        "global_limit": 150,
        "identity_limit": history.DEFAULT_IDENTITY_LIMIT,
        "error": "",
    }


def test_spend_status_on_a_broken_ledger_names_the_failure(
    tmp_path: pathlib.Path,
) -> None:
    broken = tmp_path / "ceiling.db"
    broken.mkdir()
    status = history.spend_status(broken)
    assert status["available"] is False
    assert status["error"]


# --- end to end, through /ask ---------------------------------------------------


def test_the_gate_refuses_before_the_agent_is_ever_invoked(
    authed_client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    provider_that_must_not_be_called,
) -> None:
    """AC18, at the boundary D-3 named: before cache lookups or LLM calls.

    `provider_that_must_not_be_called` raises if `complete()` is reached at
    all -- the strongest available proof that a refused question never reaches
    the agent. The one slot the identity limit allows is pre-consumed directly
    against the same ledger the app is pointed at, since a limit of `0` is
    treated as nonsensical configuration and falls back to the default (see
    `test_a_nonsensical_limit_falls_back_to_the_default`).
    """
    db_path = tmp_path / "history.db"
    monkeypatch.setenv("QUERYPILOT_HISTORY_PATH", str(db_path))
    monkeypatch.setenv(history.IDENTITY_LIMIT_ENV, "1")
    monkeypatch.setattr(
        "api.llm.factory.get_provider", lambda *a, **k: provider_that_must_not_be_called
    )
    assert history.reserve_question("test-analyst", db_path).allowed is True

    response = authed_client.post("/ask", json={"question": "anything"})
    assert response.status_code == 429
    body = response.json()
    assert body["ok"] is False
    assert body["category"] == history.CATEGORY_IDENTITY_DAILY_LIMIT
    assert body["sql"] == ""
    assert body["trace"] == []


def test_a_refused_question_writes_no_history_row(
    authed_client, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    db_path = tmp_path / "history.db"
    monkeypatch.setenv("QUERYPILOT_HISTORY_PATH", str(db_path))
    monkeypatch.setenv(history.IDENTITY_LIMIT_ENV, "1")
    assert history.reserve_question("test-analyst", db_path).allowed is True

    authed_client.post("/ask", json={"question": "anything"})
    assert history.recent(path=db_path) == []


def test_an_unauthenticated_probe_never_reaches_the_ledger(
    anonymous_client, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The 401 fires first. An anonymous caller has no identity to weigh a
    question against, and must never reach code that assumes one exists."""
    monkeypatch.setenv("QUERYPILOT_HISTORY_PATH", str(tmp_path / "history.db"))
    monkeypatch.setenv(history.IDENTITY_LIMIT_ENV, "1")
    response = anonymous_client.post("/ask", json={"question": "anything"})
    assert response.status_code == 401
    # And the ledger this identity would have touched stays untouched.
    assert not (tmp_path / "history.db").exists()
