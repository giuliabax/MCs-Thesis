from __future__ import annotations

import json

import httpx
import pytest

from thesis_rest_tester.llm.lmstudio_client import LMStudioConnectionError, LMStudioLLMClient


def _client(handler) -> LMStudioLLMClient:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return LMStudioLLMClient(model="test-model", http_client=http_client)


def test_lmstudio_client_returns_generated_text() -> None:
    captured_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured_payloads.append(payload)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                "model": payload["model"],
            },
        )

    client = _client(handler)
    response = client.generate("system", "user")

    assert response.text == '{"ok": true}'
    assert response.token_usage is not None
    assert response.token_usage.total_tokens == 3
    assert response.model == "test-model"
    assert "response_format" not in captured_payloads[0]
    assert captured_payloads[0]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
    ]


def test_lmstudio_client_strips_leading_think_block() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "<think>reasoning about the answer</think>\n"
                            '{"ok": true}'
                        }
                    }
                ],
            },
        )

    client = _client(handler)
    response = client.generate("system", "user")

    assert response.text == '{"ok": true}'


def test_lmstudio_client_retries_once_on_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}
    sleeps: list[float] = []
    monkeypatch.setattr("thesis_rest_tester.llm.lmstudio_client.time.sleep", sleeps.append)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    client = _client(handler)
    response = client.generate("system", "user")

    assert response.text == '{"ok": true}'
    assert calls["count"] == 2
    assert sleeps == [2.0]


def test_lmstudio_client_raises_actionable_error_when_server_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("thesis_rest_tester.llm.lmstudio_client.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(handler)

    with pytest.raises(LMStudioConnectionError, match="LM Studio"):
        client.generate("system", "user")


def test_lmstudio_client_retries_with_larger_max_tokens_when_truncated_empty() -> None:
    captured_max_tokens: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured_max_tokens.append(payload["max_tokens"])
        if len(captured_max_tokens) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "", "reasoning_content": "..."},
                         "finish_reason": "length"}
                    ]
                },
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}]},
        )

    client = _client(handler)
    response = client.generate("system", "user", max_tokens=100)

    assert response.text == '{"ok": true}'
    assert captured_max_tokens == [100, 150]


def test_lmstudio_client_raises_when_still_empty_after_escalation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
        )

    client = _client(handler)

    with pytest.raises(RuntimeError, match="empty response"):
        client.generate("system", "user", max_tokens=100)


def test_lmstudio_client_raises_on_http_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "model not loaded"}})

    client = _client(handler)

    with pytest.raises(RuntimeError, match="model not loaded"):
        client.generate("system", "user")
