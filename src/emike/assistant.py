"""Assistant runtime built on Amazon Nova Sonic bidirectional streaming.

One asyncio event loop drives everything. Threaded hardware (gpiozero/keyboard,
PortAudio) crosses into the loop through two narrow bridges:

- Button presses are awaited via ``asyncio.to_thread``.
- Button releases feed a single "release pump" thread that increments a
  counter; mic streaming compares the counter between chunks. A turn that is
  cancelled (barge-in) can therefore never strand release tokens and wedge
  the next turn.

Each button press opens one fresh Nova Sonic session, so a failed or
interrupted turn reconnects automatically on the next press.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from emike.audio import MicStreamer
from emike.button import ButtonInterface
from emike.config import AppConfig
from emike.led import LEDController, LEDState
from emike.playback import PlaybackQueue
from emike.sonic_client import SonicClient
from emike.sonic_events import (
    build_audio_content_end,
    build_audio_content_start,
    build_audio_input,
    build_prompt_end,
    build_prompt_start,
    build_session_end,
    build_session_start,
    build_system_prompt_events,
    build_tool_result_events,
    classify_output_event,
    encode_pcm_chunk,
    extract_audio_output,
    extract_content_end,
    extract_content_start,
    extract_interrupted,
    extract_text_output,
    extract_tool_use,
    peak_pcm_amplitude,
)
from emike.sonic_session import SonicTurnSession
from emike.tools import ToolRouter

LOGGER = logging.getLogger(__name__)


class ReleaseCounter:
    """Button releases as an ever-increasing counter, from one pump thread."""

    def __init__(self, button: ButtonInterface) -> None:
        self._button = button
        self._count = 0
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._pump, name="release-pump", daemon=True).start()

    def _pump(self) -> None:
        while True:
            self._button.wait_for_release()
            with self._lock:
                self._count += 1

    def count(self) -> int:
        with self._lock:
            return self._count


class AssistantRuntime:
    def __init__(
        self,
        config: AppConfig,
        button: ButtonInterface,
        led: LEDController,
    ) -> None:
        self._config = config
        self._button = button
        self._led = led
        self._client = SonicClient(config.sonic)
        self._tools = ToolRouter(config)
        self._releases = ReleaseCounter(button)
        self._active: asyncio.Task[None] | None = None

    def run_forever(self) -> None:
        """Blocking entry point used by the CLI."""
        asyncio.run(self._run())

    async def _run(self) -> None:
        LOGGER.info("Assistant ready", extra={"event": "assistant_ready"})
        self._releases.start()
        while True:
            self._led.set_state(LEDState.IDLE)
            await asyncio.to_thread(self._button.wait_for_press)
            request_id = uuid.uuid4().hex[:10]
            active, self._active = self._active, None
            if active is not None:
                # A press during playback or processing is barge-in: the same
                # press ends the old turn immediately and starts the new one.
                LOGGER.debug(
                    "Interrupting active turn",
                    extra={"event": "turn_interrupt", "request_id": request_id},
                )
                active.cancel()
                await asyncio.gather(active, return_exceptions=True)
            self._active = asyncio.create_task(self._run_turn(request_id))

    async def _run_turn(self, request_id: str) -> None:
        config = self._config.sonic
        self._led.set_state(LEDState.LISTENING)
        LOGGER.info(
            "Turn started — listening",
            extra={"event": "turn_started", "request_id": request_id},
        )
        mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        mic = MicStreamer(
            asyncio.get_running_loop(),
            mic_queue,
            config.input_sample_rate_hz,
            config.chunk_ms,
        )
        playback = PlaybackQueue(config.output_sample_rate_hz, config.prebuffer_ms)
        prompt_name = uuid.uuid4().hex
        content_name = uuid.uuid4().hex
        baseline = self._releases.count()
        started = time.monotonic()
        session: SonicTurnSession | None = None
        try:
            opened = await asyncio.wait_for(
                self._open_session(), timeout=config.open_timeout_seconds
            )
            session, model_id = opened
            LOGGER.info(
                "Nova Sonic session opened",
                extra={
                    "event": "session_opened",
                    "request_id": request_id,
                    "model": model_id,
                },
            )
            await session.send_events(
                [
                    build_session_start(
                        config.max_tokens,
                        config.top_p,
                        config.temperature,
                        config.endpointing_sensitivity,
                    ),
                    build_prompt_start(
                        prompt_name,
                        config.voice_id,
                        config.output_sample_rate_hz,
                        self._tools.get_nova_tool_definitions(),
                    ),
                    *build_system_prompt_events(
                        prompt_name, uuid.uuid4().hex, self._system_prompt()
                    ),
                    build_audio_content_start(
                        prompt_name, content_name, config.input_sample_rate_hz
                    ),
                ]
            )
            mic.start()
            playback.start()
            LOGGER.debug(
                "Recording — mic streaming into session",
                extra={"event": "recording_started", "request_id": request_id},
            )
            # Sender and receiver must overlap: the service may hold back its
            # initial response until input starts flowing.
            sender = asyncio.ensure_future(
                self._stream_mic(
                    session, mic_queue, baseline, prompt_name, content_name, request_id
                )
            )
            try:
                await asyncio.wait_for(
                    self._receive(session, playback, request_id, prompt_name),
                    timeout=config.turn_timeout_seconds,
                )
                await asyncio.to_thread(playback.wait_until_drained, 30.0)
            finally:
                sender.cancel()
            LOGGER.info(
                "Turn completed",
                extra={
                    "event": "turn_completed",
                    "request_id": request_id,
                    "duration_seconds": round(time.monotonic() - started, 2),
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a turn error must not kill the service
            LOGGER.error(
                "Turn failed",
                extra={
                    "event": "turn_error",
                    "request_id": request_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "duration_seconds": round(time.monotonic() - started, 2),
                },
            )
            self._led.set_state(LEDState.ERROR)
        finally:
            # Dropping everything here is what makes barge-in instant: the mic
            # stops feeding and any audio still queued for playback discards.
            mic.stop()
            playback.stop()
            if session is not None:
                await self._close_session(session, prompt_name)

    async def _open_session(self) -> tuple[SonicTurnSession, str]:
        duplex, model_id = await self._client.open_stream()
        return SonicTurnSession(duplex), model_id

    async def _close_session(self, session: SonicTurnSession, prompt_name: str) -> None:
        """Send the documented end-of-session sequence, then close the stream.

        Skipping promptEnd/sessionEnd can orphan server-side resources, so do
        both even when the turn failed; every failure is best-effort.
        """
        try:
            await session.send_event(build_prompt_end(prompt_name))
            await session.send_event(build_session_end())
        except Exception:
            LOGGER.debug("Error closing Nova Sonic session", exc_info=True)
        await session.close()

    async def _stream_mic(
        self,
        session: SonicTurnSession,
        mic_queue: asyncio.Queue[bytes],
        baseline: int,
        prompt_name: str,
        content_name: str,
        request_id: str,
    ) -> None:
        """Stream mic audio until button release, then end the user turn."""
        sent = 0
        peak = 0
        warned_silent = False
        report_every = max(1, 1000 // self._config.sonic.chunk_ms)  # ~1 report/second
        while self._releases.count() == baseline:
            try:
                chunk = await asyncio.wait_for(mic_queue.get(), timeout=0.05)
            except TimeoutError:
                continue
            await session.send_event(
                build_audio_input(prompt_name, content_name, encode_pcm_chunk(chunk))
            )
            sent += 1
            level = peak_pcm_amplitude(chunk)
            peak = max(peak, level)
            if sent % report_every == 0:
                LOGGER.debug(
                    "Recording",
                    extra={
                        "event": "mic_stream",
                        "request_id": request_id,
                        "chunks_sent": sent,
                        "peak_amplitude": peak,
                    },
                )
                if peak < 100 and not warned_silent:
                    warned_silent = True
                    LOGGER.warning(
                        "Microphone input is silent — check the input device and "
                        "microphone permissions (macOS: Settings > Privacy & Security > "
                        "Microphone)",
                        extra={"event": "mic_silent", "request_id": request_id},
                    )
        LOGGER.debug(
            "Button released; ending user turn",
            extra={
                "event": "mic_released",
                "request_id": request_id,
                "chunks_sent": sent,
                "peak_amplitude": peak,
            },
        )
        # The model's endpointing needs a beat of audio after speech stops
        # before it commits the turn; release coincides with end of speech, so
        # stream a short silence tail before closing the content.
        chunk_ms = self._config.sonic.chunk_ms
        samples_per_chunk = int(
            self._config.sonic.input_sample_rate_hz * chunk_ms / 1000
        )
        silence = encode_pcm_chunk(b"\x00\x00" * samples_per_chunk)
        for _ in range(max(1, self._config.sonic.release_tail_ms // chunk_ms)):
            await asyncio.sleep(chunk_ms / 1000)
            await session.send_event(
                build_audio_input(prompt_name, content_name, silence)
            )
        await session.send_event(build_audio_content_end(prompt_name, content_name))

    async def _receive(
        self,
        session: SonicTurnSession,
        playback: PlaybackQueue,
        request_id: str,
        prompt_name: str,
    ) -> None:
        """Drive the loop until one model completion finishes."""
        async for event in session.output_events():
            kind = classify_output_event(event)
            self._log_output_event_trace(kind, event)
            if kind == "content_start":
                content_start = extract_content_start(event)
                if content_start.get("type") == "AUDIO":
                    self._led.set_state(LEDState.SPEAKING)
                elif content_start.get("type") == "TOOL":
                    self._led.set_state(LEDState.THINKING)
            elif kind == "audio_output":
                playback.put_pcm(base64.b64decode(extract_audio_output(event)))
            elif kind == "text_output":
                self._on_text_output(event, request_id, playback)
            elif kind == "tool_use":
                await self._handle_tool_use(session, event, request_id, prompt_name)
            elif kind == "content_end":
                content_end = extract_content_end(event)
                stop_reason = content_end.get("stopReason")
                if stop_reason == "INTERRUPTED":
                    playback.clear()
                elif stop_reason == "END_TURN":
                    # The service's completion ends with the AUDIO contentEnd
                    # (stopReason END_TURN); completionEnd events are not
                    # reliably sent, so this closes the receive loop.
                    return
            elif kind == "completion_end":
                return

    def _log_output_event_trace(self, kind: str, event: dict[str, Any]) -> None:
        """One compact DEBUG line per Nova Sonic output event, for debugging."""
        if not LOGGER.isEnabledFor(logging.DEBUG):
            return
        extra: dict[str, object] = {}
        try:
            if kind == "content_start":
                content_start = extract_content_start(event)
                extra = {
                    key: content_start[key]
                    for key in ("type", "role", "generationStage")
                    if content_start.get(key)
                }
            elif kind == "content_end":
                extra = {"stopReason": extract_content_end(event).get("stopReason")}
            elif kind == "text_output":
                text_output = extract_text_output(event)
                content = str(text_output.get("content", ""))
                extra = {"role": text_output.get("role"), "chars": len(content)}
            elif kind == "tool_use":
                extra = {"tool": extract_tool_use(event).get("toolName")}
            elif kind == "audio_output":
                payload = str(extract_audio_output(event))
                extra = {"audio_bytes": len(payload) * 3 // 4}
            LOGGER.debug(
                "Sonic event",
                extra={"event": "sonic_event", "kind": kind, **extra},
            )
        except Exception:  # noqa: BLE001 - the debug trace must never break the turn
            LOGGER.debug("Sonic event", extra={"event": "sonic_event", "kind": kind})

    def _on_text_output(
        self, event: dict[str, Any], request_id: str, playback: PlaybackQueue
    ) -> None:
        text_output = extract_text_output(event)
        content = str(text_output.get("content", ""))
        role = str(text_output.get("role", ""))
        if extract_interrupted(content):
            playback.clear()
            return
        if not content.strip():
            return
        if role == "USER":
            LOGGER.info(
                "User input",
                extra={"event": "user_text", "request_id": request_id, "text": content},
            )
        elif role == "ASSISTANT":
            LOGGER.info(
                "Assistant output",
                extra={"event": "assistant_text", "request_id": request_id, "text": content},
            )

    async def _handle_tool_use(
        self,
        session: SonicTurnSession,
        event: dict[str, Any],
        request_id: str,
        prompt_name: str,
    ) -> None:
        tool_use = extract_tool_use(event)
        tool_name = str(tool_use.get("toolName", ""))
        tool_use_id = str(tool_use.get("toolUseId", ""))
        raw_args = str(tool_use.get("content", "") or "{}")
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            args = {}
        LOGGER.info(
            "Tool call",
            extra={"event": "tool_call", "request_id": request_id, "tool": tool_name},
        )
        try:
            result = await asyncio.to_thread(self._tools.execute_tool_call, tool_name, args)
            payload: dict[str, str] = {"result": result}
        except Exception as exc:  # noqa: BLE001 - the model needs a toolResult even on failure
            payload = {"error": str(exc)}
            LOGGER.error(
                "Tool execution failed",
                extra={
                    "event": "tool_error",
                    "request_id": request_id,
                    "tool": tool_name,
                    "error": str(exc),
                },
            )
        await session.send_events(
            build_tool_result_events(
                prompt_name=prompt_name,
                content_name=uuid.uuid4().hex,
                tool_use_id=tool_use_id,
                payload=payload,
            )
        )

    def _system_prompt(self) -> str:
        """Config prompt plus current local time, prepended to all session input."""
        now = datetime.now().astimezone()
        timestamp = now.strftime("Current local date and time: %A, %B %d, %Y %I:%M %p %Z")
        return f"{self._config.system_prompt.strip()}\n{timestamp}"
