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
