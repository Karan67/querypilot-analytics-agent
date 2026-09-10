"""The answer cache: don't pay twice for the same question.

**A cost control, not a latency fix.** `010-hardening.md` §2.3 measured latency
as bimodal — around 750-1250ms cold and up to 10,393ms under throttling — and a
cache does nothing about either. It helps the *second* identical question and
nothing else. Any claim that this made the system faster should be checked
against those numbers.

Three properties, and each of them was chosen against a specific way of being
wrong.

**The key is the exact question text, plus both fingerprints.** No case folding,
no whitespace stripping, no normalisation of any kind: "How many tracks?" and
"how many tracks?" are different keys. A normaliser is a guess about which
differences the model would have ignored, and the answer it serves is
indistinguishable from a correct one. The two fingerprints are what make a hit
*safe* rather than merely cheap — a schema change or a prompt edit changes the
key, so a long-lived process cannot serve an answer derived from a schema or a
prompt that no longer exists.

**Only successful answers are cached**, and that follows from D-3 rather than
being a preference. The cache has no expiry, so caching a failure would serve it
for the life of the process: a question asked during a thirty-second rate limit
would be permanently unanswerable, and the user would have no way to retry. A
failure costs the same as the first attempt to re-run, which is the correct
price for something that might now succeed.

**Identical questions in flight are coalesced** (resolved Q-B). Two users asking
the same thing at the same moment share one provider call rather than racing —
the second waits on the first. Without it the cache helps only questions that
are repeated *slowly*, which is the opposite of the load it should help with.

`cache_hit` therefore means **this request did not call the provider**, which
includes a coalesced follower whose answer is perfectly fresh. That is the
useful claim for both of its readers: the page tells the user the answer may not
have been computed for them, and the token accounting stays whole, because
exactly one row carries the cost of any one provider call.

Not persisted, per resolved D-3: a cache older than the process would be the one
thing in the system that nothing could account for, in the iteration whose whole
subject is observability.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from typing import Any

#: Entries kept before the oldest is dropped.
#:
#: Not expiry — nothing here goes stale by clock, and D-3's "no expiry" stands.
#: This is a bound on memory. The key contains raw user text, so an unbounded
#: dict grows with *input* rather than with data, in a process that is meant to
#: stay up; 256 answers is far more than a demo repeats and small enough that
#: the worst case is bounded by something other than trust.
#:
#: Eviction is by insertion order, so the oldest entry goes even if it is the
#: one being asked most. Least-recently-used would keep hot entries longer; it
#: is not worth the bookkeeping until there is a measurement saying the bound is
#: actually being reached.
CACHE_LIMIT = 256

#: Separates the parts of the key material.
#:
#: A NUL byte rather than nothing, because concatenation alone collides: a
#: question ending "ab" with fingerprint "c..." would key identically to one
#: ending "a" with fingerprint "bc...". NUL cannot appear in a hex fingerprint
#: and would be extraordinary in a question, which is what makes it a separator
#: rather than another thing to escape.
_SEPARATOR = "\x00"


class _Pending:
    """One in-flight computation, and whatever it produced.

    A plain `Event` rather than a `Future` — the waiters need exactly two
    things, the result and the exception, and `concurrent.futures` brings an
    executor's worth of machinery for them.
    """

    __slots__ = ("event", "value", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: Any = None
        self.error: BaseException | None = None


#: Answered questions, newest last. Guarded by `_lock`.
_entries: dict[str, Any] = {}

#: Questions currently being answered, so a second asker can wait rather than
#: start a second provider call. Guarded by `_lock`.
_inflight: dict[str, _Pending] = {}

_lock = threading.Lock()


def cache_key(question: str, schema_fp: str, prompt_fp: str) -> str:
    """The key for one question under one configuration.

    Hashed rather than concatenated so the key is a fixed size whatever the
    question, and so nothing downstream is tempted to parse a user's text back
    out of a dictionary key.
    """
    material = _SEPARATOR.join((question, schema_fp, prompt_fp))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def get_or_compute(
    key: str,
    compute: Callable[[], Any],
    cacheable: Callable[[Any], bool],
) -> tuple[Any, bool]:
    """Return `(value, cache_hit)`, computing at most once per key concurrently.

    `cacheable` decides what is worth keeping and is **required** rather than
    defaulted, because the default that would be convenient — keep everything —
    is the one that makes a transient failure permanent.

    The caller's `compute` may raise. The exception reaches every waiter and
    nothing is stored, so the next caller starts a fresh attempt rather than
    inheriting a dead entry.
    """
    with _lock:
        if key in _entries:
            return _entries[key], True

        pending = _inflight.get(key)
        leader = pending is None
        if leader:
            pending = _Pending()
            _inflight[key] = pending

    if not leader:
        # Someone else is already asking this exact question under this exact
        # configuration. Wait for their answer instead of buying a second one.
        pending.event.wait()
        if pending.error is not None:
            raise pending.error
        return pending.value, True

    value: Any = None
    error: BaseException | None = None
    try:
        value = compute()
        with _lock:
            if cacheable(value):
                _remember(key, value)
    except BaseException as exc:
        error = exc
        raise
    finally:
        # In a `finally` so a waiter can never be stranded: an exception on the
        # way out still wakes everyone, and the key is released either way. The
        # result is published *before* the event is set, or a woken waiter could
        # read a value that has not been written yet.
        pending.value = value
        pending.error = error
        with _lock:
            _inflight.pop(key, None)
        pending.event.set()

    return value, False


def _remember(key: str, value: Any) -> None:
    """Store one answer, evicting the oldest if the bound is reached.

    Caller holds `_lock`.
    """
    _entries[key] = value
    while len(_entries) > CACHE_LIMIT:
        # `dict` preserves insertion order, so the first key is the oldest.
        del _entries[next(iter(_entries))]


def size() -> int:
    with _lock:
        return len(_entries)


def clear() -> None:
    """Empty the cache. For tests and for a future operator endpoint.

    In-flight computations are deliberately left alone: their waiters are
    already committed, and cancelling them would turn a cache operation into a
    failed user request.
    """
    with _lock:
        _entries.clear()
