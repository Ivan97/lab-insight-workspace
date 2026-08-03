"""Provider dialects for thinking control and reasoning extraction.

Every provider spells "think" and "do not think" differently, and every
provider returns reasoning text under a different key. Both concerns live here
so that swapping DeepSeek for Gemini stays a configuration change instead of an
edit spread across the planner, the answer stream and the visualization agent.
"""

import os
from enum import StrEnum
from typing import Any, Protocol


class Provider(StrEnum):
    DEEPSEEK = "deepseek"
    GEMINI = "gemini"
    OPENAI = "openai"
    KIMI = "kimi"


class ThinkingMode(StrEnum):
    """How the composer toggle maps onto a request."""

    ENABLED = "enabled"
    DISABLED = "disabled"


class ReasoningEffort(StrEnum):
    """Effort levels, ordered cheapest to most expensive by EFFORT_SCALE."""

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

DEFAULT_MODEL = "deepseek-v4-flash"


class ThinkingDialect(Protocol):
    """One provider's way of expressing thinking control."""

    provider: Provider
    supported_efforts: frozenset[ReasoningEffort]

    def apply(
        self, payload: dict[str, Any], mode: ThinkingMode, effort: ReasoningEffort
    ) -> dict[str, Any]: ...

    def reasoning_from_delta(self, delta: dict[str, Any]) -> str | None: ...


def _nearest_supported(
    effort: ReasoningEffort, supported: frozenset[ReasoningEffort]
) -> ReasoningEffort:
    """Map a configured effort onto the closest level a provider accepts.

    Providers expose different granularity, so a shared setting has to be
    translated rather than rejected.
    """
    if effort in supported:
        return effort
    target = EFFORT_SCALE.index(effort)
    return min(supported, key=lambda candidate: abs(EFFORT_SCALE.index(candidate) - target))


def _openai_style_reasoning(delta: dict[str, Any]) -> str | None:
    """Both providers stream reasoning under one of these OpenAI-style keys."""
    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
    return str(reasoning) if reasoning else None


class DeepSeekDialect:
    """Verified against https://api.deepseek.com/v1 on 2026-08-03.

    `thinking.type` accepts adaptive/enabled/disabled and `reasoning_effort` is
    validated at the top level of the body -- the API rejects an unknown value
    there with HTTP 400 and ignores the same key nested inside `thinking`.
    """

    provider = Provider.DEEPSEEK
    supported_efforts = frozenset(EFFORT_SCALE)

    def apply(
        self, payload: dict[str, Any], mode: ThinkingMode, effort: ReasoningEffort
    ) -> dict[str, Any]:
        payload["thinking"] = {"type": mode.value}
        if mode is ThinkingMode.ENABLED:
            payload["reasoning_effort"] = _nearest_supported(
                effort, self.supported_efforts
            ).value
        else:
            payload.pop("reasoning_effort", None)
        return payload

    def reasoning_from_delta(self, delta: dict[str, Any]) -> str | None:
        return _openai_style_reasoning(delta)


class GeminiDialect:
    """Gemini through its OpenAI-compatibility endpoint.

    Not yet exercised against a live key: Gemini documents `reasoning_effort`
    on the compatibility layer and a `google.thinking_config.thinking_budget`
    escape hatch for finer control, and a budget of 0 is how thinking is turned
    off. Verify both against the real endpoint before trusting this in
    production.
    """

    provider = Provider.GEMINI
    supported_efforts = frozenset(
        {
            ReasoningEffort.NONE,
            ReasoningEffort.LOW,
            ReasoningEffort.MEDIUM,
            ReasoningEffort.HIGH,
        }
    )

    def apply(
        self, payload: dict[str, Any], mode: ThinkingMode, effort: ReasoningEffort
    ) -> dict[str, Any]:
        if mode is ThinkingMode.ENABLED:
            payload["reasoning_effort"] = _nearest_supported(
                effort, self.supported_efforts
            ).value
            payload.pop("google", None)
        else:
            payload["reasoning_effort"] = ReasoningEffort.NONE.value
            payload["google"] = {"thinking_config": {"thinking_budget": 0}}
        return payload

    def reasoning_from_delta(self, delta: dict[str, Any]) -> str | None:
        return _openai_style_reasoning(delta)


class OpenAIDialect:
    """Generic OpenAI-compatible endpoint.

    Only `reasoning_effort` is portable here, and reasoning models cannot
    always be told to stop reasoning, so the field is omitted rather than set
    to a value the endpoint may reject. Turning the toggle off then relies on
    LLM_NON_THINKING_MODEL plus the UI suppression in A2UIStream.
    """

    provider = Provider.OPENAI
    supported_efforts = frozenset(
        {
            ReasoningEffort.MINIMAL,
            ReasoningEffort.LOW,
            ReasoningEffort.MEDIUM,
            ReasoningEffort.HIGH,
        }
    )

    def apply(
        self, payload: dict[str, Any], mode: ThinkingMode, effort: ReasoningEffort
    ) -> dict[str, Any]:
        if mode is ThinkingMode.ENABLED:
            payload["reasoning_effort"] = _nearest_supported(
                effort, self.supported_efforts
            ).value
        else:
            payload.pop("reasoning_effort", None)
        return payload

    def reasoning_from_delta(self, delta: dict[str, Any]) -> str | None:
        return _openai_style_reasoning(delta)


class PassthroughDialect:
    """A provider with no verified thinking switch.

    Nothing provider-specific is sent, because guessing a parameter name is how
    a setting ends up silently ignored. The toggle still selects between
    LLM_THINKING_MODEL and LLM_NON_THINKING_MODEL, and A2UIStream still hides
    reasoning events when it is off.
    """

    provider = Provider.KIMI
    supported_efforts: frozenset[ReasoningEffort] = frozenset()

    def apply(
        self, payload: dict[str, Any], mode: ThinkingMode, effort: ReasoningEffort
    ) -> dict[str, Any]:
        return payload

    def reasoning_from_delta(self, delta: dict[str, Any]) -> str | None:
        return _openai_style_reasoning(delta)


DIALECTS: dict[Provider, ThinkingDialect] = {
    Provider.DEEPSEEK: DeepSeekDialect(),
    Provider.GEMINI: GeminiDialect(),
    Provider.OPENAI: OpenAIDialect(),
    Provider.KIMI: PassthroughDialect(),
}


def _parse(enum_type: type[StrEnum], raw: str, setting: str) -> Any:
    """Reject unknown settings loudly.

    A mistyped value used to be dropped by the provider without complaint,
    which is how LLM_REASONING_EFFORT could look configured while doing
    nothing.
    """
    try:
        return enum_type(raw.strip().lower())
    except ValueError:
        allowed = ", ".join(member.value for member in enum_type)
        raise ValueError(f"{setting}={raw!r} is not supported. Use one of: {allowed}") from None


def current_provider() -> Provider:
    return _parse(Provider, os.getenv("LLM_PROVIDER", Provider.DEEPSEEK.value), "LLM_PROVIDER")


def current_dialect() -> ThinkingDialect:
    return DIALECTS[current_provider()]


def configured_effort() -> ReasoningEffort:
    return _parse(
        ReasoningEffort,
        os.getenv("LLM_REASONING_EFFORT", ReasoningEffort.HIGH.value),
        "LLM_REASONING_EFFORT",
    )


def model_for_mode(thinking_enabled: bool) -> str:
    mode_key = "LLM_THINKING_MODEL" if thinking_enabled else "LLM_NON_THINKING_MODEL"
    return os.getenv(mode_key) or os.getenv("LLM_MODEL", DEFAULT_MODEL)


def apply_thinking_mode(payload: dict[str, Any], thinking_enabled: bool) -> dict[str, Any]:
    """Attach model and thinking control for the configured provider."""
    payload["model"] = model_for_mode(thinking_enabled)
    mode = ThinkingMode.ENABLED if thinking_enabled else ThinkingMode.DISABLED
    return current_dialect().apply(payload, mode, configured_effort())


def reasoning_from_delta(delta: dict[str, Any]) -> str | None:
    """Pull reasoning text out of one streaming delta, if the provider sent any."""
    return current_dialect().reasoning_from_delta(delta)
