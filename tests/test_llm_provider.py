"""Tests for the provider layer — specs/005-single-shot-generation.md AC1–AC6.

No network: the SDK client is replaced, so these exercise our adapter rather
than Groq. The live tests in `test_llm_live.py` cover the real thing.

A coverage audit found this whole layer untested — including **AC5, that the API
key never reaches an error message**, which is a security property that had been
verified only by a smoke test I ran by hand.
"""

from __future__ import annotations

import pytest

from api.llm.base import LLM_TIMEOUT_SECONDS, LLMError, LLMProvider
from api.llm.factory import MODEL_ENV, PROVIDER_ENV, get_provider
from api.llm.groq_provider import DEFAULT_MODEL, TEMPERATURE, GroqProvider

FAKE_KEY = "gsk_thisisafakekeyforteststhatmustneverleak"


@pytest.fixture
def provider(monkeypatch):
    """A GroqProvider whose SDK client is inert."""
    p = GroqProvider(api_key=FAKE_KEY, model="test-model")
    monkeypatch.setattr(p, "_client", object())
    return p


# --- AC1: the abstraction ---------------------------------------------------


def test_ac1_groq_provider_satisfies_the_protocol():
    assert isinstance(GroqProvider(api_key=FAKE_KEY), LLMProvider)


def test_ac1_interface_is_one_method():
    """Deliberately narrow. No tools, no streaming, no token accounting — those
    are shaped differently by every vendor, and adding one now would put a
    vendor concept into the interface that exists to keep them out."""
    import inspect

    methods = [
        name
        for name, _ in inspect.getmembers(LLMProvider, inspect.isfunction)
        if not name.startswith("_")
    ]
    assert methods == ["complete"]


# --- AC3: configuration -----------------------------------------------------


def test_ac3_defaults_to_groq(monkeypatch):
    monkeypatch.delenv(PROVIDER_ENV, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    assert isinstance(get_provider(), GroqProvider)


def test_ac3_model_comes_from_the_environment(monkeypatch):
    """Switching model must be an environment change, not a code change — which
    matters because the model this iteration was specified against,
    `llama-3.3-70b-versatile`, turned out not to exist."""
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    monkeypatch.setenv(MODEL_ENV, "openai/gpt-oss-20b")
    assert get_provider()._model == "openai/gpt-oss-20b"


def test_ac3_explicit_argument_beats_the_environment(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    monkeypatch.setenv(MODEL_ENV, "from-env")
    assert get_provider(model="from-argument")._model == "from-argument"


def test_ac3_default_model_is_one_that_exists():
    """`llama-3.3-70b-versatile` returns 404 on Groq. Pinned so the dead model
    cannot quietly return as a default."""
    assert DEFAULT_MODEL == "openai/gpt-oss-120b"
    assert "llama" not in DEFAULT_MODEL


def test_ac3_unknown_provider_is_rejected(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "not-a-provider")
    with pytest.raises(LLMError, match="Unknown LLM provider"):
        get_provider()


# --- AC4: missing key -------------------------------------------------------


@pytest.mark.parametrize("key", ["", "   ", None])
def test_ac4_missing_key_fails_at_construction(monkeypatch, key):
    """At construction, not at call time. A provider that builds without a key
    and dies on first use turns a configuration mistake into a runtime
    surprise — and in this project, into a failed eval run."""
    if key is None:
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
    else:
        monkeypatch.setenv("GROQ_API_KEY", key)

    with pytest.raises(LLMError) as caught:
        get_provider()

    assert "GROQ_API_KEY" in str(caught.value)


def test_ac4_message_says_what_to_do(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMError) as caught:
        get_provider()
    message = str(caught.value).lower()
    assert "environment" in message or ".env" in message


# --- AC5: the key never leaks ----------------------------------------------


def test_ac5_key_is_redacted_from_error_messages(provider):
    """**The security property of this layer.**

    Vendor exceptions can carry request context, and from Iteration 6 these
    messages stream to a browser. This does not depend on believing the SDK
    keeps the key out of its own errors — it scrubs regardless.
    """
    leaky = RuntimeError(f"auth failed for key {FAKE_KEY} on /v1/chat")
    message = provider._safe_message(leaky)

    assert FAKE_KEY not in message
    assert "<redacted>" in message
    assert "RuntimeError" in message, "the failure type must survive redaction"


def test_ac5_redaction_keeps_the_message_useful(provider):
    """Scrubbing must not reduce every error to a blank. The type and the
    surrounding text are what make a failure diagnosable."""
    message = provider._safe_message(ValueError("rate limit exceeded, retry in 20s"))
    assert "ValueError" in message
    assert "rate limit exceeded" in message


def test_ac5_key_absent_from_the_raised_llm_error(monkeypatch):
    """End to end through `complete()`: whatever the SDK throws, the key is not
    in what escapes."""

    class _LeakyClient:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError(f"boom with {FAKE_KEY}")

    p = GroqProvider(api_key=FAKE_KEY)
    monkeypatch.setattr(p, "_client", _LeakyClient)

    with pytest.raises(LLMError) as caught:
        p.complete("system", "user")

    assert FAKE_KEY not in str(caught.value)
    assert FAKE_KEY not in repr(caught.value)


def test_ac5_key_is_not_an_attribute_anything_prints(provider):
    """`repr()` of the provider must not expose it either — a provider dropped
    into a log line or a traceback frame is a plausible accident."""
    assert FAKE_KEY not in repr(provider)
    assert FAKE_KEY not in str(provider)


# --- AC6: determinism -------------------------------------------------------


def test_ac6_temperature_is_zero():
    """Measured: five identical calls at this setting produced one distinct SQL
    string. Not a guarantee — batching can defeat it — but it is the setting
    that gives `EVALS.md` its best chance of attributing a delta to a change."""
    assert TEMPERATURE == 0.0


def test_ac6_temperature_is_actually_sent(monkeypatch):
    captured = {}

    class _Client:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    captured.update(kwargs)

                    class _R:
                        choices = [
                            type("C", (), {"message": type("M", (), {"content": "SELECT 1"})()})()
                        ]

                    return _R()

    p = GroqProvider(api_key=FAKE_KEY, model="m")
    monkeypatch.setattr(p, "_client", _Client)
    p.complete("sys", "usr")

    assert captured["temperature"] == 0.0
    assert captured["model"] == "m"
    assert [m["role"] for m in captured["messages"]] == ["system", "user"]


def test_reasoning_field_is_discarded(monkeypatch):
    """gpt-oss returns its chain of thought in a separate `reasoning` field —
    591 characters against 137 of content on one measured call. It is a
    model-shaped concept, and surfacing it would leak a vendor detail into an
    interface that exists to keep them out."""

    class _Client:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    message = type(
                        "M", (), {"content": "SELECT 1", "reasoning": "long trace"}
                    )()
                    return type("R", (), {"choices": [type("C", (), {"message": message})()]})()

    p = GroqProvider(api_key=FAKE_KEY)
    monkeypatch.setattr(p, "_client", _Client)
    assert p.complete("s", "u") == "SELECT 1"


def test_none_content_becomes_empty_string(monkeypatch):
    """A refusal can arrive with `content=None`. That must reach `extract_sql`
    as "" and become `no_sql_returned`, not an AttributeError."""

    class _Client:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    message = type("M", (), {"content": None})()
                    return type("R", (), {"choices": [type("C", (), {"message": message})()]})()

    p = GroqProvider(api_key=FAKE_KEY)
    monkeypatch.setattr(p, "_client", _Client)
    assert p.complete("s", "u") == ""


def test_timeout_matches_the_measured_decision():
    """Resolved D-2: 20s, roughly 24x the measured 0.84s maximum."""
    assert LLM_TIMEOUT_SECONDS == 20
