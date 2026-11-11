"""Message-notification chime, synthesized from scratch — no bundled audio.

``build_message_chime`` renders a soft two-tone "ding" (E5 → A5, ~0.6 s) that
announces an incoming phone message before the assistant reads it. Pure
function: 24 kHz-style mono, little-endian int16 PCM bytes, the same format
``PlaybackQueue.put_pcm`` eats — shaped like ``emike.timer_alarm``.
"""

from __future__ import annotations

import numpy as np

# Two soft tones: E5 then A5, each in its own decaying window.
TONE_HZ = (659, 880)
TONE_MS = 300
# Peak amplitude as a fraction of full scale: a clear cue, not an alarm.
AMPLITUDE = 0.30
# Soft exponential attack so the ding rises without a percussive edge, and a
# short linear fade per tone so tones never click at their boundaries.
ATTACK_MS = 30
FADE_MS = 8
# Each tone decays across its window after the attack; four decades at the end
# keep the join to the next tone (and the end of the chime) well below hearing.
DECAY_DEPTH = 4.0


def build_message_chime(sample_rate_hz: int = 24_000) -> bytes:
    """Render the two-tone ding as raw int16 mono PCM bytes."""
    if sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be positive")

    tones = []
    for frequency_hz in TONE_HZ:
        count = _ms_to_samples(sample_rate_hz, TONE_MS)
        time = np.arange(count) / sample_rate_hz
        wave = np.sin(2.0 * np.pi * frequency_hz * time)
        tones.append(wave * _ding_envelope(count, sample_rate_hz))
    samples = (np.concatenate(tones) * AMPLITUDE * 32_767).astype(np.int16)
    return samples.tobytes()


def _ding_envelope(count: int, sample_rate_hz: int) -> np.ndarray:
    """Half-cosine rise, exponential decay, then a hard-silent fade per tone.

    The attack starts with zero slope, so a full-scale sine cannot click on the
    way in, and the final fade puts each tone boundary at true zero.
    """
    envelope = np.zeros(count)
    attack = min(count - 1, max(1, _ms_to_samples(sample_rate_hz, ATTACK_MS)))
    envelope[:attack] = 0.5 - 0.5 * np.cos(np.pi * np.linspace(0.0, 1.0, attack))
    envelope[attack:] = np.exp(-DECAY_DEPTH * np.linspace(0.0, 1.0, count - attack))
    fade = min(count, max(1, _ms_to_samples(sample_rate_hz, FADE_MS)))
    envelope[-fade:] *= np.linspace(1.0, 0.0, fade)
    return envelope


def _ms_to_samples(sample_rate_hz: int, duration_ms: int) -> int:
    return int(sample_rate_hz * duration_ms / 1000)
