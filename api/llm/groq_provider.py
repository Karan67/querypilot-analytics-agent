"""Groq provider — **the only module in this project that imports a vendor SDK.**

A structural test asserts that. If it ever fails, the swappability commitment in
`specs/000-project.md` §5 has quietly stopped being true.

Named `groq_provider` rather than `groq`: a module called `groq.py` inside a
package that also does `import groq` is a shadowing accident waiting to happen.
"""

from __future__ import annotations

import os

import groq

from api.llm.base import (
    LLM_TIMEOUT_SECONDS,
    LLMError,
    RateLimitError,
    TokenUsage,
)

#: Default model (resolved D-1).
#:
#: `llama-3.3-70b-versatile` -- the model this iteration was originally
#: specified against -- returns 404 model_not_found on Groq; no Llama chat model
#: is available. Measured over four questions, this model answered all three
#: real ones correctly at 0.70s median.
#:
#: `openai/gpt-oss-20b` is the documented fast fallback: equally correct on the
#: same questions at 0.59s median. Switch with QUERYPILOT_LLM_MODEL, no code
#: change (AC3).
DEFAULT_MODEL = "openai/gpt-oss-120b"

#: Deterministic generation (AC6). Measured: five identical calls at this
#: setting produced one distinct SQL string.
TEMPERATURE = 0.0


class GroqProvider:
    """Groq chat completions behind the `LLMProvider` interface."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: float = LLM_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            # Fails here rather than at call time (AC4): a provider that
            # constructs without a key and dies on first use turns a
            # configuration mistake into a runtime surprise.
            raise LLMError(
                "No Groq API key configured. Set GROQ_API_KEY in the "
                "environment, or in .env for local development."
            )
        self._api_key = api_key
        self._model = model
        self._client = groq.Groq(api_key=api_key, timeout=timeout)
        self._last_usage: TokenUsage | None = None

    @property
    def model(self) -> str:
        """Which model this provider talks to.

        Not on the `LLMProvider` protocol -- that stays one method. This exists
        because `EVALS.md` records the model id against every number (AC23), and
        the alternative was for the eval runner to read `_model` through the
        class's back door. A number filed against the wrong model is the exact
        failure AC24 argues about for prompt versions.
        """
        return self._model

    def _safe_message(self, exc: Exception) -> str:
        """Describe a failure without ever leaking the key (AC5).

        Vendor exceptions can carry request context, and from Iteration 6 these
        messages stream to a browser. The exception type and text are useful for
        diagnosis, so they are kept -- but scrubbed defensively rather than
        trusted. Belt and braces: the SDK is not believed to include the key,
        and this does not depend on that belief.
        """
        text = f"{type(exc).__name__}: {exc}"
        if self._api_key and self._api_key in text:
            text = text.replace(self._api_key, "<redacted>")
        return text

    @property
    def last_usage(self) -> TokenUsage | None:
        """What the most recent call cost, as Groq counted it (D-1).

        **Best-effort, and off the protocol on purpose.** `complete()` stays one
        method returning one string; a caller that wants this reads it with
        `getattr`, exactly as `describe_model()` reads `model`. A provider that
        does not report usage simply never grows this attribute.

        `None` until a call has been made, and `None` again if a call came back
        without a usage block -- which is not hypothetical, since a streamed or
        errored response may carry none. Callers must treat absence as "count it
        yourself", never as zero: a missing figure silently read as 0 would make
        a run look free.

        Reset at the start of every call, so a failed call cannot leave the
        previous call's numbers standing and be counted twice.
        """
        return self._last_usage

    def complete(self, system: str, user: str) -> str:
        """One call. No retries -- see AC16; retry policy is Iteration 4's."""
        # Cleared first. If the call raises, `last_usage` must not still hold the
        # previous call's cost -- a caller accumulating per call would otherwise
        # bill a successful call twice and a failed one at someone else's rate.
        self._last_usage = None

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=TEMPERATURE,
            )
        except groq.RateLimitError as exc:
            # AC9. Split out *before* the general handler, because a quota
            # refusal is evidence about the clock rather than about the prompt,
            # and Iteration 4 reported the two identically -- which is what made
            # its 82.5% a floor rather than a measurement.
            #
            # Typed, never text-matched: `003` established that message wording
            # is not a contract.
            raise RateLimitError(
                f"Groq rate limit reached. {self._safe_message(exc)}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - see below
            # Broad by necessity, narrow in effect. The SDK raises a family of
            # its own errors plus whatever httpx raises underneath, and this
            # boundary exists precisely so none of that vocabulary escapes into
            # the rest of the project. Every one is re-raised as LLMError with a
            # scrubbed message; nothing is swallowed.
            raise LLMError(f"Groq request failed. {self._safe_message(exc)}") from exc

        self._last_usage = self._read_usage(response)

        # `.reasoning` is deliberately not read. gpt-oss returns its chain of
        # thought in that separate field -- measured at 591 characters against
        # 137 of content on one call -- and it is a model-shaped concept. The
        # interface returns content only.
        content = response.choices[0].message.content
        return content or ""

    @staticmethod
    def _read_usage(response) -> TokenUsage | None:
        """Lift the vendor's usage block into the project's own shape.

        Defensive about every field. The SDK's response model is not a contract
        this project controls, and the failure mode of trusting it -- an
        AttributeError raised *after* a successful, already-paid-for call --
        would turn a working answer into a provider error and spend the budget
        for nothing.
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            return None

        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        if not isinstance(prompt, int) or not isinstance(completion, int):
            return None

        return TokenUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            calls=1,
            measured=True,
        )
