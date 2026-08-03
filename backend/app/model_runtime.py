"""LangChain chat models, configured per provider.

Transport, retries, streaming and tool binding come from LangChain. What stays
here is the part LangChain cannot know: how each provider spells "think" and
"do not think", and which levels of effort it accepts. Swapping DeepSeek for
Gemini is then a change to .env rather than to the planner or the answer stream.

Two behaviours are deliberate and verified against the live DeepSeek API:

- `thinking` travels in `extra_body`; the endpoint validates `reasoning_effort`
  at the top level of the body and silently ignores it nested inside `thinking`.
- The OpenAI SDK honours proxy environment variables by default, which the old
  hand-rolled client disabled. Both HTTP clients are pinned to trust_env=False
  so a SOCKS proxy in the environment cannot capture model traffic.
"""

import os
from enum import StrEnum
from typing import Any

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk

HTTP_TIMEOUT = 120.0


class Provider(StrEnum):
    DEEPSEEK = "deepseek"
    GEMINI = "gemini"
    OPENAI = "openai"
    KIMI = "kimi"


class ReasoningEffort(StrEnum):
    """Ordered cheapest to most expensive by EFFORT_SCALE."""

    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


EFFORT_SCALE: tuple[ReasoningEffort, ...] = (
    ReasoningEffort.NONE,
    ReasoningEffort.MINIMAL,
    ReasoningEffort.LOW,
    ReasoningEffort.MEDIUM,
    ReasoningEffort.HIGH,
    ReasoningEffort.XHIGH,
    ReasoningEffort.MAX,
)

# Levels each provider actually accepts. A configured level outside the set is
# translated to the nearest one rather than dropped.
SUPPORTED_EFFORTS: dict[Provider, frozenset[ReasoningEffort]] = {
    Provider.DEEPSEEK: frozenset(EFFORT_SCALE),
    Provider.GEMINI: frozenset(
        {
            ReasoningEffort.NONE,
            ReasoningEffort.LOW,
            ReasoningEffort.MEDIUM,
            ReasoningEffort.HIGH,
        }
    ),
    Provider.OPENAI: frozenset(
        {
            ReasoningEffort.MINIMAL,
            ReasoningEffort.LOW,
            ReasoningEffort.MEDIUM,
            ReasoningEffort.HIGH,
        }
    ),
    Provider.KIMI: frozenset(),
}

DEFAULT_MODEL = "deepseek-v4-flash"


def _parse(enum_type: type[StrEnum], raw: str, setting: str) -> Any:
    """Reject unknown settings loudly.

    Providers ignore request fields they do not recognise, so a typo used to
    look configured while doing nothing.
    """
    try:
        return enum_type(raw.strip().lower())
    except ValueError:
        allowed = ", ".join(member.value for member in enum_type)
        raise ValueError(f"{setting}={raw!r} is not supported. Use one of: {allowed}") from None


def current_provider() -> Provider:
    return _parse(Provider, os.getenv("LLM_PROVIDER", Provider.DEEPSEEK.value), "LLM_PROVIDER")


def configured_effort() -> ReasoningEffort:
    return _parse(
        ReasoningEffort,
        os.getenv("LLM_REASONING_EFFORT", ReasoningEffort.HIGH.value),
        "LLM_REASONING_EFFORT",
    )


def model_for_mode(thinking_enabled: bool) -> str:
    mode_key = "LLM_THINKING_MODEL" if thinking_enabled else "LLM_NON_THINKING_MODEL"
    return os.getenv(mode_key) or os.getenv("LLM_MODEL", DEFAULT_MODEL)


def _nearest_supported(
    effort: ReasoningEffort, supported: frozenset[ReasoningEffort]
) -> ReasoningEffort:
    if effort in supported:
        return effort
    target = EFFORT_SCALE.index(effort)
    return min(supported, key=lambda candidate: abs(EFFORT_SCALE.index(candidate) - target))


def thinking_body(provider: Provider, thinking_enabled: bool) -> dict[str, Any]:
    """Provider-specific request fields carrying the composer toggle."""
    supported = SUPPORTED_EFFORTS[provider]
    effort = _nearest_supported(configured_effort(), supported) if supported else None

    if provider is Provider.DEEPSEEK:
        body: dict[str, Any] = {"thinking": {"type": "enabled" if thinking_enabled else "disabled"}}
        if thinking_enabled and effort:
            body["reasoning_effort"] = effort.value
        return body

    if provider is Provider.GEMINI:
        if thinking_enabled and effort:
            return {"reasoning_effort": effort.value}
        # A budget of zero is how Gemini is told not to think. Documented but
        # not yet exercised against a live key.
        return {
            "reasoning_effort": ReasoningEffort.NONE.value,
            "google": {"thinking_config": {"thinking_budget": 0}},
        }

    if provider is Provider.OPENAI:
        # Reasoning models cannot always be told to stop, so the field is
        # omitted rather than set to a value the endpoint may reject.
        return {"reasoning_effort": effort.value} if thinking_enabled and effort else {}

    # Kimi has no verified switch; the toggle only selects a model id.
    return {}


class ModelConfigurationError(RuntimeError):
    pass


def chat_model(thinking_enabled: bool, **overrides: Any) -> BaseChatModel:
    """Build the chat model for one call, with thinking control applied."""
    base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "")
    model = model_for_mode(thinking_enabled)
    if not base_url or not api_key or not model:
        raise ModelConfigurationError(
            "LLM is not configured. Set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL."
        )
    provider = current_provider()
    shared: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "extra_body": thinking_body(provider, thinking_enabled),
        "http_client": httpx.Client(trust_env=False, timeout=HTTP_TIMEOUT),
        "http_async_client": httpx.AsyncClient(trust_env=False, timeout=HTTP_TIMEOUT),
        **overrides,
    }
    if provider is Provider.DEEPSEEK:
        from langchain_deepseek import ChatDeepSeek

        return ChatDeepSeek(api_base=base_url, **shared)

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(base_url=base_url, **shared)


def reasoning_text(chunk: AIMessageChunk) -> str:
    """Reasoning emitted by one streamed chunk.

    LangChain normalises provider reasoning into `reasoning` content blocks, so
    this works the same for DeepSeek's reasoning_content and Gemini's thoughts.
    """
    return "".join(
        str(block.get("reasoning") or "")
        for block in (chunk.content_blocks or [])
        if block.get("type") == "reasoning"
    )


def answer_text(chunk: AIMessageChunk) -> str:
    """Visible answer text emitted by one streamed chunk, excluding reasoning."""
    return "".join(
        str(block.get("text") or "")
        for block in (chunk.content_blocks or [])
        if block.get("type") == "text"
    )
