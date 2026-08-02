import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import ROOT_DIR  # noqa: F401 - importing config loads local runtime settings.
from .text_to_sql import ModelConfigurationError, ModelRequestError


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
        prompt = (
            "You are a careful internal data analyst. Answer only from the supplied query result. "
            "Write concise Markdown: lead with the direct answer, then at most two evidence bullets. "
            "Do not invent causes or numbers.\n\n"
            f"Question: {question}\nAnalysis JSON: {json.dumps(analysis, ensure_ascii=False, default=str)}"
        )
        payload = {
            "model": self.model,
            "stream": True,
            "temperature": 0.1,
            "messages": [{"role": "user", "content": prompt}],
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
