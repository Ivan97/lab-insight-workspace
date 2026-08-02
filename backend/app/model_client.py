import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import ROOT_DIR  # noqa: F401 - importing config loads local runtime settings.
from .text_to_sql import ModelConfigurationError, ModelRequestError

ANSWER_SYSTEM_PROMPT = """You are a careful internal data analyst.
Answer only from the supplied query result and never invent causes or numbers.
Write concise Markdown: lead with the direct answer, then at most two evidence bullets.

Presentation contract:
- Preserve the values and units returned by SQL; do not recompute metrics.
- Render date-only values as YYYY-MM-DD and month values as YYYY-MM. Never append 00:00:00.
- Render currency in USD with a $ sign, thousands separators, and two decimal places.
- Render percentages with a % sign and at most two decimal places. The SQL result is already on a 0-100 scale; never multiply it by 100 again.
- Render other decimal measures with at most two decimal places and avoid floating-point noise.
- Render counts as integers.
- Use the supplied table.formats metadata as the authoritative formatting contract.
"""


class OpenAICompatibleModel:
    def __init__(self) -> None:
        self.base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.model = os.getenv("LLM_MODEL", "deepseek-chat")

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    async def stream_answer(self, question: str, analysis: dict[str, Any]) -> AsyncIterator[str]:
        if not self.configured:
            raise ModelConfigurationError(
                "LLM is not configured. Set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL."
            )
        prompt = f"Question: {question}\nAnalysis JSON: {json.dumps(analysis, ensure_ascii=False, default=str)}"
        payload = {
            "model": self.model,
            "stream": True,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = f"{self.base_url}/chat/completions"
        emitted = False
        try:
            async with (
                httpx.AsyncClient(timeout=35, trust_env=False) as client,
                client.stream("POST", url, json=payload, headers=headers) as response,
            ):
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    delta = json.loads(data)["choices"][0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        emitted = True
                        yield content
        except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
            if not emitted:
                raise ModelRequestError(
                    f"Answer model request failed: {type(exc).__name__}"
                ) from exc
