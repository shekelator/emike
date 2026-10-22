from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np
import sounddevice as sd

from emike.button import ButtonInterface


class AudioRecorder:
    def __init__(self, sample_rate_hz: int = 16_000) -> None:
        self._sample_rate_hz = sample_rate_hz

    def record_while_button_held(self, button: ButtonInterface) -> np.ndarray:
        chunks: list[np.ndarray[Any, np.dtype[np.int16]]] = []
        released = threading.Event()

        def on_release() -> None:
            button.wait_for_release()
            released.set()

        def callback(indata: np.ndarray[Any, np.dtype[np.int16]], *_: object) -> None:
            chunks.append(indata.copy())

        thread = threading.Thread(target=on_release, daemon=True)
        thread.start()

        with sd.InputStream(
            samplerate=self._sample_rate_hz,
            channels=1,
            dtype="int16",
            callback=callback,
        ):
            while not released.is_set():
                time.sleep(0.02)

        if not chunks:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(chunks, axis=0).reshape(-1)
