from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from emike.config import LLMConfig

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ChatMessage:
    role: str
    content: str


class LLMClient:
    def __init__(self, config: LLMConfig, system_prompt: str) -> None:
        self._config = config
        self._system_prompt = system_prompt.strip()

    def _build_messages(self, history: list[ChatMessage]) -> list[dict[str, str]]:
        timestamp = datetime.now().strftime("%A, %B %d, %Y, %I:%M %p")
        system = (
            f"{self._system_prompt}\n"
            f"Current local date and time: {timestamp}."
        )
        return [{"role": "system", "content": system}] + [
            {"role": msg.role, "content": msg.content} for msg in history
        ]

    def complete(self, history: list[ChatMessage], request_id: str | None = None) -> str:
        api_key = os.getenv(self._config.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"Missing API key. Set env var: {self._config.api_key_env}"
            )

        body: dict[str, Any] = {
            "model": self._config.model,
            "messages": self._build_messages(history),
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        response = requests.post(
            self._config.endpoint,
            headers=headers,
            json=body,
            timeout=self._config.timeout_seconds,
        )
        response.raise_for_status()

        payload: dict[str, Any] = response.json()
        meta_extra: dict[str, object] = {
            "event": "llm_response_metadata",
            "model": self._config.model,
            "status_code": response.status_code,
            "payload_keys": sorted(payload.keys()),
        }
        if request_id is not None:
            meta_extra["request_id"] = request_id
        LOGGER.debug("LLM response metadata", extra=meta_extra)
        choices = payload.get("choices", [])
        if not choices:
            raise RuntimeError("LLM response did not include any choices")

        message = choices[0].get("message", {})
        if not isinstance(message, dict):
            raise TypeError("LLM response message payload is invalid")

        content = message.get("content", "")
        if not isinstance(content, str):
            raise TypeError("LLM response content is invalid")
        content = content.strip()
        if not content:
            finish_reason = ""
            first_choice = choices[0]
            if isinstance(first_choice, dict):
                finish_reason = str(first_choice.get("finish_reason", ""))
            empty_extra: dict[str, object] = {
                "event": "llm_empty_content",
                "finish_reason": finish_reason,
                "message_keys": sorted(message.keys()),
            }
            if request_id is not None:
                empty_extra["request_id"] = request_id
            LOGGER.debug("LLM content empty", extra=empty_extra)
            raise RuntimeError("LLM returned an empty response")
        return content
