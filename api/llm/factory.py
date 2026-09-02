"""Provider selection from configuration (AC3).

The only place that decides *which* provider exists. Everything else takes an
`LLMProvider` and does not care where it came from -- which is what makes
swapping one an environment change rather than a refactor.
"""

from __future__ import annotations

import os

from api.llm.base import LLMError, LLMProvider

#: Environment variables. Names, not values, live in code.
PROVIDER_ENV = "QUERYPILOT_LLM_PROVIDER"
MODEL_ENV = "QUERYPILOT_LLM_MODEL"

DEFAULT_PROVIDER = "groq"


def get_provider(name: str | None = None, model: str | None = None) -> LLMProvider:
    """Build the configured provider.

    Args:
        name: provider key; defaults to `QUERYPILOT_LLM_PROVIDER`, then "groq".
        model: model id; defaults to `QUERYPILOT_LLM_MODEL`, then the
            provider's own default.

    Raises:
        LLMError: unknown provider, or a missing API key (AC4).

    The vendor SDK is imported *inside* the branch, not at module scope, so
    importing this module never requires every provider's dependencies to be
    installed -- and so a provider that is not selected costs nothing.
    """
    provider = (name or os.environ.get(PROVIDER_ENV) or DEFAULT_PROVIDER).strip().lower()

    if provider == "groq":
        from api.llm.groq_provider import DEFAULT_MODEL, GroqProvider

        return GroqProvider(
            api_key=os.environ.get("GROQ_API_KEY", "").strip(),
            model=(model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL).strip(),
        )

    raise LLMError(
        f"Unknown LLM provider {provider!r}. Set {PROVIDER_ENV} to one of: groq."
    )
