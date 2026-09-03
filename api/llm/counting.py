"""Local token counting — the second instrument (`008-prompt-tuning-plan.md` D-1).

Two instruments measure tokens in this project, and they answer different
questions:

| Instrument | Answers | Used for |
|---|---|---|
| the provider's reported `usage` | what was actually billed | the budget abort, spends in `EVALS.md` |
| this module (`tiktoken`) | what a prompt costs, offline | design decisions, prompt-size tests, the T7 A/B |

Neither substitutes for the other. The daily ceiling is denominated in *Groq's*
count, so a local number cannot police it; and a local number is the only one
reproducible in CI without an API key, so a billed number cannot be a unit test.

**This module is not a vendor SDK.** `tiktoken` is a tokenizer, and the §5
swappability commitment is about provider clients -- the structural test guards
`groq` imports under `api/`, and nothing here changes that. It lives in
`api/llm/` because token counting is an LLM-shaped concern, not because it talks
to anyone.

Every figure this module produces is marked `measured=False`, so a sum that
passed through it can never be mistaken for a billed one.
"""

from __future__ import annotations

import functools

from api.llm.base import TokenUsage

#: The encoding of the deployed `openai/gpt-oss-*` family.
#:
#: An approximation of `o200k_harmony`, which is `o200k_base` plus special
#: tokens the prompt text does not contain. Measured at T1 against `cl100k_base`
#: as a control: the two agree to within 6 tokens on a 944-token prompt, so the
#: choice between real tokenizers matters far less than the choice between a
#: tokenizer and the `chars // 4` heuristic it replaced -- which was ~9% high on
#: every component of the prompt.
ENCODING_NAME = "o200k_base"


class TokenCountingUnavailable(RuntimeError):
    """`tiktoken` is not installed.

    Raised rather than silently falling back to a character heuristic. The
    heuristic is exactly what produced the incorrect figures throughout
    `008-prompt-tuning.md` §2, and re-introducing it as a quiet default would
    reproduce that failure in a place nobody would think to check. A caller that
    genuinely cannot count says so; it does not guess.
    """


@functools.lru_cache(maxsize=4)
def _encoding(name: str = ENCODING_NAME):
    try:
        import tiktoken
    except ImportError as exc:  # pragma: no cover - exercised by mutation
        raise TokenCountingUnavailable(
            "tiktoken is not installed, so tokens cannot be counted locally. "
            "Install api/requirements-dev.txt, or read usage from the provider."
        ) from exc
    return tiktoken.get_encoding(name)


def count_tokens(text: str) -> int:
    """Tokens in one string, by the deployed model's encoding.

    Cached encoding, because building it is far more expensive than using it and
    a 40-question projection calls this once per question.
    """
    return len(_encoding().encode(text))


def estimate_usage(system: str, user: str, completion: str = "") -> TokenUsage:
    """A local stand-in for a provider that does not report usage (D-1).

    **Marked `measured=False`**, which is the whole point. D-1's fallback was
    originally proposed as "~4 characters per token"; that estimate is what
    produced the wrong numbers this iteration spent T1 correcting, so the
    fallback is a real tokenizer and the result still says it was not billed.

    Undercounts slightly against any chat API by construction: providers add
    message framing and role tokens that this cannot see. That direction is the
    safe one for a *fallback* but the dangerous one for a *ceiling*, which is why
    the budget projection multiplies by the call budget rather than trusting this
    to be exact.
    """
    return TokenUsage(
        prompt_tokens=count_tokens(system) + count_tokens(user),
        completion_tokens=count_tokens(completion) if completion else 0,
        calls=1,
        measured=False,
    )


def usage_for_call(provider, system: str, user: str, response: str) -> TokenUsage:
    """What one completed call cost: the provider's count, or a local one.

    **This lives here rather than in the orchestrator, and that placement was
    forced by a test.** `007` AC19 forbids `getattr` in `api/agent/orchestrator.py`
    -- nothing in the loop may resolve a name dynamically, because dispatch there
    acts on model output. The first version of this read `provider.last_usage`
    inside the loop and failed that invariant immediately.

    Moving it here is the right answer rather than a workaround. D-1 put the
    best-effort attribute on the *provider*, so reading it is a provider-shaped
    concern and belongs behind the LLM boundary -- the same argument that keeps
    `describe_model()` out of the orchestrator. The loop now asks a function a
    question instead of introspecting an object.

    Never raises. Token accounting is instrumentation, and instrumentation that
    can fail a working answer is a bug: a provider with an exotic `last_usage`,
    or an environment without `tiktoken`, degrades to an empty `TokenUsage`
    rather than taking the run down.
    """
    reported = getattr(provider, "last_usage", None)
    if isinstance(reported, TokenUsage):
        return reported

    try:
        return estimate_usage(system, user, response)
    except Exception:  # noqa: BLE001 - instrumentation must not break the loop
        return TokenUsage()


def project_worst_case(prompt_tokens: int, questions: int, max_calls: int) -> int:
    """The most a run could possibly cost, before it spends anything (AC8).

    **Worst case deliberately.** The plan is explicit: an optimistic projection
    that lets a run die at question 31 wastes the 30 that worked and produces a
    number that is not a measurement. So this assumes every question burns its
    full call budget, which is what a run of nothing but failures would do.

    Completion tokens are excluded and that is a real limitation, stated rather
    than hidden: they cannot be known before generating them. The loop's
    completions are short -- one action line and a SQL statement -- against a
    prompt of roughly a thousand, so the omission is small, but it means this is
    a floor on the worst case rather than a true ceiling. The in-flight check
    against reported usage is what covers the difference.
    """
    return prompt_tokens * questions * max_calls
