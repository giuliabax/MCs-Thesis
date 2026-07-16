"""Groq implementation of the provider-neutral LLM client."""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from groq import Groq, RateLimitError

from thesis_rest_tester.domain.models import TokenUsage
from thesis_rest_tester.llm.base import LLMClient, LLMResponse

_THINK_BLOCK = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


class GroqLLMClient(LLMClient):
    def __init__(
        self,
        model: str,
        default_temperature: float = 0.1,
        default_max_tokens: int = 4096,
        sdk_client: Any | None = None,
    ) -> None:
        api_key = os.getenv("GROQ_API_KEY")
        if sdk_client is None and not api_key:
            raise ValueError(
                "GROQ_API_KEY is missing. Set it in the environment or a local .env file, "
                "or use --dry-run."
            )
        self._client = (
            sdk_client if sdk_client is not None else Groq(api_key=api_key, max_retries=2)
        )
        self._model = model
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens
        self._logger = logging.getLogger(__name__)

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        think: bool = True,
    ) -> LLMResponse:
        response = self._create_with_rate_limit_retries(
            system_prompt,
            user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            think=think,
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("Groq returned an empty response")
        # Reasoning models (e.g. Qwen on Groq) can emit an inline <think> block. With
        # reasoning_format="parsed" the server separates it, but strip here too so
        # downstream JSON parsing is robust if a model inlines it anyway.
        content = _THINK_BLOCK.sub("", content, count=1)

        usage = getattr(response, "usage", None)
        token_usage = None
        if usage is not None:
            token_usage = TokenUsage(
                prompt_tokens=getattr(usage, "prompt_tokens", None),
                completion_tokens=getattr(usage, "completion_tokens", None),
                total_tokens=getattr(usage, "total_tokens", None),
            )
        return LLMResponse(
            text=content,
            token_usage=token_usage,
            model=getattr(response, "model", self._model),
        )

    def _create_with_rate_limit_retries(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float | None,
        max_tokens: int | None,
        think: bool,
    ) -> Any:
        # When reasoning is requested, ask the model to return it out-of-band so
        # ``content`` stays clean JSON (Groq ignores this for non-reasoning models).
        extra_body = {"reasoning_format": "parsed"} if think else None
        max_attempts = 6
        for attempt in range(max_attempts):
            try:
                return self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=self._default_temperature if temperature is None else temperature,
                    max_completion_tokens=(
                        self._default_max_tokens if max_tokens is None else max_tokens
                    ),
                    extra_body=extra_body,
                )
            except RateLimitError as exc:
                if attempt >= max_attempts - 1:
                    raise
                delay = self._retry_delay(exc, attempt)
                self._logger.warning(
                    "Groq rate limit reached; retrying in %.1f seconds "
                    "(attempt %s/%s)",
                    delay,
                    attempt + 1,
                    max_attempts,
                )
                time.sleep(delay)
        raise RuntimeError("unreachable Groq retry state")

    @staticmethod
    def _retry_delay(exc: RateLimitError, attempt: int) -> float:
        response = getattr(exc, "response", None)
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after is not None:
                try:
                    return max(1.0, float(retry_after))
                except ValueError:
                    pass

        match = re.search(r"try again in ([0-9.]+)s", str(exc), re.I)
        if match is not None:
            return max(1.0, float(match.group(1)) + 1.0)

        return min(60.0, 2.0**attempt)
