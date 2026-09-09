"""What the provider last said about its limits, and whether to warn about it.

**The slow mode this exists for is invisible everywhere else.**
`010-hardening.md` §2.3 measured latency climbing fourteen-fold — 742ms to
10,393ms — with the work held constant at ~1,100 tokens a question. It is not
difficulty and it is not a refusal: there is no 429, no error, and no header a
user would ever see. The provider simply slows down as the per-minute bucket
drains, and until this module nothing in the product noticed. AC11 calls that a
silent penalty, which is exactly right.

**The snapshot has to be kept here rather than read off the provider**, and that
is a consequence of T4. The endpoint builds a provider per request and a cache
hit builds none at all, so `last_rate_limit` dies with the request that observed
it. This module is the one place that outlives them.

**Nothing here widens the provider protocol.** `complete(system, user) -> str`
stays one method (`008` D-1); the snapshot arrives through `getattr` on the
concrete provider and this module copes with `None`, exactly as `usage_for_call`
already does.

**And it reports two buckets rather than three, on purpose.** B-1 measured a
tokens-per-minute limit and a requests-per-day limit in the headers, and a
**tokens-per-day limit of 200,000 that appears in no header at all** — only in
the body of a 429. This endpoint cannot report what the provider never sends, so
it reports what it has and says nothing about the daily token budget. Inventing
a figure for it would repeat B-1's original error in the opposite direction:
that task's first conclusion was that the daily limit did not exist, because no
header mentioned it.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

#: What one question costs, measured rather than assumed.
#:
#: `010` §2.3 held twelve questions between 1,047 and 1,256 tokens, and three
#: live questions on 2026-09-09 cost 1,047, 1,077 and 1,134. The high end is
#: used because this drives a *warning*, and a warning that arrives late is
#: worth less than one that arrives early.
TYPICAL_QUESTION_TOKENS = 1256

#: Warn below two questions' worth of the minute bucket.
#:
#: **A threshold, not a cliff, and the difference is the honest part.** §2.3
#: measured latency degrading *progressively* as the bucket drains -- at 3,257
#: remaining a call still took 1,157ms -- so there is no measured point at which
#: answers become slow. Two questions' worth is the point past which the *next*
#: question plausibly does not fit, which is the soonest a warning can be made
#: on evidence rather than on feel.
LOW_TOKENS = 2 * TYPICAL_QUESTION_TOKENS

#: How long a reading stays meaningful when the provider gives no reset time.
#:
#: 60s is the measured window of the minute bucket (B-1 derives it from the
#: headers rather than assuming it). Past that the bucket has refilled and an
#: old reading describes a state that no longer exists.
DEFAULT_STALE_AFTER_SECONDS = 60.0

_lock = threading.Lock()
_snapshot = None
_observed_at: datetime | None = None


def observe(snapshot) -> None:
    """Remember the most recent rate-limit snapshot.

    A `None` snapshot is **ignored rather than stored**. It means this request
    learned nothing -- a cache hit, or a provider with no telemetry -- and the
    last real reading is still the most recent thing anybody knows. Overwriting
    it with an absence would turn "we have not looked lately" into "we looked
    and there was nothing", which are different claims.
    """
    global _snapshot, _observed_at
    if snapshot is None:
        return
    with _lock:
        _snapshot = snapshot
        _observed_at = datetime.now(timezone.utc)


def clear() -> None:
    """Forget it. For tests, and for the same reason every other module here
    grew one: shared state a test can reach will eventually be written by one."""
    global _snapshot, _observed_at
    with _lock:
        _snapshot = None
        _observed_at = None


def _bucket(bucket) -> dict | None:
    if bucket is None:
        return None
    return {
        "limit": bucket.limit,
        "remaining": bucket.remaining,
        "reset_seconds": bucket.reset_seconds,
        # Derived from the headers, never assumed, so it survives a provider
        # changing its tiers (B-1).
        "window": bucket.describe_window(),
    }


def snapshot(now: datetime | None = None) -> dict:
    """The current view of the provider's limits, as the endpoint returns it.

    `known` is `False` before any provider call has been made, and the payload
    then carries no numbers at all. **Absence is reported as absence**: a
    default of "8,000 remaining" would be a fabrication, and one that reads
    exactly like a healthy reading.
    """
    with _lock:
        current, observed_at = _snapshot, _observed_at

    if current is None or observed_at is None:
        return {
            "known": False,
            "observed_at": None,
            "age_seconds": None,
            "stale": False,
            "low": False,
            "note": "",
            "tokens": None,
            "requests": None,
        }

    now = now or datetime.now(timezone.utc)
    age = max(0.0, (now - observed_at).total_seconds())

    tokens = getattr(current, "tokens", None)
    remaining = getattr(tokens, "remaining", None)
    reset_seconds = getattr(tokens, "reset_seconds", None)

    # **The reading expires, and saying so is the point.** The minute bucket
    # refills in 60 seconds, so a warning drawn from a five-minute-old snapshot
    # would describe a bucket that is long since full. A stale reading is
    # reported as stale and never as low.
    horizon = reset_seconds if reset_seconds else DEFAULT_STALE_AFTER_SECONDS
    stale = age >= horizon

    low = bool(not stale and remaining is not None and remaining < LOW_TOKENS)

    return {
        "known": True,
        "observed_at": observed_at.isoformat(timespec="seconds"),
        "age_seconds": round(age, 1),
        "stale": stale,
        "low": low,
        "note": _note(low, remaining, getattr(tokens, "limit", None)),
        "tokens": _bucket(tokens),
        "requests": _bucket(getattr(current, "requests", None)),
    }


def _note(low: bool, remaining, limit) -> str:
    """The sentence the page shows, worded so it never promises.

    **"May" rather than "will", and it is not hedging.** §2.3 measured
    degradation as progressive with no threshold at which answers become slow,
    so a message saying the next answer *will* take ten seconds would be a claim
    the measurement does not support -- the same overreach `009` AC13 banned
    when it kept the accuracy number off this interface.

    It also says what is happening rather than only that something is. A user
    told "this may be slow" learns nothing; one told the provider's per-minute
    budget is nearly spent knows both why and that it recovers on its own.
    """
    if not low:
        return ""
    left = f"{remaining:,}" if remaining is not None else "very little"
    of = f" of {limit:,}" if limit else ""
    return (
        f"The model provider's per-minute token budget is nearly spent "
        f"({left}{of} left). Answers may take several seconds until it refills, "
        f"which happens within a minute."
    )
