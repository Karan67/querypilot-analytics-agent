"""Cerebras provider — the second module in this project that imports a vendor SDK.

A structural test asserts that only `api/llm/` imports a vendor SDK, against an
explicit allow-list that now names both `groq` and `cerebras` (Iteration 13,
specs/016-second-llm-provider.md). Adding a third provider is one more entry in
that list, not a new rule.

Named `cerebras_provider` rather than `cerebras`, for the same reason
`groq_provider.py` is not named `groq.py`: a module that both lives inside a
package named after the vendor and does `import cerebras...` is a shadowing
accident waiting to happen.
"""

from __future__ import annotations

import cerebras.cloud.sdk as cerebras

from api.llm.base import (
    LLM_TIMEOUT_SECONDS,
    LLMError,
    RateLimitError,
    TokenUsage,
)
from api.llm.rate_limits import RateLimitSnapshot, snapshot_from_cerebras_headers

#: Default model (decision D-H, specs/016-second-llm-provider-plan.md).
#:
#: Measured live (specs/016-second-llm-provider.md §2.2): this account's
#: `GET /v1/models` lists exactly two ids, `qwen-3.8-27b` and `gpt-oss-120b`.
#: `gpt-oss-120b` is chosen because `EVALS.md`'s existing Groq entries already
#: run `openai/gpt-oss-120b` -- the same underlying open model -- so a
#: Cerebras eval run isolates the comparison to infrastructure and rate
#: limits rather than confounding it with a model-quality difference.
#: Switch with QUERYPILOT_LLM_MODEL, no code change, exactly as for Groq.
DEFAULT_MODEL = "gpt-oss-120b"

#: Deterministic generation, matching the Groq provider's setting (AC6 there).
TEMPERATURE = 0.0


class CerebrasProvider:
    """Cerebras chat completions behind the `LLMProvider` interface."""

    #: The key `api/llm/factory.py` selects this provider by. Same
    #: best-effort-attribute pattern as `.model` below -- see
    #: `GroqProvider.NAME` for why.
    NAME = "cerebras"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: float = LLM_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            # Fails here rather than at call time, matching GroqProvider: a
            # provider that constructs without a key and dies on first use
            # turns a configuration mistake into a runtime surprise.
            raise LLMError(
                "No Cerebras API key configured. Set CEREBRAS_API_KEY in the "
                "environment, or in .env for local development."
            )
        self._api_key = api_key
        self._model = model
        self._client = cerebras.Cerebras(api_key=api_key, timeout=timeout)
        self._last_usage: TokenUsage | None = None
        self._last_rate_limit: RateLimitSnapshot | None = None

    @property
    def model(self) -> str:
        """Which model this provider talks to. Not on the `LLMProvider`
        protocol -- see `GroqProvider.model` for why."""
        return self._model

    def _safe_message(self, exc: Exception) -> str:
        """Describe a failure without ever leaking the key.

        Same defensive scrub as `GroqProvider._safe_message`: the SDK is not
        believed to include the key in an exception's text, and this does not
        depend on that belief.
        """
        text = f"{type(exc).__name__}: {exc}"
        if self._api_key and self._api_key in text:
            text = text.replace(self._api_key, "<redacted>")
        return text

    @property
    def last_usage(self) -> TokenUsage | None:
        """What the most recent call cost, as Cerebras counted it.

        Best-effort and off the protocol, on the same terms as
        `GroqProvider.last_usage`. `None` until a call has been made, and
        `None` again if a call came back without a usage block. Reset at the
        start of every call so a failed call cannot leave a previous call's
        numbers standing.
        """
        return self._last_usage

    @property
    def last_rate_limit(self) -> RateLimitSnapshot | None:
        """What the provider said about its limits on the most recent call.

        Best-effort and off the protocol, on the same terms as
        `GroqProvider.last_rate_limit`. Cerebras's exact header names are
        doc-sourced, not yet measured against a live response
        (specs/016-second-llm-provider.md §2.4/§2.6) -- `snapshot_from_headers`
        is defensive about missing or unrecognised headers regardless of
        vendor, so this degrades to `None` rather than raising if the real
        headers turn out to differ from what was documented. Built by
        `rate_limits.snapshot_from_cerebras_headers`, kept separate from
        Groq's header-name table (decision D-D).
        """
        return self._last_rate_limit

    def complete(self, system: str, user: str) -> str:
        """One call. No retries -- matching GroqProvider's contract."""
        self._last_usage = None
        self._last_rate_limit = None

        try:
            response = self._create(system, user)
        except cerebras.RateLimitError as exc:
            # Read headers before re-raising, mirroring GroqProvider: the
            # vendor response is gone once translated into RateLimitError.
            self._last_rate_limit = snapshot_from_cerebras_headers(
                getattr(getattr(exc, "response", None), "headers", None)
            )
            raise RateLimitError(
                f"Cerebras rate limit reached. {self._safe_message(exc)}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - see GroqProvider.complete
            # Broad by necessity, narrow in effect -- same boundary as
            # GroqProvider: every vendor/transport error becomes LLMError
            # with a scrubbed message, nothing is swallowed. This is also
            # where a 402 (no billing on the account, measured in
            # specs/016-second-llm-provider.md §2.1) surfaces today: it is
            # not a RateLimitError, so it is reported as a plain LLMError.
            raise LLMError(
                f"Cerebras request failed. {self._safe_message(exc)}"
            ) from exc

        self._last_usage = self._read_usage(response)

        # `.reasoning` is deliberately not read, matching GroqProvider: the
        # gpt-oss family exposes its chain of thought in that separate field,
        # and the interface returns content only.
        content = response.choices[0].message.content
        return content or ""

    def _create(self, system: str, user: str):
        """Make the call, capturing rate-limit headers when the SDK allows it.

        Mirrors `GroqProvider._create` exactly: `with_raw_response` exposes
        headers on a **successful** call, which is the only way to learn
        remaining budget before being refused. Falls back to the plain call
        if the SDK does not offer it -- checked rather than assumed, since
        this is a vendor surface the project does not control. Verified
        present in cerebras-cloud-sdk 1.91.0.
        """
        kwargs = dict(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=TEMPERATURE,
        )

        raw_api = getattr(self._client.chat.completions, "with_raw_response", None)
        if raw_api is None:  # pragma: no cover - exercised by mutation
            return self._client.chat.completions.create(**kwargs)

        raw = raw_api.create(**kwargs)
        self._last_rate_limit = snapshot_from_cerebras_headers(
            getattr(raw, "headers", None)
        )
        return raw.parse()

    @staticmethod
    def _read_usage(response) -> TokenUsage | None:
        """Lift the vendor's usage block into the project's own shape.

        Defensive about every field, matching `GroqProvider._read_usage`.
        Cerebras's own response schema names these fields identically to
        Groq's (`prompt_tokens`, `completion_tokens`) -- confirmed by reading
        `cerebras.cloud.sdk`'s generated response types, not yet by a live
        call (specs/016-second-llm-provider.md §2.6) -- so this stays
        defensive rather than trusting the schema at runtime.
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
