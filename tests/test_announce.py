"""Phase-2 announcement tests: wake bus, script builder, announcement runtime.

Pure — no network, no audio devices, no AWS: the Nova Sonic session sits
behind fakes and the script builder is tested as a plain function.
"""

import asyncio
import base64
import json
import logging
import threading
import time
from typing import Any, ClassVar

import pytest
from aws_sdk_bedrock_runtime.models import (
    BidirectionalOutputPayloadPart,
    InvokeModelWithBidirectionalStreamOutputChunk,
)

from emike.assistant import (
    MAX_ANNOUNCEMENT_CHARS,
    MESSAGE_WAKE,
    PRESS_WAKE,
    AssistantRuntime,
    ButtonWakePump,
    MessageWake,
    PressWake,
    WakeBus,
    build_announcement_script,
)
from emike.button import ButtonInterface
from emike.chime import build_message_chime
from emike.config import AppConfig, HardwareConfig, MessagingConfig, SonicConfig
from emike.messaging import IncomingMessage


def _message(update_id: int, chat_id: str = "111", text: str = "pizza at six") -> IncomingMessage:
    return IncomingMessage(chat_id=chat_id, text=text, update_id=update_id, received_at=0.0)


class StubButton(ButtonInterface):
    """Presses via a gate; waiting threads park (never spin)."""

    def __init__(self) -> None:
        self._press = threading.Event()
        self._forever = threading.Event()

    def press(self) -> None:
        self._press.set()

    def wait_for_press(self) -> None:
        # Consume the gate: a one-shot press parks the pump again, exactly
        # like a real (debounced) button; a left-set gate would hot-spin it.
        if self._press.wait():
            self._press.clear()

    def wait_for_release(self) -> None:
        self._forever.wait()

    def close(self) -> None:
        return None


async def _until(predicate: Any, timeout_s: float = 0.5) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def _events(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [str(r.event) for r in caplog.records if hasattr(r, "event")]


# --- wake bus ------------------------------------------------------------------


def test_wake_bus_returns_wakes_in_emission_order() -> None:
    async def scenario() -> None:
        bus = WakeBus()
        bus.attach(asyncio.get_running_loop())
        bus.emit_press()
        bus.emit_message()
        bus.emit_press()
        assert await bus.wait() is PRESS_WAKE
        assert await bus.wait() is MESSAGE_WAKE
        assert await bus.wait() is PRESS_WAKE

    asyncio.run(scenario())


def test_wake_bus_cross_thread_emission_reaches_the_loop() -> None:
    async def scenario() -> None:
        bus = WakeBus()
        bus.attach(asyncio.get_running_loop())
        thread = threading.Thread(target=bus.emit_message)
        thread.start()
        thread.join()
        wake = await asyncio.wait_for(bus.wait(), 1.0)
        assert isinstance(wake, MessageWake)

    asyncio.run(scenario())


def test_wake_bus_emits_are_safe_unattached_and_after_loop_close() -> None:
    WakeBus().emit_press()  # no loop attached yet: must be a no-op

    async def scenario() -> WakeBus:
        bus = WakeBus()
        bus.attach(asyncio.get_running_loop())
        return bus

    bus = asyncio.run(scenario())  # the loop is closed once asyncio.run returns
    bus.emit_message()  # must not raise (the closing loop during shutdown)


# --- button wake pump ------------------------------------------------------------


def test_button_wake_pump_forwards_presses_to_the_bus() -> None:
    async def scenario() -> None:
        button = StubButton()
        bus = WakeBus()
        bus.attach(asyncio.get_running_loop())
        ButtonWakePump(button, bus).start()  # daemon thread; parks until pressed
        # No press yet: the pump must stay parked, not spin out phantom wakes.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.wait(), 0.1)
        button.press()
        wake = await asyncio.wait_for(bus.wait(), 1.0)
        assert isinstance(wake, PressWake)

    asyncio.run(scenario())


# --- announcement script ---------------------------------------------------------


def test_script_frames_messages_verbatim_with_sender_name() -> None:
    script = build_announcement_script("Dad", ["pizza at six", "call the plumber"])

    assert script.startswith("Announce to the household that you have 2 new messages from Dad")
    assert "read each one aloud" in script.lower() and "before anything else" in script.lower()
    assert 'First message: "pizza at six"' in script
    assert 'Second message: "call the plumber"' in script


def test_script_singular_wording_for_one_message() -> None:
    script = build_announcement_script("Dad", ["hi"])
    assert script.startswith("Announce to the household that you have 1 new message from Dad")
    assert 'First message: "hi"' in script


def test_script_keeps_the_quote_frame_safe() -> None:
    text = '"pizza"\nnight  at   six?'
    script = build_announcement_script("Dad", [text])
    assert "'pizza' night at six?" in script


def test_script_caps_total_length_but_announces_every_message() -> None:
    texts = [" ".join([f"note{i}", "x" * 3000]) for i in range(11)]
    script = build_announcement_script("Dad", texts)

    assert len(script) <= MAX_ANNOUNCEMENT_CHARS + 60
    assert "Message 11:" in script  # past the ordinals, the label falls back
    assert all(f"note{i}" in script for i in range(11))  # every message announced
    assert '"note0 ' in script  # long texts are trimmed, not dropped


# --- fakes for the announcement runtime ------------------------------------------


class FakePlaybackQueue:
    """Stands in for the speaker: records puts, drains instantly."""

    order: ClassVar[list[str]] = []  # shared across instances: "chime_put" before "open"
    last_put_length: int = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.put_pcm_length = 0

    def start(self) -> None:
        FakePlaybackQueue.order.append("playback_start")

    def stop(self) -> None:
        return None

    def put_pcm(self, pcm: bytes) -> None:
        self.put_pcm_length = len(pcm)
        FakePlaybackQueue.last_put_length = len(pcm)
        FakePlaybackQueue.order.append("chime_put")

    def clear(self) -> None:
        return None

    def is_playing(self) -> bool:
        return False

    def is_drained(self) -> bool:
        return True

    def wait_until_drained(self, timeout_s: float) -> bool:
        return True


class FakeInputStream:
    """The duplex's input half: records sent chunks, closes cleanly."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.closed = False

    async def send(self, chunk: Any) -> None:
        self.chunks.append(chunk.value.bytes_)

    async def close(self) -> None:
        self.closed = True


class FakeDuplex:
    """Collects the wire events sent in, replays scripted output events."""

    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self.input_stream = FakeInputStream()
        self._outputs = [
            InvokeModelWithBidirectionalStreamOutputChunk(
                value=BidirectionalOutputPayloadPart(bytes_=json.dumps(e).encode("utf-8"))
            )
            for e in outputs
        ]
        self.closed = False

    @property
    def sent(self) -> list[dict[str, Any]]:
        return [json.loads(chunk) for chunk in self.input_stream.chunks]

    async def await_output(self) -> tuple[None, Any]:
        async def gen() -> Any:
            for chunk in self._outputs:
                yield chunk

        return None, gen()

    async def close(self) -> None:
        self.closed = True


class FakeSonicClient:
    """Replaces SonicClient: scripted output, or a failing open."""

    def __init__(self, outputs: list[dict[str, Any]], fail_open: bool = False) -> None:
        self._outputs = outputs
        self._fail_open = fail_open
        self.duplexes: list[Any] = []

    async def open_stream(self, model_id: str) -> tuple[Any, str]:
        FakePlaybackQueue.order.append("open")
        if self._fail_open:
            raise RuntimeError("open refused")
        duplex = FakeDuplex(self._outputs)
        self.duplexes.append(duplex)
        return duplex, model_id


def _runtime(announce_chime: bool = False) -> AssistantRuntime:
    config = AppConfig(
        system_prompt="You are Mike.",
        sonic=SonicConfig(),
        hardware=HardwareConfig(),
        enabled_tools=[],
        messaging=MessagingConfig(announce_chime=announce_chime),
    )
    return AssistantRuntime(config, StubButton(), NullLEDStub())


class NullLEDStub:
    """Records transitions without touching emike.led's real controller."""

    def __init__(self) -> None:
        self.history: list[str] = []

    def set_state(self, state: Any) -> None:
        self.history.append(str(state.value))

    def close(self) -> None:
        return None


def _model_output() -> list[dict[str, Any]]:
    """One spoken response that ends the announcement turn immediately."""
    return [
        {"event": {"contentStart": {"type": "AUDIO", "role": "ASSISTANT"}}},
        {
            "event": {
                "audioOutput": {"content": base64.b64encode(b"\x00\x00" * 48).decode("ascii")}
            }
        },
        {"event": {"contentEnd": {"stopReason": "END_TURN"}}},
        {"event": {"completionEnd": {}}},
    ]


# --- announcement runtime --------------------------------------------------------


def test_successful_announcement_announces_script_and_clears_attempts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("emike.assistant.PlaybackQueue", FakePlaybackQueue)

    def unexpected_mic(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("an announcement must never open the mic")

    monkeypatch.setattr("emike.assistant.MicStreamer", unexpected_mic)
    monkeypatch.setattr("emike.assistant.ANNOUNCE_TEXT_DELAY_SECONDS", 0.01)
    FakePlaybackQueue.order = []
    message = _message(1, chat_id="333", text="pizza at six")

    async def scenario() -> AssistantRuntime:
        runtime = _runtime(announce_chime=True)
        client = FakeSonicClient(_model_output())
        runtime._client = client  # type: ignore[assignment]
        with caplog.at_level(logging.INFO):
            await runtime._run_announcement("req-1", [message])
        return runtime

    runtime = asyncio.run(scenario())

    assert runtime._announce_attempts == {}
    assert FakePlaybackQueue.order[:4] == [
        "playback_start",
        "chime_put",
        "open",
        "playback_start",
    ]  # the ding is played through its own queue before the session opens
    assert "announcement_started" in _events(caplog)
    assert "announcement_completed" in _events(caplog)


def test_announcement_streams_silence_then_the_text_triplet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 0.08 s lets the silence streamer send its first 60 ms chunk before the
    # text lands; the fake session answers instantly once it does.
    monkeypatch.setattr("emike.assistant.PlaybackQueue", FakePlaybackQueue)
    monkeypatch.setattr("emike.assistant.ANNOUNCE_TEXT_DELAY_SECONDS", 0.08)
    message = _message(1, chat_id="333", text='pizza "at" six')

    async def scenario() -> tuple[list[dict[str, Any]], bool]:
        runtime = _runtime()
        client = FakeSonicClient(_model_output())
        runtime._client = client  # type: ignore[assignment]
        await runtime._run_announcement("req-1", [message])
        return client.duplexes[0].sent, client.duplexes[0].closed

    sent, closed = asyncio.run(scenario())

    assert closed  # the session was closed cleanly after the turn
    starts = [e["event"]["contentStart"] for e in sent if "contentStart" in e["event"]]
    text_starts = [s for s in starts if s.get("role") == "USER" and s.get("type") == "TEXT"]
    assert len(text_starts) == 1  # the SYSTEM prompt triplet is TEXT too; only one USER one
    assert text_starts[0]["role"] == "USER"
    assert text_starts[0]["interactive"] is True
    text_inputs = [e["event"]["textInput"] for e in sent if "textInput" in e["event"]]
    announcement_inputs = [t for t in text_inputs if t["content"].startswith("Announce to the household")]
    assert len(announcement_inputs) == 1  # the SYSTEM prompt also travels as textInput
    assert announcement_inputs[0]["content"].startswith("Announce to the household that you have 1 new message from someone")
    assert '"pizza \'at\' six"' in announcement_inputs[0]["content"]
    assert announcement_inputs[0]["content"].count('"') >= 2  # the frame quotes survive
    # The text triplet must use a content name distinct from the open audio one.
    names = [s["contentName"] for s in starts]
    assert len(set(names)) >= 2
    # Silence streams into the audio content before the announcement text
    # arrives (the SYSTEM prompt's textInput is part of the open handshake).
    announcement_index = next(
        i for i, e in enumerate(sent) if "textInput" in e["event"]
        and e["event"]["textInput"]["content"].startswith("Announce to the household")
    )
    first_audio_index = next(i for i, e in enumerate(sent) if "audioInput" in e["event"])
    assert first_audio_index < announcement_index


def test_failed_announcement_requeues_at_front_in_order(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("emike.assistant.PlaybackQueue", FakePlaybackQueue)
    first = _message(1)
    second = _message(2, text="second note")

    async def scenario() -> AssistantRuntime:
        runtime = _runtime()
        runtime._on_telegram_message(first)
        runtime._on_telegram_message(second)

        class ArrivingMidAnnouncement:
            def __init__(self) -> None:
                self._arrived = False

            async def open_stream(self, model_id: str) -> tuple[Any, str]:
                # A fresh message lands while the announcement is running —
                # it must stay queued behind the one being failed.
                if not self._arrived:
                    self._arrived = True
                    runtime._on_telegram_message(_message(99, chat_id="222"))
                raise RuntimeError("open refused")

        runtime._client = ArrivingMidAnnouncement()  # type: ignore[assignment]
        with caplog.at_level(logging.INFO):
            await runtime._run_announcement("req", runtime.drain_inbox())
            # The loop drains before each retry; the mid-announcement arrival
            # rides along in the second batch.
            await runtime._run_announcement("req", runtime.drain_inbox())
        return runtime

    runtime = asyncio.run(scenario())

    # The failed batch went back to the front, in order, ahead of the newer one.
    assert [m.update_id for m in runtime._inbox] == [1, 2, 99]
    assert runtime._announce_attempts == {1: 2, 2: 2, 99: 1}
    events = _events(caplog)
    assert events.count("announcement_error") == 2


def test_third_failure_drops_the_messages(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("emike.assistant.PlaybackQueue", FakePlaybackQueue)

    async def scenario() -> AssistantRuntime:
        runtime = _runtime()
        runtime._client = FakeSonicClient([], fail_open=True)
        runtime._on_telegram_message(_message(1))
        with caplog.at_level(logging.INFO):
            for _ in range(3):  # the loop drains before each retry
                batch = runtime.drain_inbox()
                await runtime._run_announcement("req", batch)
        return runtime

    runtime = asyncio.run(scenario())
    assert runtime._inbox == []
    assert runtime._announce_attempts == {}
    assert _events(caplog).count("announcement_dropped") == 1


def test_press_cancels_an_active_announcement_and_requeues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("emike.assistant.PlaybackQueue", FakePlaybackQueue)
    message = _message(1)

    class NeverOpens:
        async def open_stream(self, model_id: str) -> tuple[Any, str]:
            await asyncio.Event().wait()  # parked like a wedged open

    async def scenario() -> tuple[AssistantRuntime, bool]:
        runtime = _runtime()
        runtime._client = NeverOpens()  # type: ignore[assignment]
        task = asyncio.create_task(runtime._run_announcement("req", [message]))
        await asyncio.sleep(0.05)  # let it wedge in the open
        task.cancel()  # what a press wake does to `_active`
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1.0)
        return runtime, task.cancelled()

    runtime, cancelled = asyncio.run(scenario())
    assert cancelled
    assert runtime._inbox == [message]
    assert runtime._announce_attempts == {1: 1}


def test_on_telegram_message_buffers_and_wakes_the_bus() -> None:
    async def scenario() -> tuple[AssistantRuntime, object]:
        runtime = _runtime()
        runtime._wakes.attach(asyncio.get_running_loop())
        runtime._on_telegram_message(_message(1))
        wake = await asyncio.wait_for(runtime._wakes.wait(), 0.1)
        return runtime, wake

    runtime, wake = asyncio.run(scenario())
    assert [m.update_id for m in runtime._inbox] == [1]
    assert wake is MESSAGE_WAKE


def test_chime_plays_the_rendered_ding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("emike.assistant.PlaybackQueue", FakePlaybackQueue)
    FakePlaybackQueue.order = []

    async def scenario() -> int:
        runtime = _runtime()
        runtime._client = FakeSonicClient(_model_output())
        runtime._config.messaging.announce_chime = True
        await runtime._play_chime()
        return FakePlaybackQueue.last_put_length

    played = asyncio.run(scenario())
    assert played == len(build_message_chime(24_000))


# --- the runtime loop itself -----------------------------------------------------


def test_run_loop_drains_pending_messages_and_cancels_on_press(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full `_run` path: idle + buffered messages announce; press barge-ins.

    `_run_turn`/`_run_announcement` are stubbed on the instance — this test
    pins the loop's wake/drain/cancel wiring, not the session internals.
    """
    runtime = _runtime()
    assert runtime._config.messaging.enabled is False  # messaging off: no poller thread

    announcements: list[list[IncomingMessage]] = []
    started = asyncio.Event()
    cancelled = asyncio.Event()
    turns: list[str] = []

    async def fake_announcement(request_id: str, messages: list[IncomingMessage]) -> None:
        announcements.append(list(messages))
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def fake_turn(request_id: str) -> None:
        turns.append(request_id)

    # Stub the session-side methods: this test pins the loop's wake/drain/
    # cancel wiring, not the Nova session internals (which need no network).
    runtime._run_announcement = fake_announcement  # type: ignore[method-assign]
    runtime._run_turn = fake_turn  # type: ignore[method-assign]

    async def scenario() -> None:
        runner = asyncio.create_task(runtime._run())
        try:
            await _until(lambda: runtime._wakes._queue is not None)
            runtime._on_telegram_message(_message(7))
            await asyncio.wait_for(started.wait(), 0.5)
            runtime._wakes.emit_press()  # barge-in on the announcement
            await asyncio.wait_for(cancelled.wait(), 0.5)
            await _until(lambda: bool(turns))
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())

    # The idle runtime drained the inbox into one announcement, which the
    # press cancelled; push-to-talk then started a turn from that same press.
    assert announcements == [[_message(7)]]
    assert turns  # a fresh turn started after the cancellation


def test_run_loop_waits_for_the_message_wake_when_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message arriving during an active turn must not cancel the turn."""
    runtime = _runtime()
    turn_ends = asyncio.Event()

    async def fake_turn(request_id: str) -> None:
        await turn_ends.wait()  # a long-running turn

    runtime._run_turn = fake_turn  # type: ignore[method-assign]

    async def scenario() -> None:
        runner = asyncio.create_task(runtime._run())
        try:
            await _until(lambda: runtime._wakes._queue is not None)
            runtime._wakes.emit_press()
            await _until(lambda: runtime._active is not None)
            runtime._on_telegram_message(_message(8))  # arrives mid-turn
            await asyncio.sleep(0.05)
            assert not runtime._active.done()  # still running: no cancel
            assert [m.update_id for m in runtime._inbox] == [8]
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())


# --- the announce_during_conversation toggle -------------------------------------


def test_toggle_on_message_cancels_the_active_turn_and_announces_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Toggle on: a message arriving mid-turn interrupts the turn via the
    same cancel path a press takes, and the buffered announce immediately."""
    runtime = _runtime()
    runtime._config.messaging.announce_during_conversation = True
    turn_cancelled = asyncio.Event()
    announcements: list[list[IncomingMessage]] = []

    async def fake_turn(request_id: str) -> None:
        try:
            await asyncio.Event().wait()  # a running turn that never ends
        except asyncio.CancelledError:
            turn_cancelled.set()
            raise

    async def fake_announcement(request_id: str, messages: list[IncomingMessage]) -> None:
        announcements.append(list(messages))

    runtime._run_turn = fake_turn  # type: ignore[method-assign]
    runtime._run_announcement = fake_announcement  # type: ignore[method-assign]

    async def scenario() -> None:
        runner = asyncio.create_task(runtime._run())
        try:
            await _until(lambda: runtime._wakes._queue is not None)
            runtime._wakes.emit_press()
            await _until(lambda: runtime._active is not None)
            runtime._on_telegram_message(_message(9))  # arrives mid-turn
            await asyncio.wait_for(turn_cancelled.wait(), 0.5)
            await _until(lambda: bool(announcements))
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())
    assert turn_cancelled.is_set()
    assert announcements == [[_message(9)]]


def test_toggle_on_message_cancels_an_active_conversation_like_a_press(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In conversation mode a message interrupt behaves like the press that
    ends the conversation: cancel, then the drain announces the messages."""
    runtime = _runtime()
    runtime._config.messaging.announce_during_conversation = True
    runtime._conversation = True
    conversation_cancelled = asyncio.Event()
    announcements: list[list[IncomingMessage]] = []

    async def fake_conversation(request_id: str) -> None:
        try:
            await asyncio.Event().wait()  # an ongoing conversation
        except asyncio.CancelledError:
            conversation_cancelled.set()
            raise

    async def fake_announcement(request_id: str, messages: list[IncomingMessage]) -> None:
        announcements.append(list(messages))

    runtime._run_conversation = fake_conversation  # type: ignore[method-assign]
    runtime._run_announcement = fake_announcement  # type: ignore[method-assign]

    async def scenario() -> None:
        runner = asyncio.create_task(runtime._run())
        try:
            await _until(lambda: runtime._wakes._queue is not None)
            runtime._wakes.emit_press()  # starts the conversation
            await _until(lambda: runtime._active is not None)
            runtime._on_telegram_message(_message(5))  # arrives mid-conversation
            await asyncio.wait_for(conversation_cancelled.wait(), 0.5)
            await _until(lambda: bool(announcements))
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())
    assert conversation_cancelled.is_set()
    assert announcements == [[_message(5)]]


def test_toggle_on_message_never_cancels_an_active_announcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The announcement took a fixed batch at spawn and is itself what will
    read the newly arrived message — a mid-announcement arrival must not
    cancel it; the extra message announces at the next drain instead."""
    runtime = _runtime()
    runtime._config.messaging.announce_during_conversation = True
    announcement_cancelled = asyncio.Event()

    async def fake_announcement(request_id: str, messages: list[IncomingMessage]) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            announcement_cancelled.set()
            raise

    runtime._run_announcement = fake_announcement  # type: ignore[method-assign]

    async def scenario() -> None:
        runner = asyncio.create_task(runtime._run())
        try:
            await _until(lambda: runtime._wakes._queue is not None)
            runtime._on_telegram_message(_message(1))
            await _until(lambda: runtime._active is not None)
            runtime._on_telegram_message(_message(2))  # arrives mid-announcement
            await asyncio.sleep(0.05)
            assert not runtime._active.done()  # the announcement stands
            assert [m.update_id for m in runtime._inbox] == [2]
            assert not announcement_cancelled.is_set()
            # (checked here: cancelling `runner` for cleanup cancels the fake
            # as a side effect of asyncio.run's loop shutdown)
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())


def test_toggle_on_bare_message_wake_with_empty_inbox_does_not_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wake with nothing buffered (e.g. the done-callback's nudge racing a
    drain) must not cancel a running turn — only real buffered messages may."""
    runtime = _runtime()
    runtime._config.messaging.announce_during_conversation = True
    turn_cancelled = asyncio.Event()

    async def fake_turn(request_id: str) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            turn_cancelled.set()
            raise

    runtime._run_turn = fake_turn  # type: ignore[method-assign]

    async def scenario() -> None:
        runner = asyncio.create_task(runtime._run())
        try:
            await _until(lambda: runtime._wakes._queue is not None)
            runtime._wakes.emit_press()
            await _until(lambda: runtime._active is not None)
            runtime._wakes.emit_message()  # empty-inbox nudge mid-turn
            await asyncio.sleep(0.05)
            assert not runtime._active.done()
            # Checked inside the scenario: cancelling `runner` for cleanup
            # cancels the fake via asyncio.run's loop shutdown.
            assert not turn_cancelled.is_set()
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())
