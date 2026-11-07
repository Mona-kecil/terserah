"""OpenAI-compatible streaming chat client.

Works against OpenAI, DeepSeek, OpenRouter, Ollama's /v1 shim, LM Studio,
vLLM, and anything else that speaks the /chat/completions SSE format.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1500,
        timeout: float = 120.0,
        extra: dict | None = None,
    ) -> None:
        if not base_url:
            raise LLMError("LLM base_url is not set. Check .env / personas.yaml.")
        if not model:
            raise LLMError("LLM model is not set. Check .env / personas.yaml.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        # Merged into every request body. Provider quirks live here, not in code.
        self.extra = dict(extra or {})
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0)
        )

    @property
    def _endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        payload = {
            "model": model or self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        payload.update(self.extra)

        try:
            async with self._client.stream(
                "POST", self._endpoint, headers=self._headers, json=payload
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise LLMError(
                        f"LLM returned HTTP {response.status_code}: {body[:400]}"
                    )

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        if data == "[DONE]":
                            break
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece
        except httpx.TimeoutException as exc:
            raise LLMError("The model took too long to respond.") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach the LLM endpoint: {exc}") from exc

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> str:
        parts: list[str] = []
        async for piece in self.stream(messages, model=model, temperature=temperature):
            parts.append(piece)
        return "".join(parts)

    async def aclose(self) -> None:
        await self._client.aclose()
