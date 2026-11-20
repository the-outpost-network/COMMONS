"""Client for the local llama.cpp `llama-server` (OpenAI-compatible API).

Only ever talks to the server on the internal Docker network. Message text is
personal data, so nothing here logs it.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

# SmolLM3 can emit an (empty) reasoning block even in /no_think mode.
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(Exception):
    """The model server is down, slow or answered something unusable."""


class LLMClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        api_key: str,
        *,
        max_tokens: int = 400,
        timeout_s: float = 180.0,
    ) -> None:
        self._http = http
        self._url = base_url.rstrip("/") + "/v1/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._max_tokens = max_tokens
        self._timeout = timeout_s

    async def complete_json(
        self, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Ask for one JSON object. llama.cpp turns the schema into a grammar, so
        the model can only produce JSON of this shape; the caller still validates it."""
        body = {
            "messages": messages,
            "max_tokens": self._max_tokens,
            "temperature": 0.3,
            "top_p": 0.95,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "intent", "strict": True, "schema": schema},
            },
        }
        try:
            resp = await self._http.post(
                self._url, json=body, headers=self._headers, timeout=self._timeout
            )
        except httpx.HTTPError as e:
            raise LLMError(f"request failed: {type(e).__name__}") from e
        if resp.status_code != 200:
            raise LLMError(f"status {resp.status_code}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
            data = json.loads(_THINK.sub("", content))
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMError("malformed response") from e
        if not isinstance(data, dict):
            raise LLMError("malformed response")
        return data
