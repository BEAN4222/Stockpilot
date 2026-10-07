"""
LLM provider: one OpenAI-compatible /chat/completions call with tool calling.

The agent only calls `provider.generate(messages, tools=[...])` and gets back
an `LLMResponse`; it doesn't care which provider is behind it.

Never hardcode API keys: they are read from an environment variable.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional

import requests


@dataclass
class LLMResponse:
    """Normalized result of one LLM call."""

    text: str
    # The assistant message exactly as the API returned it. It is sent back
    # unchanged on the next turn: some providers (e.g. Gemini) attach data to
    # tool calls that must come back with them.
    message: dict
    tool_calls: List[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    request_time_ms: float = 0.0
    retries: int = 0


def _retry_delay(response) -> Optional[float]:
    """Seconds the provider asks us to wait (Retry-After header, or a
    'retry in 55.2s' hint in the body). None if it does not say."""
    header = getattr(response, "headers", {}).get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    match = re.search(r"retry in ([0-9.]+)\s*s", response.text or "", re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return None


class LLMProvider:
    """Talks to any OpenAI-compatible API (OpenRouter / Gemini / ...).

    Several keys can be stored comma-separated in one env var:
        OPENROUTER_API_KEY=key1,key2,key3
    """

    def __init__(self, model_name: str, base_url: str, api_key_env: str):
        self.model_name = model_name
        self.base_url = base_url.rstrip("/")
        raw_keys = os.environ.get(api_key_env, "")
        self.api_keys: List[str] = [k.strip() for k in raw_keys.split(",") if k.strip()]
        if not self.api_keys:
            raise ValueError(
                f"No API key found in environment variable {api_key_env}. "
                f"Check your .env file."
            )
        self._key_index = 0
        # Keys that answered 429 and have not been tried successfully since.
        self._limited_keys: set = set()

    def _current_key(self) -> str:
        return self.api_keys[self._key_index]

    def _rotate_key(self) -> None:
        self._key_index = (self._key_index + 1) % len(self.api_keys)

    def generate(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        max_tokens: int = 4096,
        max_retries: Optional[int] = None,
    ) -> LLMResponse:
        # Enough retries to try every key once, plus a few more.
        if max_retries is None:
            max_retries = len(self.api_keys) + 2

        payload = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        retries = 0
        start = time.perf_counter()
        while True:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._current_key()}"},
                json=payload,
                timeout=120,
            )

            if response.status_code != 429 and response.status_code < 500:
                self._limited_keys.discard(self._key_index)

            # Rate limited or server error: switch key, wait, try again.
            if response.status_code == 429 or response.status_code >= 500:
                if response.status_code == 429:
                    wait = 2.0 * (retries + 1)
                    self._limited_keys.add(self._key_index)
                    # Another key has its own quota: switch to it and wait only
                    # briefly. If every key is limited, wait as long as the
                    # provider asks (when it says how long).
                    if len(self._limited_keys) >= len(self.api_keys):
                        hinted = _retry_delay(response)
                        if hinted is not None:
                            wait = max(wait, hinted + 1.0)
                            self._limited_keys.clear()
                    else:
                        wait = 2.0
                else:
                    # 5xx (e.g. 503 "high demand") hits the whole model, not one
                    # key: wait longer each time, capped at 30s.
                    wait = min(30.0, 5.0 * (2 ** retries))
                if retries < max_retries:
                    retries += 1
                    self._rotate_key()
                    time.sleep(wait)
                    continue
            if not response.ok:
                # Include the body: providers explain the real cause there
                # (e.g. unknown model, or a model without tool support).
                raise RuntimeError(
                    f"HTTP {response.status_code} from {response.url}: {response.text[:500]}"
                )
            break

        data = response.json()
        usage = data.get("usage") or {}
        choices = data.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        message.setdefault("role", "assistant")
        return LLMResponse(
            text=message.get("content") or "",
            message=message,
            tool_calls=message.get("tool_calls") or [],
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            request_time_ms=(time.perf_counter() - start) * 1000,
            retries=retries,
        )
