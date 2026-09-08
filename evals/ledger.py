"""Accumulated daily spend — charter backlog B-5.

**Why a local ledger exists at all, and only for one of three limits.** B-1
measured what the provider reports:

| limit | remaining reported live? |
|---|---|
| 8,000 tokens per minute | yes, in headers |
| 1,000 requests per day | yes, in headers |
| **200,000 tokens per day** | **no — named only in a 429 body** |

For the first two, asking the provider is both easier and more truthful than
bookkeeping. For the third there is nothing to ask, so a run cannot know how
much of the day is gone without having written it down. `--token-budget` policed
only the cost of the invocation in front of it, which is satisfied by a 30,000
ceiling on a run starting at 199,000 used, and then refused on its first call.

**This ledger is a floor, not the truth, and the difference matters.** It counts
what *this project* spent through the eval runner. Spend from the deployed API,
another checkout, a colleague sharing the key or a scratch script is invisible to
it, so the real figure is always at least this and possibly more. Two
consequences worth stating rather than discovering:

- Under-counting fails the safe way round: the guard approves a run the provider
  then refuses, which is exactly today's behaviour and no worse.
- **A 429 is authoritative and overrides the estimate.** Groq's refusal body
  carries `Used 199301`, the provider's own count, and `reconcile()` writes it
  straight over whatever was accumulated. A refusal is therefore not only a
  failure, it is a free correction -- which is the main reason to keep the two
  paths in one place.

Keyed by UTC date, because the limit resets on one and the machine's local date
is not necessarily it. Iteration 5 nearly mis-planned a day around exactly that:
local time was already the 4th while UTC was still the 3rd.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from dataclasses import dataclass, replace

#: Where the ledger lives. Deliberately outside `evals/` and gitignored: it is
#: machine state, not project content, and committing one developer's spend
#: would be meaningless to everyone else and merge-conflict on every run.
DEFAULT_PATH = pathlib.Path(__file__).resolve().parent.parent / ".querypilot" / "spend.json"


def utc_today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class DailySpend:
    """What has been spent against today's limits, so far as this project knows."""

    day: str
    tokens: int = 0
    requests: int = 0
    reconciled: bool = False

    @property
    def estimated(self) -> bool:
        """True while no provider figure has ever corrected this.

        Named the way `TokenUsage.measured` is named, and for the same reason:
        a number that might be a floor and a number the provider itself stated
        are different quantities, and a report that mixed them silently would be
        wrong in the one way nobody checks.
        """
        return not self.reconciled

    def describe(self, token_limit: int) -> str:
        source = "provider-reconciled" if self.reconciled else "local estimate"
        return (
            f"today: {self.tokens:,}/{token_limit:,} tokens, "
            f"{self.requests:,} requests ({source})"
        )


def load(path: pathlib.Path | None = None, *, day: str | None = None) -> DailySpend:
    """Today's spend, or an empty ledger.

    A ledger from an earlier day is not carried forward -- the limit reset, so
    the count did too. Any unreadable file is treated as absent rather than
    raised on: a corrupted state file must not be able to stop a benchmark, and
    the cost of being wrong is one optimistic guard.
    """
    path = DEFAULT_PATH if path is None else path
    day = utc_today() if day is None else day

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DailySpend(day=day)

    if not isinstance(raw, dict) or raw.get("day") != day:
        return DailySpend(day=day)

    return DailySpend(
        day=day,
        tokens=int(raw.get("tokens", 0) or 0),
        requests=int(raw.get("requests", 0) or 0),
        reconciled=bool(raw.get("reconciled", False)),
    )


def _write(spend: DailySpend, path: pathlib.Path) -> DailySpend:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "day": spend.day,
                "tokens": spend.tokens,
                "requests": spend.requests,
                "reconciled": spend.reconciled,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return spend


def record(tokens: int, requests: int, path: pathlib.Path | None = None) -> DailySpend:
    """Add a run's billed spend to today's total.

    `path` resolves at call time rather than binding `DEFAULT_PATH` as a default
    argument. That trap cost T5 a fabricated entry in `EVALS.md`: the module
    constant bound once at import, so a test's `monkeypatch.setattr` had no
    effect and the test wrote to the real record.
    """
    path = DEFAULT_PATH if path is None else path
    current = load(path)
    return _write(
        replace(
            current,
            tokens=current.tokens + max(0, tokens),
            requests=current.requests + max(0, requests),
        ),
        path,
    )


def reconcile(used_tokens: int, path: pathlib.Path | None = None) -> DailySpend:
    """Replace the estimate with the provider's own figure from a 429.

    Overwrites rather than adds. The provider is stating a total, not a
    delta, and it counts spend this ledger never saw -- which is the whole
    reason the local number is only a floor.
    """
    path = DEFAULT_PATH if path is None else path
    current = load(path)
    return _write(
        replace(current, tokens=max(0, used_tokens), reconciled=True), path
    )
