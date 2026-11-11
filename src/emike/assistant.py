"""Assistant runtime built on Amazon Nova Sonic bidirectional streaming.

One asyncio event loop drives everything. Threaded hardware (gpiozero/keyboard,
PortAudio, the Telegram poller) crosses into the loop through narrow bridges
that feed one wake bus:

- Button presses come from a pump thread into the bus; the runtime loop parks
  on the bus instead of parking in ``to_thread(button.wait_for_press)``, so a
  press can reach the loop while any other work (announcement, long drain) is
  in flight.
- Button releases feed a "release pump" thread that increments a counter;
  mic streaming compares the counter between chunks. A turn that is cancelled
  (barge-in) can therefore never strand release tokens and wedge the next turn.
- Phone messages land from the Telegram poller thread; an idle runtime
  announces them over a mic-less Nova Sonic session (see `_run_announcement`).

Each button press opens one fresh Nova Sonic session, so a failed or
interrupted turn reconnects automatically on the next press.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from emike.audio import MicStreamer
from emike.button import ButtonInterface
from emike.chime import build_message_chime
from emike.config import AppConfig
from emike.led import LEDController, LEDState
from emike.messaging import (
    DEFAULT_OFFSET_PATH,
    IncomingMessage,
    MessageTransport,
    TelegramPoller,
    TelegramTransport,
)
from emike.playback import PlaybackQueue
from emike.sonic_client import SonicClient, exc_message
from emike.sonic_events import (
    build_audio_content_end,
    build_audio_content_start,
    build_audio_input,
    build_prompt_end,
    build_prompt_start,
    build_session_end,
    build_system_prompt_events,
    build_tool_result_events,
    build_user_text_events,
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
from emike.timer_alarm import build_timer_alarm_pcm
from emike.tools import ActiveTimer, ToolRouter

LOGGER = logging.getLogger(__name__)

# How long the announcement session streams silence before the cross-modal
# text is injected (the docs require an actively streaming session; this has
# been verified to work at half a second on amazon.nova-2-5-sonic).
ANNOUNCE_TEXT_DELAY_SECONDS = 0.5
# Whole-script cap (~same budget as the outbound Telegram message).
MAX_ANNOUNCEMENT_CHARS = 4000
# Failed announcement attempts kept per message before it is dropped. A press
# barge-in counts as one attempt: without a cap, pressing mid-announcement
# would ding and retry forever.
MAX_ANNOUNCE_ATTEMPTS = 3
_ANNOUNCE_ORDINALS = (
    "First",
    "Second",
    "Third",
    "Fourth",
    "Fifth",
    "Sixth",
    "Seventh",
    "Eighth",
    "Ninth",
    "Tenth",
)


@dataclass(frozen=True, slots=True)
class PressWake:
    """A button press (GPIO on the Pi, Enter in keyboard mode)."""


@dataclass(frozen=True, slots=True)
class MessageWake:
    """A non-press nudge: a phone message buffered, or `_active` finished."""


PRESS_WAKE = PressWake()
MESSAGE_WAKE = MessageWake()
Wake = PressWake | MessageWake


class WakeBus:
    """Press and message wakes from threads into the runtime loop.

    ``wait()`` is the runtime loop's park point — the replacement for
    ``await asyncio.to_thread(button.wait_for_press)``. ``emit_*`` is safe from
    any thread: off the loop it crosses with ``call_soon_threadsafe``, and a
    closed running loop is ignored so shutdown races cannot crash the pump or
    poller threads late in the process's life.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        # The queue must be created on the running loop; attach() does that.
        self._queue: asyncio.Queue[Wake] | None = None

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the bus to the running loop (called once from `_run`)."""
        self._loop = loop
        self._queue = asyncio.Queue()

    def emit_press(self) -> None:
        self._emit(PRESS_WAKE)

    def emit_message(self) -> None:
        self._emit(MESSAGE_WAKE)

    def _emit(self, wake: Wake) -> None:
        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            # Already on the loop thread (e.g. a done callback): queue directly.
            queue.put_nowait(wake)
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, wake)
        except RuntimeError:
            pass  # the loop closed during shutdown; the thread is on its way out

    async def wait(self) -> Wake:
        queue = self._queue
        if queue is None:
            raise RuntimeError("WakeBus used before attach()")
        return await queue.get()


class ButtonWakePump:
    """Button presses from one daemon thread into the wake bus.

    Mirrors ``ReleaseCounter`` (which owns ``wait_for_release`` the same way):
    the thread only blocks in ``wait_for_press`` and then emits one wake. The
    runtime loop parks on the bus instead of parking in an executor, so a
    press always reaches the loop — even during an announcement, which is what
    makes barge-in cancel it.

    The thread is a daemon and deliberately NOT joined at shutdown:
    ``wait_for_press`` parks with no timeout (a thread-safe acquire / gpiozero's
    own wait) and has no stop hook, so an unconditional join is exactly the old
    process-exit hang where ``asyncio.run`` waited up to 300 s for a parked
    executor thread. cli.py's ``os._exit(0)`` on Ctrl-C tears the process down;
    a parked daemon simply dies with it. When a KeyboardButton reader hits EOF
    it releases no further presses either, so this thread stays parked (idle,
    not spinning) until then.
    """

    def __init__(self, button: ButtonInterface, wakes: WakeBus) -> None:
        self._button = button
        self._wakes = wakes

    def start(self) -> None:
        threading.Thread(target=self._pump, name="button-wake-pump", daemon=True).start()

    def _pump(self) -> None:
        while True:
            self._button.wait_for_press()
            self._wakes.emit_press()


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


def _quote_safe(text: str) -> str:
    """Flatten a phone message into one line and keep the quote frame intact."""
    return " ".join(text.split()).replace('"', "'")


def build_announcement_script(sender_name: str, texts: list[str]) -> str:
    """The announcement prompt (design §3.3: verbatim-message policy).

    Messages travel inside the script as quoted data — the model is told to
    read them and add nothing else about the phone conversation. The whole
    script is capped at ``MAX_ANNOUNCEMENT_CHARS``; each message gets a share
    of the room so a full inbox announces everything (trimming a long message
    instead of dropping it from the batch). Announcing the frame first matters
    in practice: without it the model sometimes opens with the message body,
    which is confusing with zero context ("a message from Dad" tells the
    household what just dinged).
    """
    count = len(texts)
    plural = "" if count == 1 else "s"
    header = (
        f"Announce to the household that you have {count} new message{plural} from "
        f"{sender_name} on their phone — say that before anything else. Then read "
        "each one aloud, verbatim — do not summarize, answer, or comment on them. "
        "Do not reveal anything else about the phone conversation. After reading "
        "them, say you're caught up and close."
    )
    # ~30 characters of label + quote frame per message on top of the text.
    per_message = max(200, (MAX_ANNOUNCEMENT_CHARS - len(header) - 30 * count) // count)
    lines = []
    for index, text in enumerate(texts):
        label = (
            f"{_ANNOUNCE_ORDINALS[index]} message"
            if index < len(_ANNOUNCE_ORDINALS)
            else f"Message {index + 1}"
        )
        lines.append(f'{label}: "{_quote_safe(text)[:per_message]}"')
    return "\n".join([header, *lines])


class AssistantRuntime:
    def __init__(
        self,
        config: AppConfig,
        button: ButtonInterface,
        led: LEDController,
        conversation: bool = False,
    ) -> None:
        """``conversation=True``: the first press opens one Nova Sonic session
        and the model's endpointing decides turn boundaries itself; the next
        press ends the whole conversation. Push-to-talk (default) ends the
        user turn on release, one session per press."""
        self._config = config
        self._button = button
        self._led = led
        self._conversation = conversation
        self._client = SonicClient(config.sonic)
        self._inbox: list[IncomingMessage] = []
        self._telegram_transport: MessageTransport | None = None
        self._poller: TelegramPoller | None = None
        self._init_messaging()
        self._tools = ToolRouter(
            config, on_timer_elapsed=self._on_timer_elapsed, transport=self._telegram_transport
        )
        self._releases = ReleaseCounter(button)
        self._wakes = WakeBus()
        self._press_pump = ButtonWakePump(button, self._wakes)
        self._active: asyncio.Task[None] | None = None
        # What `_active` is: only a turn/conversation may be interrupted by a
        # newly arrived message (`announce_during_conversation`); an
        # announcement never is. Set together with `_active` in `_spawn_active`.
        self._active_is_announcement = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._alarm_task: asyncio.Task[None] | None = None
        self._announce_attempts: dict[int, int] = {}

    def run_forever(self) -> None:
        """Blocking entry point used by the CLI."""
        try:
            asyncio.run(self._run())
        finally:
            poller = self._poller
            if poller is not None:
                # Bounded best-effort: a long-poll holding the socket may not
                # release within the join; the thread is daemon either way.
                poller.stop(join_timeout=5.0)

    def _init_messaging(self) -> None:
        """Poll allowlisted chats into a bounded inbox (the wake-and-announce
        consumer reads it — see `_run_announcement`).

        When messaging is disabled or no bot token is present nothing starts
        and the inbox simply stays empty.
        """
        messaging = self._config.messaging
        if not messaging.enabled:
            return
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token.strip():
            LOGGER.warning(
                "Messaging enabled but TELEGRAM_BOT_TOKEN is not set — starting without it",
                extra={"event": "messaging_disabled", "reason": "missing_token"},
            )
            return
        transport = TelegramTransport(
            token, poll_timeout_seconds=messaging.poll_timeout_seconds
        )
        self._telegram_transport = transport
        self._poller = TelegramPoller(
            transport,
            on_message=self._on_telegram_message,
            allowlist=(str(chat_id) for chat_id in messaging.chat_allowlist),
            offset_path=DEFAULT_OFFSET_PATH,
        )
        LOGGER.info(
            "Messaging poller wired",
            extra={
                "event": "messaging_started",
                "allowlist_count": len(messaging.chat_allowlist),
            },
        )

    def _on_telegram_message(self, message: IncomingMessage) -> None:
        """Loop-side landing for one allowlisted phone message.

        Runs on the event loop thread only (the poller hands messages over
        with call_soon_threadsafe), so the inbox needs no lock. Message text
        is never logged. The wake tells an idle runtime to announce
        immediately; while a turn or conversation is active the message waits
        for it to end — or, with ``announce_during_conversation`` on, the wake
        interrupts it (never an announcement in progress).
        """
        cap = self._config.messaging.max_announcements
        if len(self._inbox) >= cap:
            dropped = self._inbox.pop(0)
            LOGGER.info(
                "Inbox full — dropping the oldest pending message",
                extra={
                    "event": "inbox_full",
                    "dropped_update_id": dropped.update_id,
                    "chat_id": dropped.chat_id,
                },
            )
        self._inbox.append(message)
        LOGGER.info(
            "Phone message received",
            extra={
                "event": "message_received",
                "chat_id": message.chat_id,
                "chars": len(message.text),
                "update_id": message.update_id,
                "pending": len(self._inbox),
            },
        )
        self._wakes.emit_message()

    def drain_inbox(self) -> list[IncomingMessage]:
        """Take all pending phone messages (event loop thread only)."""
        messages = self._inbox
        self._inbox = []
        return messages

    def _requeue_messages(self, messages: list[IncomingMessage]) -> None:
        """Put an unannounced batch back at the front, in order (loop thread).

        Each requeued message carries one more failed attempt (barge-in
        included); past the cap it is dropped so a persistently failing
        session or a press-happy household cannot ding and retry forever.
        """
        requeued: list[IncomingMessage] = []
        for message in messages:
            attempts = self._announce_attempts.get(message.update_id, 0) + 1
            if attempts >= MAX_ANNOUNCE_ATTEMPTS:
                self._announce_attempts.pop(message.update_id, None)
                LOGGER.warning(
                    "Announcement dropped after repeated failures",
                    extra={
                        "event": "announcement_dropped",
                        "update_id": message.update_id,
                        "chat_id": message.chat_id,
                        "attempts": attempts,
                    },
                )
                continue
            self._announce_attempts[message.update_id] = attempts
            requeued.append(message)
        self._inbox[:0] = requeued

    def _announcement_sender_name(self, chat_id: str) -> str:
        """Household-facing name for the sender: the configured recipient whose
        chat id matches (e.g. ``dad`` → "Dad"). Unnamed chats stay generic."""
        for name, recipient_chat_id in self._config.messaging.recipients.items():
            if str(recipient_chat_id) == chat_id:
                return name[:1].upper() + name[1:]
        return "someone"

    def _spawn_active(
        self, coro: Coroutine[Any, Any, None], *, announcement: bool
    ) -> None:
        """Run one turn/announcement as the cancellable ``_active`` task.

        ``announcement`` tags what the task is so a message wake can tell a
        turn (interruptible to announce a new message when the toggle allows)
        from an announcement (never interruptible by one — it is itself the
        thing that will read that message).
        """
        task = asyncio.create_task(coro)
        task.add_done_callback(self._active_finished)
        self._active_is_announcement = announcement
        self._active = task

    def _active_finished(self, task: asyncio.Task[None]) -> None:
        """Done callback: nudge the loop so queued messages announce promptly.

        The loop is parked on the wake bus while ``_active`` runs and nothing
        else wakes it when that finishes, so a buffered message would sit
        until the next press otherwise. Retrieving the outcome here also keeps
        asyncio's "exception was never retrieved" warning quiet (the task's
        own error path already logged it).
        """
        if not task.cancelled() and task.exception() is not None:
            LOGGER.debug(
                "Active task ended in a logged error", exc_info=task.exception()
            )
        # Only nudge when something is actually pending: after a failed turn
        # with an empty inbox the LED keeps the ERROR pattern until the next
        # press instead of the loop spin clearing it right away.
        if self._inbox:
            self._wakes.emit_message()

    async def _run(self) -> None:
        # Declared up front: the two wake branches assign it from differently
        # narrowed expressions of the same attribute.
        active: asyncio.Task[None] | None
        self._loop = asyncio.get_running_loop()
        LOGGER.info("Assistant ready", extra={"event": "assistant_ready"})
        self._releases.start()
        self._wakes.attach(self._loop)
        self._press_pump.start()
        poller = self._poller
        if poller is not None:
            poller.start(self._loop)
        while True:
            self._led.set_state(LEDState.IDLE)
            if self._active is not None and self._active.done():
                self._active = None
            if self._active is None and self._inbox:
                # Idle with pending phone messages: announce them. Running the
                # announcement as `_active` is what keeps press-cancellation
                # (barge-in) working.
                request_id = uuid.uuid4().hex[:10]
                self._spawn_active(
                    self._run_announcement(request_id, self.drain_inbox()),
                    announcement=True,
                )
                continue
            wake = await self._wakes.wait()
            if not isinstance(wake, PressWake):
                # A message wake (or an active task that finished) re-spins the
                # loop; the top of the loop does the announcing. With
                # announce_during_conversation on, a wake carrying buffered
                # messages cancels the running turn/conversation through the
                # same path a press takes, so the messages announce at the
                # next loop-top instead of after the turn ends. Never cancel
                # on a bare nudge with an empty inbox, and never an
                # announcement — it took a fixed batch at spawn and is what
                # will read what just arrived (the loop-top drains again when
                # it finishes). The LED needs nothing here: this ends in the
                # same loop-top IDLE → announcement/turn sequence a press
                # takes.
                if (
                    self._config.messaging.announce_during_conversation
                    and self._inbox
                    and self._active is not None
                    and not self._active.done()
                    and not self._active_is_announcement
                ):
                    request_id = uuid.uuid4().hex[:10]
                    LOGGER.info(
                        "Message while active — interrupting the turn to announce",
                        extra={"event": "turn_interrupt", "request_id": request_id},
                    )
                    active, self._active = self._active, None
                    self._active_is_announcement = False
                    active.cancel()
                    await asyncio.gather(active, return_exceptions=True)
                continue
            request_id = uuid.uuid4().hex[:10]
            active, self._active = self._active, None
            if active is not None:
                if self._conversation:
                    # The toggle key: a press while a conversation is running
                    # just ends it (barge-in inside the conversation is done
                    # by voice — keep talking over the assistant).
                    LOGGER.info(
                        "Press while active — ending the conversation",
                        extra={"event": "turn_interrupt", "request_id": request_id},
                    )
                    active.cancel()
                    await asyncio.gather(active, return_exceptions=True)
                    continue
                # Push-to-talk: the press is barge-in — end the old turn and
                # start the new one from this same press.
                LOGGER.info(
                    "Press while active — barge-in",
                    extra={"event": "turn_interrupt", "request_id": request_id},
                )
                active.cancel()
                await asyncio.gather(active, return_exceptions=True)
            turn = (
                self._run_conversation(request_id)
                if self._conversation
                else self._run_turn(request_id)
            )
            self._spawn_active(turn, announcement=False)

    async def _run_turn(self, request_id: str) -> None:
        self._led.set_state(LEDState.LISTENING)
        LOGGER.info(
            "Turn started — listening",
            extra={"event": "turn_started", "request_id": request_id},
        )
        sonic = self._config.sonic
        attempts = [sonic.model_id]
        if sonic.fallback_model_id and sonic.fallback_model_id != sonic.model_id:
            attempts.append(sonic.fallback_model_id)
        for attempt_index, model_id in enumerate(attempts):
            try:
                await self._run_turn_once(request_id, model_id)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a turn error must not kill the service
                if attempt_index == len(attempts) - 1:
                    LOGGER.error(
                        "Turn failed",
                        extra={
                            "event": "turn_error",
                            "request_id": request_id,
                            "model": model_id,
                            "error": exc_message(exc),
                        },
                    )
                    self._led.set_state(LEDState.ERROR)
                else:
                    # 403 AccessDenied (model access not enabled) and similar
                    # service refusals surface with no model output at all;
                    # a whole fresh turn (session, prompt, mic) then runs on
                    # the fallback model.
                    LOGGER.warning(
                        "Model %s failed (%s); retrying turn with %s",
                        model_id,
                        exc_message(exc),
                        attempts[-1],
                        extra={
                            "event": "sonic_model_fallback",
                            "primary_model": model_id,
                            "fallback_model": attempts[-1],
                            "error": exc_message(exc),
                        },
                    )

    async def _open_with_deadline(
        self, model_id: str, prompt_name: str, content_name: str
    ) -> SonicTurnSession:
        """Open the session under the open timeout, abandoning a wedged open.

        ``asyncio.wait_for`` waits for the cancellation *it requests* to take
        effect; the SDK's duplex futures sometimes refuse it (an open stalled
        in the CRT transport can never be cancelled), which would wedge the
        whole turn. Racing the open against a plain deadline lets the turn
        fail on time, and ``_abandon_open`` closes the stream if it resolves
        later instead of leaking it toward Bedrock's session quota.
        """
        timeout = self._config.sonic.open_timeout_seconds
        open_task = asyncio.ensure_future(
            self._open_and_handshake(model_id, prompt_name, content_name)
        )
        done, _pending = await asyncio.wait({open_task}, timeout=timeout)
        if not done:
            open_task.cancel()  # best effort; may not take effect
            open_task.add_done_callback(self._abandon_open)
            raise TimeoutError(f"Nova Sonic open exceeded {timeout}s")
        return open_task.result()

    def _abandon_open(self, task: asyncio.Task[SonicTurnSession]) -> None:
        """Best-effort teardown of an open that resolved after its deadline."""
        try:
            if task.cancelled():
                return
            if task.exception() is not None:
                return
            session = task.result()
            asyncio.create_task(session.close())
        except Exception:
            LOGGER.debug("Error cleaning up abandoned Nova Sonic open", exc_info=True)

    async def _run_turn_once(self, request_id: str, model_id: str) -> None:
        """One full turn on one session; raise instead of swallowing errors."""
        config = self._config.sonic
        hardware = self._config.hardware
        mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        mic = MicStreamer(
            asyncio.get_running_loop(),
            mic_queue,
            config.input_sample_rate_hz,
            config.chunk_ms,
            device=hardware.input_device,
        )
        playback = PlaybackQueue(
            config.output_sample_rate_hz, config.prebuffer_ms, hardware.output_device
        )
        prompt_name = uuid.uuid4().hex
        content_name = uuid.uuid4().hex
        baseline = self._releases.count()
        started = time.monotonic()
        session: SonicTurnSession | None = None
        saw_output: list[bool] = []
        try:
            session = await self._open_with_deadline(
                model_id, prompt_name, content_name
            )
            LOGGER.info(
                "Nova Sonic session opened",
                extra={"event": "session_opened", "request_id": request_id, "model": model_id},
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
                    session, mic_queue, baseline, prompt_name, content_name, request_id, playback
                )
            )
            try:
                await asyncio.wait_for(
                    self._receive(
                        session, playback, request_id, prompt_name, saw_output
                    ),
                    timeout=config.turn_timeout_seconds,
                )
                await asyncio.to_thread(playback.wait_until_drained, 30.0)
            finally:
                await self._drain_sender(sender)
            LOGGER.info(
                "Turn completed",
                extra={
                    "event": "turn_completed",
                    "request_id": request_id,
                    "duration_seconds": round(time.monotonic() - started, 2),
                },
            )
        except asyncio.CancelledError:
            LOGGER.info(
                "Turn cancelled (press barge-in or message interrupt)",
                extra={
                    "event": "turn_cancelled",
                    "request_id": request_id,
                    "duration_seconds": round(time.monotonic() - started, 2),
                },
            )
            raise
        finally:
            # Dropping everything here is what makes barge-in instant: the mic
            # stops feeding and any audio still queued for playback discards.
            mic.stop()
            playback.stop()
            if session is not None:
                await self._close_session(session, prompt_name)

    async def _run_conversation(self, request_id: str) -> None:
        """One press opens a conversation; the next press ends it.

        The mic streams continuously into one session while the model's
        endpointing decides when each utterance ends, and it answers between
        user turns. Barge-in works the way the protocol intends: keep talking
        and the service emits INTERRUPTED, which clears playback here.
        """
        config = self._config.sonic
        self._led.set_state(LEDState.LISTENING)
        LOGGER.info(
            "Conversation started — speak when ready; pressing again ends it",
            extra={"event": "conversation_started", "request_id": request_id},
        )
        mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        mic = MicStreamer(
            asyncio.get_running_loop(),
            mic_queue,
            config.input_sample_rate_hz,
            config.chunk_ms,
            device=self._config.hardware.input_device,
        )
        playback = PlaybackQueue(
            config.output_sample_rate_hz, config.prebuffer_ms, self._config.hardware.output_device
        )
        prompt_name = uuid.uuid4().hex
        content_name = uuid.uuid4().hex
        started = time.monotonic()
        session: SonicTurnSession | None = None
        try:
            session = await self._open_with_deadline(
                config.model_id, prompt_name, content_name
            )
            LOGGER.info(
                "Nova Sonic session opened",
                extra={"event": "session_opened", "request_id": request_id, "model": config.model_id},
            )
            mic.start()
            playback.start()
            sender = asyncio.ensure_future(
                self._stream_mic_continuously(
                    session, mic_queue, prompt_name, content_name, playback
                )
            )
            try:
                # Bedrock caps the connection at 8 minutes; keep the
                # conversation under that with a graceful close.
                await asyncio.wait_for(
                    self._receive(
                        session, playback, request_id, prompt_name, conversation=True
                    ),
                    timeout=config.turn_timeout_seconds,
                )
            except TimeoutError:
                LOGGER.info(
                    "Conversation reached its maximum duration; closing",
                    extra={
                        "event": "conversation_limit",
                        "request_id": request_id,
                        "duration_seconds": round(time.monotonic() - started, 2),
                    },
                )
            finally:
                await self._drain_sender(sender)
            LOGGER.info(
                "Conversation ended",
                extra={
                    "event": "conversation_ended",
                    "request_id": request_id,
                    "duration_seconds": round(time.monotonic() - started, 2),
                },
            )
        except asyncio.CancelledError:
            LOGGER.info(
                "Conversation cancelled (press or message interrupt)",
                extra={
                    "event": "conversation_ended",
                    "request_id": request_id,
                    "duration_seconds": round(time.monotonic() - started, 2),
                },
            )
            raise
        finally:
            mic.stop()
            playback.stop()
            if session is not None:
                await self._close_streaming_session(session, prompt_name, content_name)

    async def _run_announcement(
        self, request_id: str, messages: list[IncomingMessage]
    ) -> None:
        """Read pending phone messages aloud over one mic-less Nova session.

        Modeled on ``_run_conversation`` minus the mic: the sender streams
        encoded silence into an open USER AUDIO content (the docs require an
        actively streaming session for cross-modal text, and this can't trip
        voice activity detection), the scripted prompt travels as USER text,
        and ``_receive`` streams the model's spoken reply out through the
        normal playback path. Messages only leave the inbox on success — an
        error or a press barge-in requeues them at the front.
        """
        started = time.monotonic()
        self._led.set_state(LEDState.LISTENING)
        LOGGER.info(
            "Announcement started",
            extra={
                "event": "announcement_started",
                "request_id": request_id,
                "messages": len(messages),
            },
        )
        sender_name = self._announcement_sender_name(messages[0].chat_id)
        script = build_announcement_script(sender_name, [m.text for m in messages])
        try:
            if self._config.messaging.announce_chime:
                # The ding comes first and does not wait on Bedrock: opening
                # the session takes 1-3 s and the cue must land immediately.
                await self._play_chime()
            await self._announce_over_sonic(request_id, script)
        except asyncio.CancelledError:
            LOGGER.info(
                "Announcement cancelled by press",
                extra={
                    "event": "announcement_cancelled",
                    "request_id": request_id,
                    "duration_seconds": round(time.monotonic() - started, 2),
                },
            )
            self._requeue_messages(messages)
            raise
        except Exception as exc:  # noqa: BLE001 - an announcement must not kill the service
            LOGGER.error(
                "Announcement failed",
                extra={
                    "event": "announcement_error",
                    "request_id": request_id,
                    "error": exc_message(exc),
                },
            )
            self._requeue_messages(messages)
            self._led.set_state(LEDState.ERROR)
        else:
            for message in messages:
                self._announce_attempts.pop(message.update_id, None)
            LOGGER.info(
                "Announcement completed",
                extra={
                    "event": "announcement_completed",
                    "request_id": request_id,
                    "messages": len(messages),
                    "duration_seconds": round(time.monotonic() - started, 2),
                },
            )

    async def _play_chime(self) -> None:
        """Sound the two-tone ding through its own one-shot playback stream.

        No PlaybackQueue exists between turns, so the chime opens one just for
        itself (same pattern as the timer alarm). Polling the drain instead of
        blocking keeps shutdown's cancellation instant.
        """
        config = self._config.sonic
        pcm = build_message_chime(config.output_sample_rate_hz)
        playback = PlaybackQueue(
            config.output_sample_rate_hz,
            prebuffer_ms=50,  # the chime is its own startup transient
            device=self._config.hardware.output_device,
        )
        try:
            playback.start()
            playback.put_pcm(pcm)
            loop = asyncio.get_running_loop()
            duration = len(pcm) / (2.0 * config.output_sample_rate_hz)
            deadline = loop.time() + duration + 1.0
            while not playback.is_drained() and loop.time() < deadline:
                await asyncio.sleep(0.05)
        finally:
            playback.stop()

    async def _announce_over_sonic(self, request_id: str, script: str) -> None:
        """One announcement session: silence in, cross-modal text, speech out."""
        config = self._config.sonic
        playback = PlaybackQueue(
            config.output_sample_rate_hz, config.prebuffer_ms, self._config.hardware.output_device
        )
        prompt_name = uuid.uuid4().hex
        audio_content_name = uuid.uuid4().hex
        # Cross-modal text wants a fresh content name, separate from the open
        # audio content the silence is streaming into.
        text_content_name = uuid.uuid4().hex
        session: SonicTurnSession | None = None
        try:
            session = await self._open_with_deadline(
                config.model_id, prompt_name, audio_content_name
            )
            LOGGER.info(
                "Nova Sonic session opened",
                extra={"event": "session_opened", "request_id": request_id, "model": config.model_id},
            )
            playback.start()
            sender = asyncio.ensure_future(
                self._stream_silence(session, prompt_name, audio_content_name)
            )
            try:
                # Let the silence stream briefly first so the session is live
                # when the text triplet arrives (the docs' active-session
                # requirement; verified working at half a second).
                await asyncio.sleep(ANNOUNCE_TEXT_DELAY_SECONDS)
                await session.send_events(
                    build_user_text_events(prompt_name, text_content_name, script)
                )
                await asyncio.wait_for(
                    self._receive(session, playback, request_id, prompt_name),
                    timeout=config.turn_timeout_seconds,
                )
                await asyncio.to_thread(playback.wait_until_drained, 30.0)
            finally:
                await self._drain_sender(sender)
        finally:
            playback.stop()
            if session is not None:
                await self._close_streaming_session(session, prompt_name, audio_content_name)

    async def _stream_silence(
        self,
        session: SonicTurnSession,
        prompt_name: str,
        content_name: str,
    ) -> None:
        """Stream encoded silence into the open USER AUDIO content until cancelled.

        Keeps the announcement session streaming without opening the mic
        (which would hear the chime and the model's own voice) and without
        a contentEnd that could hand the turn to the model early.
        """
        chunk_ms = self._config.sonic.chunk_ms
        samples_per_chunk = int(
            self._config.sonic.input_sample_rate_hz * chunk_ms / 1000
        )
        silence = encode_pcm_chunk(b"\x00\x00" * samples_per_chunk)
        while True:
            await asyncio.sleep(chunk_ms / 1000)
            await session.send_event(
                build_audio_input(prompt_name, content_name, silence)
            )

    async def _stream_mic_continuously(
        self,
        session: SonicTurnSession,
        mic_queue: asyncio.Queue[bytes],
        prompt_name: str,
        content_name: str,
        playback: PlaybackQueue,
    ) -> None:
        """Stream mic audio for the whole conversation; no contentEnd until close.

        While the assistant is speaking, mic audio is dropped (half-duplex):
        sitting near the speakers, the microphone hears the model's own voice
        and the model barge-ins on itself otherwise — the "Hello Mort" echo
        loop. Capture resumes ``duck_resume_ms`` after playback goes quiet.
        """
        resume_at = 0.0
        while True:
            try:
                chunk = await asyncio.wait_for(mic_queue.get(), timeout=0.05)
            except TimeoutError:
                continue
            now = time.monotonic()
            if self._config.sonic.duck_mic_while_speaking and playback.is_playing():
                resume_at = now + self._config.sonic.duck_resume_ms / 1000
                continue
            if now < resume_at:
                continue
            await session.send_event(
                build_audio_input(prompt_name, content_name, encode_pcm_chunk(chunk))
            )

    async def _drain_sender(self, sender: asyncio.Future[None]) -> None:
        """Cancel the mic sender and let its in-flight send settle."""
        sender.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(sender, return_exceptions=True), timeout=5.0
            )
        except Exception:
            LOGGER.debug("Mic sender did not settle", exc_info=True)

    async def _close_streaming_session(
        self, session: SonicTurnSession, prompt_name: str, content_name: str
    ) -> None:
        """End the open user audio content, then run the normal close sequence.

        Used by conversations and announcements alike — both keep one USER
        AUDIO content open for their whole session, unlike the push-to-talk
        sender which ends it on button release.
        """
        try:
            await asyncio.wait_for(
                session.send_event(build_audio_content_end(prompt_name, content_name)),
                timeout=5.0,
            )
        except Exception:
            LOGGER.debug("Error ending audio content on close", exc_info=True)
        await self._close_session(session, prompt_name)

    async def _open_and_handshake(
        self, model_id: str, prompt_name: str, content_name: str
    ) -> SonicTurnSession:
        """Open the stream and send the turn handshake as one unit under one timeout.

        Bedrock resolves model access asynchronously: a 403 refusal can arrive
        after the duplex is handed back and then hangs any further input send.
        Keeping the handshake inside the open timeout is what lets the
        fallback-model retry happen on a dead session.
        """
        duplex: Any = None
        session: SonicTurnSession | None = None
        try:
            duplex, _ = await self._client.open_stream(model_id)
            session = SonicTurnSession(duplex)
            await session.send_events(
                [
                    build_prompt_start(
                        prompt_name,
                        self._config.sonic.voice_id,
                        self._config.sonic.output_sample_rate_hz,
                        self._tools.get_nova_tool_definitions(),
                    ),
                    *build_system_prompt_events(
                        prompt_name, uuid.uuid4().hex, self._system_prompt()
                    ),
                    build_audio_content_start(
                        prompt_name,
                        content_name,
                        self._config.sonic.input_sample_rate_hz,
                    ),
                ]
            )
        except asyncio.CancelledError:
            # The open timeout fired: the half-open stream must not leak
            # (Bedrock allows 20 concurrent sessions per region).
            if session is not None:
                await session.close()
            elif duplex is not None:
                await SonicTurnSession(duplex).close()
            raise
        except Exception:
            if session is not None:
                await self._close_session(session, prompt_name)
            raise
        return session

    async def _close_session(self, session: SonicTurnSession, prompt_name: str) -> None:
        """Send the documented end-of-session sequence, then close the stream.

        Skipping promptEnd/sessionEnd can orphan server-side resources, so do
        both even when the turn failed. Every step here is best-effort and
        timeout-guarded: sends into a dying duplex can block indefinitely (the
        service stops consuming events once it refuses or finishes the turn),
        which would hang the finally block forever without the guard.
        """
        for event in (build_prompt_end(prompt_name), build_session_end()):
            try:
                await asyncio.wait_for(session.send_event(event), timeout=5.0)
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
        playback: PlaybackQueue,
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
            if self._config.sonic.duck_mic_while_speaking and playback.is_playing():
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
        saw_output: list[bool] | None = None,
        conversation: bool = False,
    ) -> None:
        """Drive the loop until one model completion finishes.

        In conversation mode the stream spans many responses: END_TURN closes
        the model's current response (LED back to listening) and the loop keeps
        reading for the next user utterance.
        """
        async for event in session.output_events():
            if saw_output is not None and not saw_output:
                saw_output.append(True)
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
                    if conversation:
                        # The service's completion ends with the AUDIO
                        # contentEnd (stopReason END_TURN); completionEnd is
                        # not reliably sent. In conversation mode the same
                        # stream continues with the next user utterance.
                        self._led.set_state(LEDState.LISTENING)
                        continue
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

    def _on_timer_elapsed(self, timer: ActiveTimer) -> None:
        """Timer thread → loop bridge for a fired timer.

        threading.Timer fires on its own thread; like the mic counter it may
        only touch pure state, and reaches the loop through
        ``call_soon_threadsafe``.
        """
        label = f" ({timer.label})" if timer.label else ""
        LOGGER.info(
            f"Timer elapsed — sounding the alarm{label}",
            extra={
                "event": "timer_elapsed",
                "timer_id": timer.timer_id,
                "label": timer.label,
                "duration_seconds": timer.duration_seconds,
            },
        )
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._start_alarm_task)
        except RuntimeError:
            # The loop already closed (shutdown raced a firing timer).
            pass

    def _start_alarm_task(self) -> None:
        if self._alarm_task is not None and not self._alarm_task.done():
            # An alarm is already sounding; a second timer firing into it just
            # extends nothing — keep it simple and let the running alarm stand.
            return
        self._alarm_task = asyncio.create_task(self._play_timer_alarm())

    async def _play_timer_alarm(self) -> None:
        """Sound one alarm through a one-shot playback stream.

        No PlaybackQueue exists between turns, so the alarm opens its own just
        for this. A live conversational stream keeps its own queue — both feed
        the same output device, so the alarm sounds over the assistant's voice,
        and each side's queue (barge-in clear, mic-duck check) is untouched.
        """
        config = self._config.sonic
        try:
            pcm = build_timer_alarm_pcm(
                config.output_sample_rate_hz, self._config.timer_alarm_seconds
            )
            playback = PlaybackQueue(
                config.output_sample_rate_hz,
                prebuffer_ms=50,  # the alarm is its own startup transient; don't sit behind a prebuffer
                device=self._config.hardware.output_device,
            )
            try:
                playback.start()
                playback.put_pcm(pcm)
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self._config.timer_alarm_seconds + 2.0
                # Poll instead of a blocking wait: shutdown must be able to
                # cancel this task instantly rather than strand a thread.
                while not playback.is_drained() and loop.time() < deadline:
                    await asyncio.sleep(0.05)
            finally:
                playback.stop()
            # Shutdown's task cancellation re-raises before this except.
        except Exception:
            LOGGER.exception(
                "Timer alarm playback failed",
                extra={"event": "timer_alarm_error"},
            )

    def _system_prompt(self) -> str:
        """Config prompt plus current local time, prepended to all session input."""
        now = datetime.now().astimezone()
        timestamp = now.strftime("Current local date and time: %A, %B %d, %Y %I:%M %p %Z")
        return f"{self._config.system_prompt.strip()}\n{timestamp}"
