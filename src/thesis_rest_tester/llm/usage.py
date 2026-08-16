"""Record what every model call costs, so the cost of the pipeline can be reported.

RQ3 asks what the feedback loop costs in tokens, time and computational effort. Each
agent already receives a token count with its response, but every stage discards it after
using the parsed result, so nothing accumulated and the question had no data behind it.

The accounting is done by wrapping the client rather than by threading a counter through
every agent and every stage. A wrapper sees every call by construction, including the
retries an agent makes internally -- an escalated budget after a truncated answer, or a
schema repair -- which is exactly what a cost figure has to include and what a count kept
by the caller would miss.

Which stage a call belongs to is not visible from inside the client, so the agent
announces it: ``BaseAgent`` enters ``recording_stage(name)`` around its call, and the
recorder attributes whatever happens inside to that name. A context variable rather than
an attribute, because it must survive a nested call without being reset by it.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from thesis_rest_tester.llm.base import LLMClient, LLMResponse

# The stage a model call belongs to: an agent name, and the project when there is one.
_current_stage: ContextVar[str] = ContextVar("llm_stage", default="unattributed")


@contextmanager
def recording_stage(name: str) -> Iterator[None]:
    token = _current_stage.set(name)
    try:
        yield
    finally:
        _current_stage.reset(token)


@dataclass
class StageUsage:
    """What one stage spent."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    # Calls the provider reported no usage for. Kept because a token total that silently
    # omits them would understate the cost while looking complete.
    calls_without_usage: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, float | int]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "seconds": round(self.seconds, 2),
            "calls_without_usage": self.calls_without_usage,
        }


@dataclass
class UsageRecorder(LLMClient):
    """An LLM client that forwards every call and remembers what it cost."""

    inner: LLMClient
    stages: dict[str, StageUsage] = field(default_factory=dict)

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        think: bool = True,
    ) -> LLMResponse:
        stage = _current_stage.get()
        usage = self.stages.setdefault(stage, StageUsage())
        started = time.perf_counter()
        try:
            response = self.inner.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                think=think,
            )
        finally:
            # Timed even when the call raises: a request that timed out after two minutes
            # cost those two minutes, and omitting it would flatter the total.
            usage.seconds += time.perf_counter() - started
            usage.calls += 1

        if response.token_usage is None:
            usage.calls_without_usage += 1
        else:
            usage.prompt_tokens += response.token_usage.prompt_tokens or 0
            usage.completion_tokens += response.token_usage.completion_tokens or 0
        return response

    # --- reporting ---------------------------------------------------------------------

    @property
    def totals(self) -> StageUsage:
        combined = StageUsage()
        for usage in self.stages.values():
            combined.calls += usage.calls
            combined.prompt_tokens += usage.prompt_tokens
            combined.completion_tokens += usage.completion_tokens
            combined.seconds += usage.seconds
            combined.calls_without_usage += usage.calls_without_usage
        return combined

    def report(self, *, prompt_cost_per_million: float = 0.0,
               completion_cost_per_million: float = 0.0) -> dict[str, object]:
        """A persistable summary.

        The cost is zero for the local model this study runs on, which is the point of
        reporting it: the figure answers what the same pipeline would cost against a
        hosted API, at rates the caller supplies.
        """

        totals = self.totals
        return {
            "totals": totals.as_dict(),
            "estimated_cost_usd": round(
                totals.prompt_tokens * prompt_cost_per_million / 1_000_000
                + totals.completion_tokens * completion_cost_per_million / 1_000_000,
                4,
            ),
            "by_stage": {
                name: usage.as_dict()
                for name, usage in sorted(
                    self.stages.items(), key=lambda item: -item[1].total_tokens
                )
            },
        }
