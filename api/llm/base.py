"""The LLM abstraction — one narrow interface, no vendor concepts.

`specs/000-project.md` §5 commits to this: provider code is reachable only
through one abstraction, and nothing in the orchestrator, tools, or safety layer
imports a vendor SDK. Swapping provider must be a configuration change, not a
refactor.

The interface is deliberately one method returning one string. It carries no
notion of tools, streaming, reasoning traces or token accounting, because those
are shaped differently by every vendor and this project does not yet need any of
them. Tool calling arrives with `specs/007-agent-loop.md`, which is where the
provider-shaped serialisation belongs -- the argument is recorded in
`specs/002-sql-validation-plan.md` D-2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: Seconds before an LLM call is abandoned (resolved D-2).
#:
#: Measured median latency for the default model is 0.70s and the observed max
#: is 0.84s, so this is roughly 24x headroom -- enough for a cold start or a
#: harder Iteration 5 question, while still failing fast enough that a hung
#: request does not occupy a worker for half a minute.
LLM_TIMEOUT_SECONDS = 20


class LLMError(RuntimeError):
    """A provider call failed: network, authentication, rate limit, timeout.

    Distinct from anything the database raises so `single_shot` can categorise
    it separately -- the agent's response to "the model is unreachable" is not
    the same as its response to "that column does not exist".

    **Never carries credential material.** Providers compose this message
    themselves rather than formatting a vendor exception into it, and redact
    defensively -- see `GroqProvider._safe_message`.
    """


class RateLimitError(LLMError):
    """The provider refused the call because a quota or rate limit was hit.

    A subclass rather than a flag, so callers branch on a *type* the way
    `RETRY_POLICY` branches on a category. `003` established the rule this
    follows: message wording is not a contract, and classifying a failure by
    matching text is how a vendor's phrasing change silently reclassifies every
    affected run.

    **This is the one distinction the interface makes between failures**, and it
    earns its place because the two are not the same event. A model failure is
    evidence about the prompt; a rate limit is evidence about the clock. Counting
    them together is what made Iteration 4's 82.5% a floor rather than a
    measurement -- 33 correct and 7 rate-limited, reported identically
    (`008-prompt-tuning.md` AC9).

    Providers map their own SDK's rate-limit type to this. A provider that cannot
    tell the difference simply raises `LLMError` and its runs are categorised as
    provider failures, which is the honest answer for a provider that does not
    report it.
    """


@dataclass(frozen=True)
class TokenUsage:
    """What one provider call cost, as the provider itself counted it.

    **Not on the `LLMProvider` protocol.** The interface stays one method
    (`008-prompt-tuning-plan.md` D-1): a concrete provider may expose a
    best-effort `last_usage` attribute, and callers read it with `getattr` --
    exactly the precedent `GroqProvider.model` and `describe_model()` already
    set. A provider that reports nothing simply has no attribute, and the caller
    falls back to counting locally.

    `measured` is the field that keeps the two instruments apart. A local
    `tiktoken` count and a provider's billed count are **different quantities**,
    and D-1's resolution requires every recorded figure to say which it is: the
    daily ceiling is denominated in the provider's number, while only the local
    one is reproducible offline. Mixing them silently would make `EVALS.md`
    unreadable in the one way that matters -- a run that looks cheaper because
    it was measured differently.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    measured: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Accumulate. **Degrades to estimated if either side is**, because a
        sum containing one estimate is an estimate.

        A **zero-call usage is the identity** and contributes nothing at all,
        including its `measured` flag. Without that carve-out an empty
        accumulator -- `TokenUsage()`, which cannot have been measured because
        no call was made -- would poison the first real addition and report
        every billed run as an estimate. Caught by a test asserting a
        single-call run was measured; the arithmetic said otherwise.

        The alternative was to seed accumulators with `TokenUsage(measured=True)`,
        which is a lie that happens to sum correctly, and one every future caller
        would have to remember.
        """
        if not isinstance(other, TokenUsage):
            return NotImplemented
        if other.calls == 0 and other.total_tokens == 0:
            return self
        if self.calls == 0 and self.total_tokens == 0:
            return other
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            calls=self.calls + other.calls,
            measured=self.measured and other.measured,
        )


@runtime_checkable
class LLMProvider(Protocol):
    """One completion call. That is the whole contract."""

    def complete(self, system: str, user: str) -> str:
        """Return the model's text response.

        Args:
            system: instructions and schema context.
            user: the question, inserted as data.

        Returns:
            The response content, with any provider-specific wrapper removed.
            Reasoning traces exposed as a separate field are **discarded** --
            they are a model-shaped concept and surfacing them would leak a
            vendor detail into this interface.

        Raises:
            LLMError: on any provider failure.
        """
        ...
