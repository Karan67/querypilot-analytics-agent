"""Keep a run from outrunning the provider's token bucket — charter backlog B-1.

**The problem this solves, measured.** Groq's free tier refills tokens at
8,000 a minute into a bucket that holds 8,000. An eval call costs about 1,200
tokens, so the bucket covers roughly six back-to-back calls and then refuses.
The agent loop answers in about a second, so an unpaced 20-question pass fires
its first seven calls inside ten seconds, empties the bucket, and spends the
rest of the run being throttled.

**What this does not solve, and the distinction cost a wrong conclusion.** The
same account also has a 200,000-token *daily* allowance, which no header reports
and which only a 429 body names. Pacing cannot help there: a daily bucket does
not refill on any timescale a run can wait for, and Iteration 5's two lost
held-out runs were that limit rather than this one. `MAX_RETRY_WAIT_SECONDS`
below exists precisely so this module declines to pretend otherwise -- it paces
a per-minute bucket and gets out of the way of a per-day one.

**Why a wrapper rather than a change to the provider.** `PacedProvider`
satisfies the same one-method interface (`complete(system, user) -> str`), so
anything taking an `LLMProvider` takes this. That matters because the deployed
API must *not* pace: sleeping inside a user's request would trade a rare 429 for
a guaranteed delay on every call. The eval runner opts in; the API does not.

**Nothing is assumed about the limits.** The pacer reads what the provider
reported on the previous call and does nothing at all when a provider reports
nothing -- an unknown limit must never become an infinite wait. Its estimate of
the next call's cost is the previous call's *billed* cost, so the pacing is
driven by measurement rather than by a constant someone guessed.
"""

from __future__ import annotations

import time

from api.llm.base import RateLimitError

#: Fraction of the token bucket kept in reserve.
#:
#: The estimate of the next call's cost is the previous call's, and prompts grow
#: within a run as the agent loop re-renders a longer transcript. Pacing to the
#: exact remaining figure would therefore run a call short every time the
#: estimate was low by a token. A fifth of the bucket is enough to absorb that
#: without materially slowing a run whose calls are far apart anyway.
DEFAULT_HEADROOM = 0.2

#: Assumed cost of the *first* call, before anything has been billed.
#:
#: Measured: Iteration 5's held-out runs billed 24,276 tokens over 20 calls.
#: Only ever used once per run -- every later call is paced against a real
#: figure -- so being wrong here costs at most one call's worth of delay.
DEFAULT_CALL_TOKENS = 1_200

#: Longest pause the pacer will take on a provider's `retry-after`.
#:
#: Pacing is for a bucket that refills in seconds. A daily allowance does not,
#: and Groq answers an exhausted one with `retry-after` values of 85 and 251
#: seconds -- measured. Sleeping through those turns a 30-question run into a
#: two-hour crawl that produces nothing recordable anyway, because AC18 refuses
#: to file a rate-limited run.
#:
#: Above this, the refusal is allowed through. Failing in a minute beats failing
#: in two hours, and the run's own `rate_limited` count then says what happened.
MAX_RETRY_WAIT_SECONDS = 30.0


class PacedProvider:
    """An `LLMProvider` that waits when the provider's bucket says to.

    Forwards `model`, `last_usage` and `last_rate_limit` so that callers reading
    those with `getattr` -- `describe_model`, the token accounting, the runner's
    telemetry line -- see straight through the wrapper.
    """

    def __init__(
        self,
        inner,
        *,
        headroom: float = DEFAULT_HEADROOM,
        first_call_tokens: int = DEFAULT_CALL_TOKENS,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ) -> None:
        self._inner = inner
        self._headroom = headroom
        self._first_call_tokens = first_call_tokens
        self._sleep = sleep
        self._monotonic = monotonic
        self._snapshot_at: float | None = None
        self._retry_after: float | None = None
        self.total_slept = 0.0
        self.waits = 0

    # --- pass-through surface ------------------------------------------------

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "unknown")

    @property
    def last_usage(self):
        return getattr(self._inner, "last_usage", None)

    @property
    def last_rate_limit(self):
        return getattr(self._inner, "last_rate_limit", None)

    @property
    def inner(self):
        return self._inner

    # --- the pacing itself ---------------------------------------------------

    def complete(self, system: str, user: str) -> str:
        wait = self._wait_seconds()
        if wait > 0:
            self.waits += 1
            self.total_slept += wait
            self._sleep(wait)

        self._retry_after = None
        try:
            return self._inner.complete(system, user)
        except RateLimitError:
            # The provider has just told us how long to wait; honour it on the
            # next call rather than charging straight back into a refusal.
            snapshot = self.last_rate_limit
            if snapshot is not None and snapshot.retry_after_seconds:
                self._retry_after = snapshot.retry_after_seconds
            raise
        finally:
            self._snapshot_at = self._monotonic()

    def _wait_seconds(self) -> float:
        """How long to pause before the next call.

        Zero whenever the answer is unknown. A pacer that stalls because a
        provider reported nothing is worse than no pacer at all, so every
        missing figure resolves to "go".
        """
        if self._retry_after:
            # A wait longer than this is not pacing, it is queuing behind a
            # limit that will not refill on a useful timescale.
            return min(self._retry_after, MAX_RETRY_WAIT_SECONDS)

        snapshot = self.last_rate_limit
        if snapshot is None or not snapshot.known:
            return 0.0

        cost = self._next_call_cost()

        # Credit the time already spent since the provider reported. A pass does
        # real work between calls -- generation, validation, SQL execution,
        # scoring -- and the bucket refills throughout. Without this the pacer
        # would sleep for time that has already elapsed and roughly double the
        # wall-clock cost of a run.
        elapsed = 0.0
        if self._snapshot_at is not None:
            elapsed = max(0.0, self._monotonic() - self._snapshot_at)

        waits = []
        tokens = snapshot.tokens
        if tokens.limit:
            needed = int(cost + tokens.limit * self._headroom)
            waits.append(tokens.seconds_to_afford(needed) - elapsed)

        requests = snapshot.requests
        if requests.limit:
            waits.append(requests.seconds_to_afford(1) - elapsed)

        return max([w for w in waits if w > 0], default=0.0)

    def _next_call_cost(self) -> int:
        """The previous call's billed cost, which is the best available estimate.

        Falls back to the measured default only before anything has been billed.
        A local `tiktoken` count is deliberately *not* used here: the bucket is
        denominated in the provider's tokens, and `008` D-1 is explicit that the
        provider's number wins for anything describing quota.
        """
        usage = self.last_usage
        if usage is not None and usage.calls and usage.total_tokens:
            return usage.total_tokens
        return self._first_call_tokens
