"""Interruptible streaming playback for Nova Sonic audio output.

The event-loop side pushes decoded PCM bytes in; the PortAudio callback thread
pulls them out. Pure threading primitives — no event-loop references — so the
loop thread and the audio thread cooperate safely.

Prebuffering: the callback emits nothing until ``prebuffer_bytes`` have been
queued (the model generates audio faster than real time), which avoids hearing
an underflow gap at the start of each response. ``clear()`` drops everything
instantly for barge-in.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

import numpy as np
import sounddevice as sd

LOGGER = logging.getLogger(__name__)


class PlaybackQueue:
    def __init__(
        self,
        sample_rate_hz: int = 24_000,
        prebuffer_ms: int = 150,
        device: str | int | None = None,
    ) -> None:
        self._sample_rate_hz = sample_rate_hz
        self._prebuffer_bytes = sample_rate_hz * 2 * prebuffer_ms // 1000
        self._chunks: deque[bytes] = deque()
        self._queued_bytes = 0
        self._started_playing = False
        self._playing = False
        self._condition = threading.Condition()
        self._stream: sd.OutputStream | None = None
        self._device = device

    def start(self) -> None:
        self._stream = sd.OutputStream(
            samplerate=self._sample_rate_hz,
            channels=1,
            dtype="int16",
            device=self._device,
            callback=_playback_callback(self),
        )
        self._stream.start()
        try:
            device = str(sd.query_devices(self._stream.device).get("name", self._stream.device))
        except Exception:  # noqa: BLE001 - diagnostics must never break playback
            device = str(self._stream.device)
        LOGGER.info(
            "Speaker open: %s (%d Hz)",
            device,
            self._sample_rate_hz,
            extra={"event": "speaker_opened", "device": device},
        )

    def stop(self) -> None:
        self._stream, stream = None, self._stream
        if stream is not None:
            stream.close()
        with self._condition:
            self._chunks.clear()
            self._queued_bytes = 0
            self._playing = False
            self._started_playing = False
            self._condition.notify_all()

    def put_pcm(self, pcm: bytes) -> None:
        if pcm:
            with self._condition:
                self._chunks.append(pcm)
                self._queued_bytes += len(pcm)
                self._condition.notify_all()

    def clear(self) -> None:
        with self._condition:
            self._chunks.clear()
            self._queued_bytes = 0
            self._playing = False
            self._started_playing = False
            self._condition.notify_all()

    def is_drained(self) -> bool:
        with self._condition:
            return self._started_playing and self._queued_bytes == 0

    def wait_until_drained(self, timeout_s: float) -> bool:
        """Block until playback of everything queued so far finishes."""
        deadline = _monotonic() + timeout_s
        with self._condition:
            while not (self._started_playing and self._queued_bytes == 0):
                remaining = deadline - _monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def _pull(self, frames: int) -> np.ndarray:
        """Called from the PortAudio callback thread only."""
        with self._condition:
            if not self._started_playing:
                if self._queued_bytes < self._prebuffer_bytes:
                    return np.zeros(frames, dtype=np.int16)
                self._started_playing = True
                self._playing = True
                LOGGER.info(
                    "Playback started (%d ms of audio buffered)",
                    self._prebuffer_bytes * 1000 // (self._sample_rate_hz * 2),
                )
            out = np.empty(frames, dtype=np.int16)
            filled = 0
            while filled < frames and self._chunks:
                chunk_bytes = self._chunks[0]
                chunk = np.frombuffer(chunk_bytes, dtype=np.int16)
                take = min(frames - filled, len(chunk))
                out[filled : filled + take] = chunk[:take]
                filled += take
                if take == len(chunk):
                    self._chunks.popleft()
                    self._queued_bytes -= len(chunk_bytes)
                else:
                    remaining_bytes = self._chunks.popleft()[take * 2 :]
                    if remaining_bytes:
                        self._chunks.appendleft(remaining_bytes)
                        self._queued_bytes -= take * 2
                    else:
                        self._queued_bytes -= len(chunk_bytes)
            if filled < frames:
                out[filled:] = 0
                if self._queued_bytes == 0:
                    self._playing = False
                    self._condition.notify_all()
            return out


def _playback_callback(playback: PlaybackQueue) -> object:
    def callback(
        outdata: Any,
        frames: int,
        _time: object,
        _status: object,
    ) -> None:
        outdata[:] = playback._pull(frames).reshape(-1, 1)

    return callback


def _monotonic() -> float:
    return time.monotonic()
