"""Microphone capture for streaming into a Nova Sonic session.

The PortAudio callback thread does exactly one thing — copy the bytes and hand
them to the asyncio loop — so capture never blocks on the network and the
network never blocks capture.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import sounddevice as sd

LOGGER = logging.getLogger(__name__)


class MicStreamer:
    """Stream 16-bit mono PCM from the microphone into an asyncio queue.

    The queue is drop-when-full: losing a chunk while blocked is quieter than
    stalling the session. Consumers call ``get()``/``get_nowait()`` on the
    queue from the event loop.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        out_queue: asyncio.Queue[bytes],
        sample_rate_hz: int = 16_000,
        chunk_ms: int = 60,
    ) -> None:
        self._loop = loop
        self._out_queue = out_queue
        self._sample_rate_hz = sample_rate_hz
        self._chunk_ms = chunk_ms
        self._stream: sd.InputStream | None = None

    def start(self) -> None:
        blocksize = max(1, int(self._sample_rate_hz * self._chunk_ms / 1000))
        self._stream = sd.InputStream(
            samplerate=self._sample_rate_hz,
            channels=1,
            dtype="int16",
            blocksize=blocksize,
            callback=_make_callback(self._loop, self._out_queue),
        )
        self._stream.start()

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()


def _make_callback(
    loop: asyncio.AbstractEventLoop,
    out_queue: asyncio.Queue[bytes],
) -> object:
    """Build the PortAudio callback that hands each block to the event loop."""

    def callback(
        indata: Any,
        _frames: int,
        _time: object,
        _status: object,
    ) -> None:
        try:
            loop.call_soon_threadsafe(_enqueue, out_queue, indata.copy().tobytes())
        except RuntimeError:
            # The loop was closed during shutdown; nothing to deliver to.
            pass

    return callback


def _enqueue(out_queue: asyncio.Queue[bytes], chunk: bytes) -> None:
    try:
        out_queue.put_nowait(chunk)
    except asyncio.QueueFull:
        LOGGER.debug("Mic capture queue full; dropping chunk of %d bytes", len(chunk))
