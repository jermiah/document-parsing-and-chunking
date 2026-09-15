"""Bounded OpenAI-compatible inference client; document content is untrusted evidence."""

import base64
import time
from pathlib import Path
from typing import Any

import httpx

from ingestion.config import ProcessingConfig, Settings


def image_message(path: Path) -> dict[str, Any]:
    return {
        "type": "image_url",
        "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
        },
    }


class ModelClient:
    def __init__(
        self,
        url: str,
        key: str = "",
        timeout: float = 120,
        retries: int = 2,
        json_mode: bool = False,
    ):
        self.url, self.key, self.timeout, self.retries = url.rstrip("/"), key, timeout, retries
        self.json_mode = json_mode

    def preflight(self) -> dict:
        response = httpx.get(
            self.url + "/models", headers={"Authorization": "Bearer " + self.key}, timeout=10
        )
        response.raise_for_status()
        return response.json()

    def complete(
        self, model: str, messages: list[dict], max_tokens: int, extra: dict | None = None
    ) -> tuple[str, dict]:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            **(extra or {}),
        }
        if model.startswith(("gpt-5", "gpt-6")):
            # Reasoning models count internal reasoning in the completion limit.
            # Leave sampling at the model default rather than forcing temperature=0.
            payload.pop("temperature", None)
            payload.pop("max_tokens", None)
            payload.setdefault("reasoning_effort", "medium")
            payload.setdefault("max_completion_tokens", max_tokens + 4096)
        if self.json_mode and "response_format" not in payload:
            payload["response_format"] = {"type": "json_object"}
        for attempt in range(self.retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(
                        self.url + "/chat/completions",
                        json=payload,
                        headers={"Authorization": "Bearer " + self.key},
                    )
                response.raise_for_status()
                data = response.json()
                choice = data["choices"][0]
                if choice["message"].get("refusal"):
                    raise ValueError("Model declined the annotation request")
                if choice.get("finish_reason") == "length":
                    raise ValueError("Model output truncated at token limit")
                if (
                    not isinstance(choice["message"].get("content"), str)
                    or not choice["message"]["content"].strip()
                ):
                    raise ValueError("Model returned no usable text output")
                return choice["message"]["content"], data
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                retryable = not isinstance(
                    exc, httpx.HTTPStatusError
                ) or exc.response.status_code in {429, 500, 502, 503, 504}
                if attempt == self.retries or not retryable:
                    raise
                time.sleep(min(2**attempt, 4))
        raise RuntimeError("Inference retries exhausted")


def annotation_client(settings: Settings, config: ProcessingConfig) -> ModelClient:
    key = settings.openai_api_key.get_secret_value().strip()
    if not key:
        raise ValueError("Set OPENAI_API_KEY in the local .env before enabling OpenAI annotations")
    return ModelClient(
        "https://api.openai.com/v1", key, config.request_timeout, config.retries, json_mode=True
    )
