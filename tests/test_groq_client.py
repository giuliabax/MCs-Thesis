from __future__ import annotations

from types import SimpleNamespace

import httpx
from groq import RateLimitError

from thesis_rest_tester.llm.groq_client import GroqLLMClient


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
            response = httpx.Response(
                429,
                request=request,
                headers={"retry-after": "1"},
                json={"error": {"message": "rate limit"}},
            )
            raise RateLimitError("rate limit", response=response, body=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3),
            model=kwargs["model"],
        )


class _FakeGroq:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


def test_groq_client_retries_rate_limits(monkeypatch) -> None:
    fake = _FakeGroq()
    sleeps: list[float] = []
    monkeypatch.setattr("thesis_rest_tester.llm.groq_client.time.sleep", sleeps.append)
    client = GroqLLMClient(model="test-model", sdk_client=fake)

    response = client.generate("system", "user")

    assert response.text == '{"ok": true}'
    assert response.token_usage is not None
    assert response.token_usage.total_tokens == 3
    assert fake.completions.calls == 2
    assert sleeps == [1.0]
