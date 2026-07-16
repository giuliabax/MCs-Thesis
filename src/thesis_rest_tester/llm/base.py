"""Provider-neutral LLM client interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from thesis_rest_tester.domain.models import TokenUsage


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    token_usage: TokenUsage | None = None
    model: str | None = None


class LLMClient(ABC):
    @abstractmethod
    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        think: bool = True,
    ) -> LLMResponse:
        """Generate one model response.

        ``think`` controls whether the model is allowed to use its reasoning phase;
        providers that expose no reasoning control ignore it.
        """


class MockLLMClient(LLMClient):
    """Return deterministic queued responses for dry runs and tests."""

    def __init__(self, responses: Iterable[str], model: str = "mock-llm") -> None:
        self._responses = deque(responses)
        self._model = model

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        think: bool = True,
    ) -> LLMResponse:
        del system_prompt, user_prompt, temperature, max_tokens, think
        if not self._responses:
            raise RuntimeError("MockLLMClient has no responses remaining")
        return LLMResponse(
            text=self._responses.popleft(),
            token_usage=TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            model=self._model,
        )
