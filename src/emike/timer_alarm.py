"""Timer alarm audio, synthesized from scratch — no bundled WAV files.

``build_timer_alarm_pcm`` renders a microwave-style alarm: repeating groups of
three two-tone beeps with silence between groups, so a fired timer is genuinely
noticeable but not eardrum-shattering. Pure function: 24 kHz-style mono,
little-endian int16 PCM bytes, the same format ``PlaybackQueue.put_pcm`` eats.
"""

from __future__ import annotations

import numpy as np

# Oven-timer pattern: beep [gap] beep [gap] beep [long gap], repeat.
BEEP_HZ = (880, 1175)
BEEP_MS = 180
GAP_MS = 140
GROUP_GAP_MS = 750
# Peak amplitude as a fraction of full scale: loud enough to ignore, not a klaxon.
AMPLITUDE = 0.35
FADE_MS = 8  # raised edges per beep so each one starts/ends silently (no clicks)


def build_timer_alarm_pcm(sample_rate_hz: int, duration_seconds: int) -> bytes:
    """Render ``duration_seconds`` of alarm tone as raw int16 mono PCM bytes."""
    if sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be positive")
    if duration_seconds < 1:
        raise ValueError("duration_seconds must be at least 1")

    total = int(sample_rate_hz * duration_seconds)
    samples = np.zeros(total, dtype=np.int16)

    cursor = 0
    while cursor < total:
        for beep_index in range(3):
            beep = _beep(sample_rate_hz, BEEP_HZ[beep_index % 2], BEEP_MS)
            end = min(cursor + len(beep), total)
            samples[cursor:end] = beep[: end - cursor]
            cursor = min(end + _ms_to_samples(sample_rate_hz, GAP_MS), total)
        cursor = min(cursor + _ms_to_samples(sample_rate_hz, GROUP_GAP_MS), total)

    # The pattern rarely lands exactly on the end; fade whatever the truncation
    # left behind so the alarm never stops mid-cycle with a hard click.
    tail = min(total, max(1, int(sample_rate_hz * FADE_MS / 1000)))
    samples[-tail:] = (samples[-tail:].astype(np.float64) * np.linspace(1.0, 0.0, tail)).astype(
        np.int16
    )
    return samples.tobytes()


def _beep(sample_rate_hz: int, frequency_hz: int, duration_ms: int) -> np.ndarray:
    count = _ms_to_samples(sample_rate_hz, duration_ms)
    time = np.arange(count) / sample_rate_hz
    wave = np.sin(2.0 * np.pi * frequency_hz * time)
    fade = min(count // 2, max(1, int(sample_rate_hz * FADE_MS / 1000)))
    envelope = np.ones(count)
    envelope[:fade] = np.linspace(0.0, 1.0, fade)
    envelope[-fade:] = np.linspace(1.0, 0.0, fade)
    return (wave * envelope * AMPLITUDE * 32_767).astype(np.int16)


def _ms_to_samples(sample_rate_hz: int, duration_ms: int) -> int:
    return int(sample_rate_hz * duration_ms / 1000)
