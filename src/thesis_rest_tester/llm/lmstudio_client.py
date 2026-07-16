"""LM Studio implementation of the provider-neutral LLM client.

LM Studio exposes a local OpenAI-compatible REST server (Developer tab, default
http://localhost:1234/v1). No API key is required.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from thesis_rest_tester.domain.models import TokenUsage
from thesis_rest_tester.llm.base import LLMClient, LLMResponse

_DEFAULT_BASE_URL = "http://localhost:1234/v1"
_DEFAULT_TIMEOUT_SECONDS = 1200.0
_THINK_BLOCK = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


class LMStudioConnectionError(RuntimeError):
    """Raised when LM Studio's local server cannot be reached."""


class LMStudioLLMClient(LLMClient):
    def __init__(
        self,
        model: str,
        base_url: str = _DEFAULT_BASE_URL,
        default_temperature: float = 0.1,
        default_max_tokens: int = 4096,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens
        self._client = http_client if http_client is not None else httpx.Client(timeout=timeout)
        self._logger = logging.getLogger(__name__)

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        think: bool = True,
    ) -> LLMResponse:
        resolved_max_tokens = self._default_max_tokens if max_tokens is None else max_tokens
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        resolved_temperature = self._default_temperature if temperature is None else temperature

        # Reasoning models (Qwen3.5) can spend the whole token budget on the reasoning
        # phase and return empty content. Escalate the budget once; if reasoning still
        # runs away, fall back to a reasoning-off call so the model emits its answer
        # directly instead of failing the whole run. reasoning_effort="none" is the only
        # switch that actually suppresses reasoning on this server.
        attempts: list[tuple[int, bool]] = [
            (resolved_max_tokens, think),
            (int(resolved_max_tokens * 1.5), think),
        ]
        if think:
            attempts.append((resolved_max_tokens, False))

        body: dict[str, Any] = {}
        choice: dict[str, Any] = {}
        content = None
        for index, (attempt_max_tokens, attempt_think) in enumerate(attempts):
            payload: dict[str, Any] = {
                "model": self._model,
                "messages": messages,
                "temperature": resolved_temperature,
                "max_tokens": attempt_max_tokens,
                "stream": False,
            }
            if not attempt_think:
                payload["reasoning_effort"] = "none"
            body = self._post_with_retry(payload)
            choice = (body.get("choices") or [{}])[0]
            content = choice.get("message", {}).get("content")
            if content or choice.get("finish_reason") != "length":
                break
            following = attempts[index + 1] if index + 1 < len(attempts) else None
            if following is None:
                break
            if following[1]:
                self._logger.warning(
                    "LM Studio exhausted %d max_tokens on reasoning before producing content; "
                    "retrying with max_tokens=%d",
                    attempt_max_tokens,
                    following[0],
                )
            else:
                self._logger.warning(
                    "LM Studio exhausted %d max_tokens on reasoning twice; retrying once with "
                    "reasoning disabled so the model emits its answer directly",
                    attempt_max_tokens,
                )

        if not content:
            raise RuntimeError("LM Studio returned an empty response")
        content = _THINK_BLOCK.sub("", content, count=1)

        usage = body.get("usage")
        token_usage = None
        if usage is not None:
            token_usage = TokenUsage(
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
            )
        return LLMResponse(
            text=content,
            token_usage=token_usage,
            model=body.get("model", self._model),
        )

    def _post_with_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        max_attempts = 2
        url = f"{self._base_url}/chat/completions"
        for attempt in range(max_attempts):
            try:
                response = self._client.post(url, json=payload)
            except httpx.ConnectError as exc:
                if attempt >= max_attempts - 1:
                    raise LMStudioConnectionError(
                        f"Could not reach LM Studio at {self._base_url}. Open LM Studio, load "
                        f"model '{self._model}' in the Developer tab, and start the local server."
                    ) from exc
                self._logger.warning(
                    "LM Studio server not reachable yet at %s; retrying in 2 seconds", url
                )
                time.sleep(2.0)
                continue

            if response.status_code >= 400:
                message = self._error_message(response)
                raise RuntimeError(
                    f"LM Studio request failed with status {response.status_code}: {message}"
                )
            return response.json()
        raise RuntimeError("unreachable LM Studio retry state")

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str):
            return error
        return response.text
