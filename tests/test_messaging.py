"""Tests for the Telegram messaging plumbing (no network, no threads)."""

import json
import threading
from pathlib import Path
from typing import Any

import pytest
import requests

from emike.messaging import (
    MAX_MESSAGE_CHARS,
    IncomingMessage,
    TelegramPoller,
    TelegramTransport,
    TokenBucket,
    TransportError,
    load_offset,
    redact_tokens,
    save_offset,
)


def _message(update_id: int, chat_id: str = "111", text: str = "hello") -> IncomingMessage:
    return IncomingMessage(chat_id=chat_id, text=text, update_id=update_id, received_at=0.0)


class FakeTransport:
    """Scripted batches; records send calls and the offsets it was polled with."""

    def __init__(self, batches: list[list[IncomingMessage]]) -> None:
        self._batches = list(batches)
        self.offsets: list[int] = []
        self.sent: list[tuple[str, str]] = []

    def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    def poll(self, offset: int) -> list[IncomingMessage]:
        self.offsets.append(offset)
        if self._batches:
            return self._batches.pop(0)
        return []


class RecordingLoop:
    """Loop stand-in: applies call_soon_threadsafe immediately, records calls."""

    def __init__(self) -> None:
        self.calls: list[IncomingMessage] = []

    def call_soon_threadsafe(self, callback: Any, /, *args: Any) -> None:
        self.calls.append(args[0])


# --- redaction ---------------------------------------------------------------


def test_redact_tokens_scrubs_pattern_and_exact_token() -> None:
    text = "url /bot999123456:secret-token-abcdefg/sendMessage and 123456:secret-token"
    scrubbed = redact_tokens(text, "123456:secret-token")
    assert "secret-token" not in scrubbed and "999123456" not in scrubbed
    assert scrubbed.count("[REDACTED]") == 2


def test_send_failure_message_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, **kwargs: Any) -> None:
        raise requests.ConnectionError(f"ConnectionError(url={url})")

    monkeypatch.setattr(requests, "post", boom)
    transport = TelegramTransport("123456:secret-token")
    with pytest.raises(TransportError) as excinfo:
        transport.send("111", "hi")
    assert "secret-token" not in str(excinfo.value)
    assert "[REDACTED]" in str(excinfo.value)


# --- send (truncation, request shape) ----------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def test_send_truncates_at_4000_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        seen["url"] = url
        seen["json"] = kwargs["json"]
        return FakeResponse(200, {"ok": True})

    monkeypatch.setattr(requests, "post", fake_post)
    transport = TelegramTransport("999123456:secret-token-abcdefg")
    transport.send("111", "x" * (MAX_MESSAGE_CHARS + 500))
    assert len(seen["json"]["text"]) == MAX_MESSAGE_CHARS
    assert seen["json"]["chat_id"] == "111"
    assert "sendMessage" in seen["url"]


def test_send_api_error_raises_redacted_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        return FakeResponse(400, {"ok": False, "description": f"bad request {url}"})

    monkeypatch.setattr(requests, "post", fake_post)
    transport = TelegramTransport("999123456:secret-token-abcdefg")
    with pytest.raises(TransportError) as excinfo:
        transport.send("111", "hi")
    assert "secret-token" not in str(excinfo.value)


# --- poll (parse + parameter shape) ------------------------------------------


def test_poll_sends_offset_and_parses_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        seen["url"] = url
        seen["params"] = kwargs["params"]
        return FakeResponse(
            200,
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 10,
                        "message": {
                            "text": "from dave",
                            "chat": {"id": 123456789, "first_name": "Dave"},
                        },
                    },
                    {
                        "update_id": 11,
                        "message": {"text": "  ", "chat": {"id": 2}},  # no text → skipped
                    },
                    {"update_id": 12, "no_message_here": True},
                ],
            },
        )

    monkeypatch.setattr(requests, "get", fake_get)
    transport = TelegramTransport("999123456:secret-token-abcdefg", poll_timeout_seconds=17)
    messages = transport.poll(7)

    assert seen["params"]["offset"] == 7
    assert seen["params"]["timeout"] == 17
    assert json.loads(seen["params"]["allowed_updates"]) == ["message"]
    assert len(messages) == 1
    assert messages[0].chat_id == "123456789"
    assert messages[0].text == "from dave"
    assert messages[0].update_id == 10


def test_empty_token_is_rejected() -> None:
    with pytest.raises(TransportError):
        TelegramTransport("   ")


# --- offset persistence ------------------------------------------------------


def test_offset_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "telegram-offset"
    assert load_offset(path) is None
    save_offset(path, 42)
    assert load_offset(path) == 42
    save_offset(path, 100)
    assert load_offset(path) == 100
    assert not list(tmp_path.glob("*.tmp"))


def test_load_offset_garbage_is_none(tmp_path: Path) -> None:
    path = tmp_path / "telegram-offset"
    path.write_text("not a number", encoding="utf-8")
    assert load_offset(path) is None


# --- poller: allowlist, offset advancing, dispatch ---------------------------


def test_poller_filters_allowlist_and_advances_offset_for_rejected(
    tmp_path: Path,
) -> None:
    offset_path = tmp_path / "telegram-offset"
    transport = FakeTransport(
        [
            [
                _message(10, chat_id="111"),
                _message(11, chat_id="999"),  # rejected chat
                _message(12, chat_id="111"),
            ]
        ]
    )
    loop = RecordingLoop()
    poller = TelegramPoller(
        transport,
        on_message=loop.calls.append,
        allowlist=["111"],
        offset_path=offset_path,
    )
    poller.attach_loop(loop)  # tests drive without a thread

    poller.poll_once()

    assert [message.update_id for message in loop.calls] == [10, 12]
    # The rejected update was confirmed too, and the offset persisted before
    # dispatch.
    assert transport.offsets == [0]
    assert poller.offset == 13
    assert load_offset(offset_path) == 12


def test_poller_persists_offset_across_instances(tmp_path: Path) -> None:
    offset_path = tmp_path / "telegram-offset"
    transport = FakeTransport([[_message(10), _message(11, chat_id="999")]])
    poller = TelegramPoller(
        transport,
        on_message=lambda _message: None,
        allowlist=["111"],
        offset_path=offset_path,
    )
    poller.poll_once()
    second = TelegramPoller(
        transport,
        on_message=lambda _message: None,
        allowlist=["111"],
        offset_path=offset_path,
    )
    assert second.offset == 12  # file holds last confirmed (11); offset = 11 + 1
    second.poll_once()
    assert transport.offsets == [0, 12]


def test_poller_backs_off_on_transport_error(tmp_path: Path) -> None:
    class FailingTransport:
        def send(self, chat_id: str, text: str) -> None:
            raise TransportError("unreachable")

        def poll(self, offset: int) -> list[IncomingMessage]:
            raise TransportError("boom")

    stop = threading.Event()
    poller = TelegramPoller(
        FailingTransport(),
        on_message=lambda _message: None,
        allowlist=["111"],
        offset_path=tmp_path / "telegram-offset",
        stop_event=stop,
        backoff_start_seconds=0.1,
    )
    poller.poll_once()  # must not raise; backs off once (0.1 s) and returns
    assert poller.offset == 0


# --- token bucket ------------------------------------------------------------


def test_token_bucket_exhausts_and_refills() -> None:
    now = [1000.0]

    def clock() -> float:
        return now[0]

    bucket = TokenBucket(2, 600, clock=clock)
    assert bucket.try_acquire()
    assert bucket.try_acquire()
    assert not bucket.try_acquire()
    now[0] += 60.0  # still inside the rolling window
    assert not bucket.try_acquire()
    now[0] += 601.0  # both events aged out
    assert bucket.try_acquire()
    assert bucket.try_acquire()
    assert not bucket.try_acquire()


def test_token_bucket_refills_one_at_a_time() -> None:
    now = [0.0]

    def clock() -> float:
        return now[0]

    bucket = TokenBucket(2, 100, clock=clock)
    assert bucket.try_acquire()
    now[0] += 50.0
    assert bucket.try_acquire()
    assert not bucket.try_acquire()
    now[0] += 51.0  # first event (t=0) has aged out; the second (t=50) has not
    assert bucket.try_acquire()
    assert not bucket.try_acquire()
