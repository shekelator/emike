"""Telegram transport and poller for the messaging feature (phase 1).

Phase 1 is scaffolding: the poller runs when ``messaging.enabled`` and feeds a
small in-process inbox that a later phase (wake-and-announce) will consume.
No announcement or chime behavior lives here.

Shaped like the existing thread bridges (``ReleaseCounter`` in
``emike.assistant``): one daemon thread owns the network; the event loop only
receives messages through ``call_soon_threadsafe``. The poller never crashes
on transport errors — it backs off exponentially and retries, because WiFi
blips must self-heal.

Privacy rules baked in:
- the bot token never reaches a log line or exception message;
- message text never reaches a log line (journalctl is shared; messages are
  read aloud later anyway) — logs carry chat id, text length, and update id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests

LOGGER = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
# The Bot API hard cap is 4096; leave headroom.
MAX_MESSAGE_CHARS = 4000
DEFAULT_OFFSET_PATH = Path.home() / ".cache" / "emike" / "telegram-offset"

# Bot tokens look like "123456:AA..."; scrub anywhere they could reach a log.
_BOT_TOKEN_PATTERN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{10,}")


def redact_tokens(text: str, token: str) -> str:
    """Replace bot-token material with a marker so it never reaches a log."""
    scrubbed = _BOT_TOKEN_PATTERN.sub("[REDACTED]", text)
    if token:
        scrubbed = scrubbed.replace(token, "[REDACTED]")
    return scrubbed


class TransportError(RuntimeError):
    """Any Telegram transport failure; the message is always token-redacted."""


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    chat_id: str
    text: str
    update_id: int
    received_at: float


class MessageTransport(Protocol):
    def send(self, chat_id: str, text: str) -> None:
        """Send ``text`` to a Telegram chat; raises TransportError."""
        ...

    def poll(self, offset: int) -> list[IncomingMessage]:
        """Long-poll for updates newer than ``offset``; blocks ~25 s."""
        ...


def load_offset(path: Path) -> int | None:
    """Read the last confirmed Telegram update id, or None if unavailable."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        LOGGER.debug("Could not read Telegram offset file", exc_info=True)
        return None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        LOGGER.debug("Telegram offset file holds garbage; starting fresh")
        return None


def save_offset(path: Path, update_id: int) -> None:
    """Persist the last confirmed update id (at-most-once, atomic rename)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(f"{update_id}\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        LOGGER.warning(
            "Could not persist Telegram offset",
            extra={"event": "offset_persist_error", "path": str(path)},
        )


class TelegramTransport:
    """Telegram Bot API over plain HTTPS long-polling.

    The token comes from the caller (resolved from the environment at wiring
    time, not inside this class). Every failure is raised as TransportError
    with the token scrubbed — requests exception text embeds the request path,
    which contains the token, so nothing is surfaced unredacted.
    """

    def __init__(
        self,
        token: str,
        poll_timeout_seconds: int = 25,
        http_timeout_seconds: float = 35.0,
    ) -> None:
        token = token.strip()
        if not token:
            raise TransportError("Telegram bot token is required")
        self._token = token
        self._poll_timeout_seconds = poll_timeout_seconds
        self._http_timeout_seconds = http_timeout_seconds

    def send(self, chat_id: str, text: str) -> None:
        url = f"{TELEGRAM_API_BASE}/bot{self._token}/sendMessage"
        try:
            response = requests.post(
                url,
                json={"chat_id": chat_id, "text": text[:MAX_MESSAGE_CHARS]},
                timeout=self._http_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise TransportError(redact_tokens(str(exc), self._token)) from None
        payload = _response_payload(response, "sendMessage", self._token)
        if response.status_code != 200 or not payload.get("ok"):
            description = str(payload.get("description", f"HTTP {response.status_code}"))
            raise TransportError(
                redact_tokens(f"sendMessage failed: {description}", self._token)
            )

    def poll(self, offset: int) -> list[IncomingMessage]:
        """One long-poll ``getUpdates``; returns text messages of any chat.

        The caller owns the offset: it is sent on every request, so calling
        this with a higher offset confirms every earlier update at Telegram's
        side (no redelivery) even for updates this call drops.
        """
        url = f"{TELEGRAM_API_BASE}/bot{self._token}/getUpdates"
        params: dict[str, str | int] = {
            "offset": offset,
            "timeout": self._poll_timeout_seconds,
            "allowed_updates": json.dumps(["message"]),
        }
        try:
            response = requests.get(url, params=params, timeout=self._http_timeout_seconds)
        except requests.RequestException as exc:
            raise TransportError(redact_tokens(str(exc), self._token)) from None
        payload = _response_payload(response, "getUpdates", self._token)
        if response.status_code != 200 or not payload.get("ok"):
            description = str(payload.get("description", f"HTTP {response.status_code}"))
            raise TransportError(
                redact_tokens(f"getUpdates failed: {description}", self._token)
            )
        updates = payload.get("result", [])
        if not isinstance(updates, list):
            raise TransportError("getUpdates returned an unexpected payload shape")
        messages: list[IncomingMessage] = []
        for update in updates:
            message = _parse_update(update)
            if message is not None:
                messages.append(message)
        return messages


def _response_payload(
    response: requests.Response, what: str, token: str
) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError:
        raise TransportError(
            redact_tokens(f"{what} returned non-JSON (HTTP {response.status_code})", token)
        ) from None
    if not isinstance(payload, dict):
        raise TransportError(
            redact_tokens(f"{what} returned an unexpected payload (HTTP {response.status_code})", token)
        )
    return payload


def _parse_update(update: object) -> IncomingMessage | None:
    """One Telegram update to IncomingMessage; None for anything not a chat message."""
    if not isinstance(update, dict):
        return None
    update_id_raw = update.get("update_id")
    if not isinstance(update_id_raw, int):
        return None
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    chat_id_raw = chat.get("id")
    if chat_id_raw is None:
        return None
    text = str(message.get("text", ""))
    if not text.strip():
        return None
    return IncomingMessage(
        chat_id=str(chat_id_raw),
        text=text,
        update_id=update_id_raw,
        received_at=time.time(),
    )


class TokenBucket:
    """Per-process rolling-window send limiter (thread-safe).

    At most ``rate_per_minute`` sends inside a rolling ``window_seconds``.
    Returns False instead of raising so a tool call can reply conversationally.
    """

    def __init__(
        self,
        rate_per_minute: int,
        window_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate_per_minute < 1:
            raise ValueError("rate_per_minute must be at least 1")
        if window_seconds < 1:
            raise ValueError("window_seconds must be at least 1")
        self._rate = rate_per_minute
        self._window = float(window_seconds)
        self._clock = clock
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        now = self._clock()
        with self._lock:
            cutoff = now - self._window
            while self._events and self._events[0] <= cutoff:
                self._events.popleft()
            if len(self._events) >= self._rate:
                return False
            self._events.append(now)
            return True


class TelegramPoller:
    """Long-poll Telegram on one daemon thread; never crashes, backs off.

    Modeled on the ``ReleaseCounter`` bridge: the thread only touches its own
    state plus the transport, and hands each allowlisted message to the event
    loop with ``call_soon_threadsafe``. It owns the offset: it is passed to
    every ``poll()`` and the last confirmed update id is persisted before any
    message can be surfaced (at-most-once; rejected chats confirm too, or
    Telegram redelivers forever).
    """

    def __init__(
        self,
        transport: MessageTransport,
        on_message: Callable[[IncomingMessage], None],
        allowlist: Iterable[str],
        offset_path: Path,
        stop_event: threading.Event | None = None,
        backoff_start_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
    ) -> None:
        self._transport = transport
        self._on_message = on_message
        self._allowlist = frozenset(str(chat_id) for chat_id in allowlist)
        self._offset_path = offset_path
        # The file holds the last confirmed update id; getUpdates takes
        # confirmed + 1.
        stored = load_offset(offset_path)
        self._offset = stored + 1 if stored is not None else 0
        self._stop = stop_event if stop_event is not None else threading.Event()
        self._backoff_start = float(backoff_start_seconds)
        self._backoff_max = float(backoff_max_seconds)
        self._backoff = self._backoff_start
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the loop messages are dispatched to (start() does this too)."""
        self._loop = loop

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._thread is not None:
            return
        self._loop = loop
        self._thread = threading.Thread(target=self._run, name="telegram-poller", daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        """Ask the thread to finish, then join it (best effort, bounded)."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(join_timeout)

    @property
    def offset(self) -> int:
        return self._offset

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()

    def poll_once(self) -> None:
        """One long-poll cycle; public so tests can drive it without threads."""
        try:
            batch = self._transport.poll(self._offset)
        except TransportError as exc:
            self._log_and_backoff(logging.WARNING, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - the poller must never die
            # Log the type only: unknown exception text could hold anything.
            self._log_and_backoff(logging.ERROR, type(exc).__name__)
            return
        self._backoff = self._backoff_start
        self._process(batch if batch else [])

    def _process(self, batch: list[IncomingMessage]) -> None:
        if not batch or self._stop.is_set():
            return
        last_id = max(message.update_id for message in batch)
        # At-most-once: persist the offset — covering rejected updates too —
        # before anything can be surfaced.
        self._offset = last_id + 1
        save_offset(self._offset_path, last_id)
        loop = self._loop
        for message in batch:
            if message.chat_id not in self._allowlist:
                LOGGER.info(
                    "Message from a non-allowlisted chat ignored",
                    extra={
                        "event": "message_rejected",
                        "chat_id": message.chat_id,
                        "chars": len(message.text),
                    },
                )
                continue
            if loop is None:
                continue
            try:
                loop.call_soon_threadsafe(self._on_message, message)
            except RuntimeError:
                return  # loop closed during shutdown

    def _log_and_backoff(self, level: int, error: str) -> None:
        wait = self._backoff
        self._backoff = min(self._backoff * 2, self._backoff_max)
        LOGGER.log(
            level,
            "Telegram poller error — backing off",
            extra={"event": "transport_error", "error": error, "backoff_seconds": wait},
        )
        # Event.wait doubles as an interruptible sleep: stop cuts it short.
        self._stop.wait(wait)
