"""
LLM Provider abstraction.

The agent loop only calls `provider.generate(messages, stop_sequences=[...])`
and gets back an `LLMResponse`; it doesn't care which provider is behind it.

STAGE 0 (current): one OpenAI-compatible /chat/completions call,
key rotation on 429/5xx. No provider fallback yet.

Never hardcode API keys: they are read from an environment variable (Section VI.3).
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import List, Optional

import requests


@dataclass
class LLMResponse:
    """Normalized result of one LLM call."""

    text: str
    input_tokens: int
    output_tokens: int
    request_time_ms: float
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
    """Talks to any OpenAI-compatible API (OpenRouter / Groq / ...).

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
        stop_sequences: Optional[List[str]] = None,
        max_tokens: int = 1024,
        max_retries: Optional[int] = None,
        deadline: Optional[float] = None,  # absolute time.perf_counter() value
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
        # Reasoning tokens count toward our output limit (Section VI.1), so ask
        # the model to think as little as possible. Each provider uses its own
        # parameter name for this.
        if "openrouter.ai" in self.base_url:
            payload["reasoning"] = {"enabled": False}
        elif "googleapis.com" in self.base_url:
            payload["reasoning_effort"] = "none"
        if stop_sequences:
            payload["stop"] = stop_sequences

        retries = 0
        last_error = ""
        start = time.perf_counter()
        while True:
            request_timeout = 120.0
            if deadline is not None:
                remaining = deadline - time.perf_counter()
                if remaining <= 1:
                    raise TimeoutError(
                        "No time left for an LLM request"
                        + (f" (last error: {last_error})" if last_error else "")
                    )
                request_timeout = min(120.0, remaining)

            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._current_key()}"},
                json=payload,
                timeout=request_timeout,
            )

            if response.status_code != 429 and response.status_code < 500:
                self._limited_keys.discard(self._key_index)
            else:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"

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
                time_left = None if deadline is None else deadline - time.perf_counter()
                if retries < max_retries and (time_left is None or time_left > wait + 1):
                    retries += 1
                    self._rotate_key()
                    time.sleep(wait)
                    continue
            if not response.ok:
                # Include the body: providers explain the real cause there (e.g. unknown model).
                raise RuntimeError(
                    f"HTTP {response.status_code} from {response.url}: {response.text[:500]}"
                )
            break

        data = response.json()
        usage = data.get("usage") or {}
        choices = data.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        # Google omits "content" entirely when the output is empty (for example
        # finish_reason "length"). That is a normal reply, not an error: return
        # empty text and let the agent loop ask the model to try again.
        return LLMResponse(
            text=message.get("content") or "",
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            request_time_ms=(time.perf_counter() - start) * 1000,
            retries=retries,
        )