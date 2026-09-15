"""Failure category to HTTP response — every category, mapped explicitly.

`AgentResult.category` says what went wrong in terms the *agent* understands.
This module says what the caller should be told and with which status code, and
it is the only place that translation happens.

**Two rules shape the table below.**

*Resolved D-2: a question the agent could not answer is not an HTTP error.* The
request was received, parsed and processed; the answer is simply negative. Those
categories return **200 with `ok: false`** and a category the UI can render.
`503` is reserved for the service failing rather than the question failing.

*A billing condition must never wear a security-shaped message.* `B-1` records
what that cost: `rate_limited` was split into its own category, a test that
substring-matched a message silently stopped guarding, and for two iterations a
rate-limited run failed red as *"prompt injection produced executable DDL"*.
The word "rejected" belongs to `Gate 2` and appears nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.agent.orchestrator import (
    CATEGORY_BUDGET_EXHAUSTED,
    CATEGORY_REPEATED_SQL,
    CATEGORY_UNKNOWN_ACTION,
)
from api.agent.single_shot import (
    CATEGORY_NO_SQL,
    CATEGORY_PROVIDER_ERROR,
    CATEGORY_RATE_LIMITED,
)
from api.db.execution import (
    CATEGORY_CONNECTION_ERROR,
    CATEGORY_DATABASE_ERROR,
    CATEGORY_GATE_VIOLATION,
    CATEGORY_REJECTED,
    CATEGORY_TIMEOUT,
)
from api.db.sampling import CATEGORY_UNKNOWN_RELATION
from api.store.history import (
    CATEGORY_GLOBAL_DAILY_LIMIT,
    CATEGORY_IDENTITY_DAILY_LIMIT,
    CATEGORY_LEDGER_UNAVAILABLE,
)

#: `200`. The service worked; the answer is negative.
STATUS_ANSWERED = 200

#: `503`. The service could not try. Distinct from a negative answer because
#: telling a user "no results" when the provider was unreachable would be a
#: lie about the data.
STATUS_UNAVAILABLE = 503

#: `429`. Iteration 12 T9. Unlike `CATEGORY_RATE_LIMITED` above -- which is
#: this *service's* upstream allowance running out, deliberately kept off 429
#: so as not to blame the caller for it -- a daily ceiling breach genuinely is
#: the caller being throttled: this deployment's own accounting, of this
#: identity's own questions. 429 is the correct word for it in a way it was not
#: for the provider's limit.
STATUS_CALLER_THROTTLED = 429


@dataclass(frozen=True)
class Failure:
    """What to tell the caller about one failed question."""

    status: int
    #: Plain, non-alarming, and written for someone who does not know what a
    #: schema is. It never contains SQL, a stack trace, or a provider name.
    message: str
    #: Whether asking again, unchanged, could plausibly succeed. Drives whether
    #: the UI offers a retry, nothing more.
    retryable: bool


#: Every category a caller can actually reach, and what it becomes.
RESPONSES: dict[str, Failure] = {
    # --- the question failed, the service did not (D-2: 200) ---------------
    CATEGORY_REJECTED: Failure(
        STATUS_ANSWERED,
        "That query was blocked by the safety check, which only allows "
        "read-only queries.",
        retryable=False,
    ),
    CATEGORY_DATABASE_ERROR: Failure(
        STATUS_ANSWERED,
        "The database rejected the query that was written for this question.",
        retryable=True,
    ),
    CATEGORY_TIMEOUT: Failure(
        STATUS_ANSWERED,
        "That question took too long to answer. A narrower question will "
        "usually work.",
        retryable=False,
    ),
    CATEGORY_GATE_VIOLATION: Failure(
        STATUS_ANSWERED,
        "That query was stopped by the read-only guard before it ran.",
        retryable=False,
    ),
    CATEGORY_BUDGET_EXHAUSTED: Failure(
        STATUS_ANSWERED,
        "This question was attempted several times without a working query.",
        retryable=True,
    ),
    CATEGORY_REPEATED_SQL: Failure(
        STATUS_ANSWERED,
        "This question could not be answered -- the same query was produced "
        "twice, so retrying would not have helped.",
        retryable=False,
    ),
    CATEGORY_UNKNOWN_ACTION: Failure(
        STATUS_ANSWERED,
        "The answer came back in a form this system could not read.",
        retryable=True,
    ),
    CATEGORY_NO_SQL: Failure(
        STATUS_ANSWERED,
        "No query was produced for that question. Rephrasing it usually helps.",
        retryable=True,
    ),
    # --- the service failed, not the question (D-2: 503) -------------------
    CATEGORY_CONNECTION_ERROR: Failure(
        STATUS_UNAVAILABLE,
        "The database is not reachable right now.",
        retryable=True,
    ),
    CATEGORY_PROVIDER_ERROR: Failure(
        STATUS_UNAVAILABLE,
        "The language model is not reachable right now.",
        retryable=True,
    ),
    #: **`503`, not `200`, and deliberately so.** The question was answerable
    #: and the service simply could not try, so reporting it as a negative
    #: answer would be a lie about the data. `429` was rejected because it
    #: describes *the caller* being throttled, and it is this service's upstream
    #: allowance that ran out, not the user's.
    #:
    #: The message names a quota and a wait. It does not say "rejected",
    #: "blocked" or "denied" -- see this module's docstring, and `B-1`.
    CATEGORY_RATE_LIMITED: Failure(
        STATUS_UNAVAILABLE,
        "The daily or per-minute usage limit for the language model has been "
        "reached. This clears on its own -- try again shortly.",
        retryable=True,
    ),
    # --- Iteration 12 T9: the daily spend ceiling ---------------------------
    #
    # Neither of these is an answer failure -- the agent is never invoked, so
    # there is no `sql`, no trace, and nothing `record_ask` writes a row about.
    # They are refused at the same point in the request `auth.py`'s gate is,
    # for the reason `_unauthorized()` gives for keeping a 401 out of this
    # table: the caller has not asked a question yet. They are mapped here
    # anyway (AC20) so `test_every_category_in_the_project_is_accounted_for`
    # covers them, and so the message a caller sees still comes from one place.
    CATEGORY_IDENTITY_DAILY_LIMIT: Failure(
        STATUS_CALLER_THROTTLED,
        "This identity has reached its daily question limit. It resets at "
        "midnight UTC.",
        retryable=True,
    ),
    CATEGORY_GLOBAL_DAILY_LIMIT: Failure(
        STATUS_CALLER_THROTTLED,
        "This deployment has reached its daily question limit across every "
        "identity. It resets at midnight UTC.",
        retryable=True,
    ),
    #: `503`, on the same reasoning as `CATEGORY_CONNECTION_ERROR`: the ledger
    #: could not be read, so there is no evidence the ceiling was not exceeded,
    #: and reporting the question as answerable would be a claim this service
    #: cannot back up.
    CATEGORY_LEDGER_UNAVAILABLE: Failure(
        STATUS_UNAVAILABLE,
        "The daily spend ceiling could not be checked right now.",
        retryable=True,
    ),
}

#: Categories that exist as constants but **cannot be produced**, with the
#: reason. Kept as a named set rather than by skipping a module, so the
#: completeness check below can still walk every module in `api/` and a new
#: category anywhere fails loudly.
#:
#: `unknown_relation` belonged to `sample_rows`, which Iteration 5 T1 retired
#: along with the only action that could raise it. `api/db/sampling.py` still
#: defines the constant. `RETRY_POLICY` handles this by excluding that module
#: from its own completeness check; excluding a *module* hides anything else
#: added to it later, so this names the *category* instead.
#:
#: Mapping it anyway was the alternative and was rejected: it would put a
#: user-facing sentence in the table for a state no user can reach, which is
#: the "false statement kept alive to satisfy a test" that `RETRY_POLICY`'s
#: own comment warns against.
UNREACHABLE: frozenset[str] = frozenset({CATEGORY_UNKNOWN_RELATION})


def failure_for(category: str) -> Failure:
    """The response for a category, refusing to invent one it does not know.

    Falling back to a generic 500 here would mean a category added upstream
    reaches users as "something went wrong" and nothing ever notices. The
    completeness test is what keeps this from raising in practice.
    """
    try:
        return RESPONSES[category]
    except KeyError:
        raise KeyError(
            f"no HTTP response is mapped for category {category!r}; add it to "
            f"api/http/errors.py RESPONSES, or to UNREACHABLE with the reason"
        ) from None
