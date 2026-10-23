from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

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

    def complete(
        self,
        history: list[ChatMessage],
        request_id: str | None = None,
        tool_router: Any | None = None,
    ) -> str:
        api_key = os.getenv(self._config.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"Missing API key. Set env var: {self._config.api_key_env}"
            )

        messages: list[dict[str, Any]] = self._build_messages(history)
        tools = tool_router.get_tool_definitions() if tool_router is not None else []
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        max_rounds = 6
        for _ in range(max_rounds):
            body: dict[str, Any] = {
                "model": self._config.model,
                "messages": messages,
                "temperature": self._config.temperature,
                "max_tokens": self._config.max_tokens,
            }
            if tools:
                body["tools"] = tools

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

            choice = choices[0]
            if not isinstance(choice, dict):
                raise TypeError("LLM response choice payload is invalid")

            message = choice.get("message", {})
            if not isinstance(message, dict):
                raise TypeError("LLM response message payload is invalid")

            tool_calls = message.get("tool_calls", [])
            if isinstance(tool_calls, list) and tool_calls and tool_router is not None:
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content", ""),
                        "tool_calls": tool_calls,
                    }
                )
                for tool_call in tool_calls:
                    tool_result = self._execute_tool_call(tool_call, tool_router, request_id)
                    messages.append(tool_result)
                continue

            content = message.get("content", "")
            if not isinstance(content, str):
                raise TypeError("LLM response content is invalid")
            content = content.strip()
            if content:
                return content

            finish_reason = str(choice.get("finish_reason", ""))
            empty_extra: dict[str, object] = {
                "event": "llm_empty_content",
                "finish_reason": finish_reason,
                "message_keys": sorted(message.keys()),
            }
            if request_id is not None:
                empty_extra["request_id"] = request_id
            LOGGER.debug("LLM content empty", extra=empty_extra)
            raise RuntimeError("LLM returned an empty response")

        raise RuntimeError("LLM tool-calling exceeded max rounds")

    def _execute_tool_call(
        self,
        tool_call: Any,
        tool_router: Any,
        request_id: str | None,
    ) -> dict[str, str]:
        if not isinstance(tool_call, dict):
            raise TypeError("Tool call payload is invalid")

        tool_call_id = str(tool_call.get("id", "unknown"))
        function_payload = tool_call.get("function", {})
        if not isinstance(function_payload, dict):
            raise TypeError("Tool call function payload is invalid")

        tool_name = str(function_payload.get("name", ""))
        arguments_raw = function_payload.get("arguments", "{}")
        args: dict[str, Any] = {}
        if isinstance(arguments_raw, str):
            try:
                parsed = json.loads(arguments_raw)
                if isinstance(parsed, dict):
                    args = parsed
            except json.JSONDecodeError:
                args = {}
        elif isinstance(arguments_raw, dict):
            args = cast(dict[str, Any], arguments_raw)

        tool_extra: dict[str, object] = {
            "event": "llm_tool_call",
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "args": args,
        }
        if request_id is not None:
            tool_extra["request_id"] = request_id
        LOGGER.debug("Executing tool call", extra=tool_extra)

        result = tool_router.execute_tool_call(tool_name, args)
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result,
        }
