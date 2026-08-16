"""Recording what the pipeline spends, which is what RQ3 asks about."""

from __future__ import annotations

from thesis_rest_tester.domain.models import TokenUsage
from thesis_rest_tester.llm.base import LLMResponse, MockLLMClient
from thesis_rest_tester.llm.usage import UsageRecorder, recording_stage


class _Failing(MockLLMClient):
    def generate(self, *args, **kwargs):
        raise RuntimeError("the server went away")


def _client(*totals: tuple[int, int]) -> MockLLMClient:
    client = MockLLMClient(["{}"] * len(totals))
    calls = iter(totals)

    def generate(system_prompt, user_prompt, temperature=None, max_tokens=None, think=True):
        prompt_tokens, completion_tokens = next(calls)
        return LLMResponse(
            text="{}",
            token_usage=TokenUsage(
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
            ),
            model="m",
        )

    client.generate = generate  # type: ignore[method-assign]
    return client


def test_usage_is_attributed_to_the_stage_that_spent_it() -> None:
    recorder = UsageRecorder(_client((10, 5), (20, 10)))

    with recording_stage("test_writer"):
        recorder.generate("s", "u")
    with recording_stage("test_strategy_planner"):
        recorder.generate("s", "u")

    assert recorder.stages["test_writer"].total_tokens == 15
    assert recorder.stages["test_strategy_planner"].total_tokens == 30
    assert recorder.totals.calls == 2


def test_a_call_that_raises_still_counts_its_time() -> None:
    """A request that timed out after two minutes cost those two minutes; omitting it
    would flatter the total."""

    recorder = UsageRecorder(_Failing([]))

    try:
        with recording_stage("api_understanding"):
            recorder.generate("s", "u")
    except RuntimeError:
        pass

    assert recorder.stages["api_understanding"].calls == 1
    assert recorder.totals.seconds >= 0.0


def test_calls_the_provider_reported_no_usage_for_are_counted_separately() -> None:
    """A token total that silently omitted them would understate the cost while looking
    complete."""

    client = MockLLMClient(["{}"])
    client.generate = lambda *a, **k: LLMResponse(text="{}", token_usage=None, model="m")  # type: ignore[method-assign]
    recorder = UsageRecorder(client)

    recorder.generate("s", "u")

    assert recorder.totals.calls_without_usage == 1
    assert recorder.totals.total_tokens == 0


def test_the_report_prices_the_run_at_the_rates_it_is_given() -> None:
    """Zero for the local model, which is the point: the figure says what the same
    pipeline would cost against a hosted API."""

    recorder = UsageRecorder(_client((1_000_000, 500_000)))
    with recording_stage("requirements_analyst"):
        recorder.generate("s", "u")

    report = recorder.report(prompt_cost_per_million=3.0, completion_cost_per_million=15.0)

    assert report["estimated_cost_usd"] == 10.5
    assert report["totals"]["total_tokens"] == 1_500_000
    assert "requirements_analyst" in report["by_stage"]
