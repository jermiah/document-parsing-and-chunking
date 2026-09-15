"""Validate wire format without paid model calls."""

import httpx
import pytest

from ingestion.vision import ModelClient


@pytest.mark.parametrize(
    "model,reasoning",
    [
        ("gpt-5.6-terra", True),
        ("gpt-5.4-mini", True),
        ("gpt-4.1-mini", False),
        ("example/non-reasoning-model", False),
    ],
)
def test_model_specific_request(monkeypatch, model, reasoning):
    def post(self, url, *, json, headers):
        if reasoning:
            assert json["max_completion_tokens"] == 4608
            assert json["reasoning_effort"] == "medium"
            assert "temperature" not in json and "max_tokens" not in json
        else:
            assert json["max_tokens"] == 512 and json["temperature"] == 0
            assert "reasoning_effort" not in json
        assert json["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", post)
    assert (
        ModelClient("https://example.test/v1", json_mode=True).complete(model, [], 512)[0]
        == '{"ok":true}'
    )
