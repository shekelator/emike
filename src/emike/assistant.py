from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field

import requests

from emike.audio import AudioRecorder
from emike.button import ButtonInterface
from emike.config import AppConfig
from emike.llm import ChatMessage, LLMClient
from emike.stt import SpeechToText
from emike.tools import ToolRouter
from emike.tts import TextToSpeech

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ConversationState:
    history: list[ChatMessage] = field(default_factory=list)
    last_turn_timestamp: float = 0.0


class AssistantRuntime:
    def __init__(
        self,
        config: AppConfig,
        button: ButtonInterface,
    ) -> None:
        self._config = config
        self._button = button
        self._audio = AudioRecorder()
        self._stt = SpeechToText(config.stt)
        self._llm = LLMClient(config.llm, config.system_prompt)
        self._tts = TextToSpeech(config.tts.voice_model_path)
        self._tools = ToolRouter(config, on_timer_elapsed=self._on_timer_elapsed)
        self._state = ConversationState()

    def _speak(self, text: str, request_id: str | None = None) -> None:
        if self._config.tts.enabled:
            try:
                self._tts.speak(text)
            except (FileNotFoundError, RuntimeError, OSError, requests.RequestException) as exc:
                extra: dict[str, str] = {"event": "tts_error", "error": str(exc)}
                if request_id is not None:
                    extra["request_id"] = request_id
                LOGGER.error("TTS failure", extra=extra)

    def _on_timer_elapsed(self, message: str) -> None:
        LOGGER.info("Timer elapsed", extra={"event": "timer_elapsed", "text": message})
        self._speak(message)

    def _prune_history(self) -> None:
        max_messages = self._config.history_turns * 2
        self._state.history = self._state.history[-max_messages:]

    def _reset_history_if_idle(self) -> None:
        if self._state.last_turn_timestamp == 0:
            return
        idle_for = time.time() - self._state.last_turn_timestamp
        if idle_for > self._config.conversation_timeout_seconds:
            self._state.history = []

    def run_forever(self) -> None:
        LOGGER.info("Assistant ready", extra={"event": "assistant_ready"})
        while True:
            self._button.wait_for_press()
            request_id = uuid.uuid4().hex[:10]
            LOGGER.debug(
                "Interaction started",
                extra={"event": "interaction_start", "request_id": request_id},
            )
            audio = self._audio.record_while_button_held(self._button)

            user_text = self._stt.transcribe(audio)
            if not user_text:
                LOGGER.info(
                    "No speech recognized",
                    extra={"event": "stt_empty", "request_id": request_id},
                )
                continue

            LOGGER.info(
                "User input",
                extra={"event": "user_text", "request_id": request_id, "text": user_text},
            )

            self._reset_history_if_idle()
            self._state.history.append(ChatMessage(role="user", content=user_text))

            try:
                tool_reply = self._tools.maybe_handle(user_text)
                if tool_reply is not None:
                    reply = tool_reply
                else:
                    reply = self._llm.complete(self._state.history, request_id=request_id)
            except (RuntimeError, requests.RequestException) as exc:
                LOGGER.error(
                    "Assistant generation error",
                    extra={
                        "event": "assistant_error",
                        "request_id": request_id,
                        "error": str(exc),
                    },
                )
                fallback = "Sorry, I hit an error while generating a response."
                self._speak(fallback, request_id=request_id)
                continue

            self._state.history.append(ChatMessage(role="assistant", content=reply))
            self._prune_history()
            self._state.last_turn_timestamp = time.time()

            LOGGER.info(
                "Assistant output",
                extra={"event": "assistant_text", "request_id": request_id, "text": reply},
            )
            self._speak(reply, request_id=request_id)
