"""Provider rate-limit telemetry — charter backlog B-1.

**Why this exists.** Iteration 5 lost two multi-pass held-out runs to rate
limits and could not say which limit it had hit. B-1 replaces that guess with
what the provider actually says.

**There are three limits, and only two are in the headers.** Measured against
Groq on 2026-09-04:

| limit | capacity | reported in |
|---|---|---|
| tokens per minute | 8,000 | headers |
| requests per day | 1,000 | headers |
| **tokens per day** | **200,000** | **only the 429 body** |

**The window is derived, never assumed.** For the two the headers describe,
a limit, what is left, and the time to reset give it away between them:

    refill_per_second = (limit - remaining) / seconds_until_reset
    window_seconds    = limit / refill_per_second

which produced 60.0s and 86400.0s to four significant figures on every sample.

**The correction that gave this module its shape.** The first version of it
stopped there and concluded that the project's 200,000-a-day figure was
unenforced, because no header mentioned it. That was wrong, and wrong in the
way this project exists to prevent: absence of a header was read as absence of
a limit. A refusal arrives with the per-minute bucket reading a full 8,000/8,000
and a body saying `on tokens per day (TPD): Limit 200000, Used 199301`. The
daily allowance was the binding constraint all along, and `limit_from_message`
exists so the next reader is told rather than left to infer.

**Nothing here is on the `LLMProvider` protocol.** `complete(system, user) -> str`
stays one method (`008` D-1). A concrete provider may grow a best-effort
`last_rate_limit` attribute; callers read it with `getattr` and cope with
`None`, exactly as they already do for `last_usage` and `model`.

**Nothing here is on the `LLMProvider` protocol.** `complete(system, user) -> str`
stays one method (`008` D-1). A concrete provider may grow a best-effort
`last_rate_limit` attribute; callers read it with `getattr` and cope with
`None`, exactly as they already do for `last_usage` and `model`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

#: Header names, lowercased. Whatever else arrives is kept in `raw` rather than
#: dropped -- the point of this module is to stop assuming what a provider sends.
LIMIT_TOKENS = "x-ratelimit-limit-tokens"
REMAINING_TOKENS = "x-ratelimit-remaining-tokens"
RESET_TOKENS = "x-ratelimit-reset-tokens"
LIMIT_REQUESTS = "x-ratelimit-limit-requests"
REMAINING_REQUESTS = "x-ratelimit-remaining-requests"
RESET_REQUESTS = "x-ratelimit-reset-requests"
RETRY_AFTER = "retry-after"

#: The limit a 429 names, which **no header reports**.
#:
#: Measured 2026-09-04, and it corrected this module's first conclusion. The
#: headers describe an 8,000-token *per-minute* bucket and a 1,000-request
#: *per-day* bucket, and nothing else -- from which it was inferred that the
#: project's 200,000-a-day figure was unenforced. It is enforced. A refusal
#: arrives with the per-minute bucket reading 8,000/8,000 and a body saying
#:
#:     on tokens per day (TPD): Limit 200000, Used 199301, Requested 1279
#:
#: Absence of a header was read as absence of a limit. This pattern exists so
#: the third limit is visible rather than inferred away a second time.
_LIMIT_NAMED = re.compile(
    r"\((?P<code>[A-Z]+)\):\s*Limit (?P<limit>\d+), Used (?P<used>\d+)"
)


def limit_from_message(message: str | None) -> tuple[str, int, int] | None:
    """`(code, limit, used)` from a 429 body, or `None`.

    Text-matching a vendor message, which `003` rightly warns against for
    *control flow*. This is not control flow: nothing branches on it and a
    miss costs one line of diagnostics. The alternative is not knowing which
    of three limits refused the call, which cost Iteration 5 two runs and
    this task one wrong conclusion.
    """
    if not message:
        return None
    match = _LIMIT_NAMED.search(str(message))
    if match is None:
        return None
    return (
        match.group("code"),
        int(match.group("limit")),
        int(match.group("used")),
    )

_DURATION = re.compile(
    r"(?:(?P<h>\d+(?:\.\d+)?)h)?"
    r"(?:(?P<m>\d+(?:\.\d+)?)m(?!s))?"
    r"(?:(?P<s>\d+(?:\.\d+)?)s)?"
    r"(?:(?P<ms>\d+(?:\.\d+)?)ms)?"
    r"$"
)


def parse_duration(value: str | None) -> float | None:
    """Seconds from a header duration such as `547ms`, `4.642s`, `1h37m55.2s`.

    Returns `None` rather than raising on anything unrecognised. A telemetry
    reader that crashes on an unfamiliar format would turn a diagnostic aid into
    an outage, and the whole module is best-effort by construction.

    A bare number is read as seconds, which is what `retry-after` sends.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None

    try:  # `retry-after` is a plain integer count of seconds.
        return float(text)
    except ValueError:
        pass

    match = _DURATION.fullmatch(text)
    if match is None or not any(match.groupdict().values()):
        return None

    parts = {k: float(v) for k, v in match.groupdict().items() if v is not None}
    return (
        parts.get("h", 0.0) * 3600
        + parts.get("m", 0.0) * 60
        + parts.get("s", 0.0)
        + parts.get("ms", 0.0) / 1000
    )


def _as_int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Bucket:
    """One rate-limited quantity: how big, how much is left, how fast it refills."""

    name: str
    limit: int | None = None
    remaining: int | None = None
    reset_seconds: float | None = None

    @property
    def used(self) -> int | None:
        if self.limit is None or self.remaining is None:
            return None
        return self.limit - self.remaining

    @property
    def refill_per_second(self) -> float | None:
        """How fast the bucket refills, derived from the reset time.

        `reset_seconds` is the time until the bucket is *full* again, so the
        outstanding amount divided by that time is the rate. Undefined when the
        bucket is already full -- there is nothing draining to measure -- which
        is honest rather than a division by zero.
        """
        used = self.used
        if not used or not self.reset_seconds:
            return None
        return used / self.reset_seconds

    @property
    def window_seconds(self) -> float | None:
        """How long the bucket takes to refill from empty. **The diagnosis.**

        60 means a per-minute limit; 86400 means per-day. Derived rather than
        assumed, so it survives a provider changing its tiers.
        """
        rate = self.refill_per_second
        if rate is None or self.limit is None:
            return None
        return self.limit / rate

    def describe_window(self) -> str:
        seconds = self.window_seconds
        if seconds is None:
            return "unknown window"
        for span, label in ((86400, "day"), (3600, "hour"), (60, "minute")):
            if abs(seconds - span) <= span * 0.05:
                return f"per {label}"
        return f"per {seconds:.0f}s"

    def seconds_to_afford(self, amount: int) -> float:
        """How long to wait before `amount` more units are available.

        Zero when the bucket already covers it. Zero too when the provider tells
        us nothing -- an unknown limit must not silently become an infinite
        wait, because a pacer that stalls on missing telemetry is worse than one
        that does not pace at all.
        """
        if self.remaining is None or amount <= self.remaining:
            return 0.0
        rate = self.refill_per_second
        if not rate:
            return 0.0
        return (amount - self.remaining) / rate


@dataclass(frozen=True)
class RateLimitSnapshot:
    """What the provider said about its limits on one response.

    Built from whatever headers arrived. Every `x-ratelimit-*` header is kept
    verbatim in `raw`, including ones this module does not model, because B-1
    exists precisely because the project had assumed which limits applied.
    """

    tokens: Bucket = field(default_factory=lambda: Bucket("tokens"))
    requests: Bucket = field(default_factory=lambda: Bucket("requests"))
    retry_after_seconds: float | None = None
    raw: Mapping[str, str] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return bool(self.raw)

    def summary(self) -> str:
        """One line for the terminal, naming the window each limit runs on."""
        if not self.known:
            return "rate limits: not reported by this provider"

        parts = []
        for bucket in (self.tokens, self.requests):
            if bucket.limit is None:
                continue
            parts.append(
                f"{bucket.name} {bucket.remaining:,}/{bucket.limit:,} "
                f"({bucket.describe_window()})"
            )
        if self.retry_after_seconds is not None:
            parts.append(f"retry after {self.retry_after_seconds:g}s")
        return "rate limits: " + ", ".join(parts) if parts else "rate limits: unparsed"


def snapshot_from_headers(headers) -> RateLimitSnapshot | None:
    """Build a snapshot, or `None` when the response carried no limit headers.

    `None` and an empty snapshot are different claims: the first says the
    provider told us nothing, the second would say it told us there are no
    limits. Only the first is ever true here.
    """
    if headers is None:
        return None

    try:
        items = {str(k).lower(): str(v) for k, v in dict(headers).items()}
    except (TypeError, ValueError):
        return None

    raw = {k: v for k, v in items.items() if k.startswith("x-ratelimit-")}
    if RETRY_AFTER in items:
        raw[RETRY_AFTER] = items[RETRY_AFTER]
    if not raw:
        return None

    return RateLimitSnapshot(
        tokens=Bucket(
            name="tokens",
            limit=_as_int(items.get(LIMIT_TOKENS)),
            remaining=_as_int(items.get(REMAINING_TOKENS)),
            reset_seconds=parse_duration(items.get(RESET_TOKENS)),
        ),
        requests=Bucket(
            name="requests",
            limit=_as_int(items.get(LIMIT_REQUESTS)),
            remaining=_as_int(items.get(REMAINING_REQUESTS)),
            reset_seconds=parse_duration(items.get(RESET_REQUESTS)),
        ),
        retry_after_seconds=parse_duration(items.get(RETRY_AFTER)),
        raw=raw,
    )
