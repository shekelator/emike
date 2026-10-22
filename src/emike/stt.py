from __future__ import annotations

import numpy as np
from faster_whisper import WhisperModel

from emike.config import STTConfig


class SpeechToText:
    def __init__(self, config: STTConfig, sample_rate_hz: int = 16_000) -> None:
        self._config = config
        self._sample_rate_hz = sample_rate_hz
        self._model = WhisperModel(
            config.model,
            device=config.device,
            compute_type=config.compute_type,
        )

    def transcribe(self, pcm: np.ndarray) -> str:
        if len(pcm) < int(self._sample_rate_hz * 0.35):
            return ""

        audio = pcm.astype(np.float32) / 32768.0
        segments, _ = self._model.transcribe(
            audio,
            language=self._config.language,
            vad_filter=True,
            beam_size=1,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return text
