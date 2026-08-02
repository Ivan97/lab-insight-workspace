import os
from typing import Any


def model_for_mode(thinking_enabled: bool) -> str:
    mode_key = "LLM_THINKING_MODEL" if thinking_enabled else "LLM_NON_THINKING_MODEL"
    return os.getenv(mode_key) or os.getenv("LLM_MODEL", "deepseek-v4-flash")


def apply_thinking_mode(payload: dict[str, Any], thinking_enabled: bool) -> dict[str, Any]:
    payload["model"] = model_for_mode(thinking_enabled)
    if os.getenv("LLM_PROVIDER", "").lower() == "deepseek":
        payload["thinking"] = {"type": "enabled" if thinking_enabled else "disabled"}
        if thinking_enabled:
            payload["reasoning_effort"] = os.getenv("LLM_REASONING_EFFORT", "high")
    return payload
