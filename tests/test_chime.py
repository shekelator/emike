"""Tests for the synthesized message chime (pure audio math, no devices)."""

import numpy as np

from emike.chime import AMPLITUDE, TONE_HZ, build_message_chime

RATE = 24_000


def test_chime_pcm_length_matches_the_two_tone_layout() -> None:
    pcm = build_message_chime(RATE)
    assert len(pcm) == 2 * 2 * RATE * 300 // 1000  # two 300 ms int16 mono tones


def test_chime_is_two_rising_tones() -> None:
    samples = np.frombuffer(build_message_chime(RATE), dtype="<i2")

    def frequency_hz(start_ms: int) -> float:
        window = samples[int(start_ms * RATE / 1000) : int((start_ms + 200) * RATE / 1000)]
        crossings = np.sum((window[:-1] > 0) != (window[1:] > 0))
        return float(crossings) / 2.0 / (len(window) / RATE)  # 2 crossings per cycle

    first = frequency_hz(20)  # inside the decaying first tone (E5)
    second = frequency_hz(320)  # inside the decaying second tone (A5)
    # Zero-crossing counts on the decayed tails drift a little in int16.
    assert abs(first - TONE_HZ[0]) < 30
    assert abs(second - TONE_HZ[1]) < 30
    assert first < second


def test_chime_peaks_at_a_soft_third_of_full_scale() -> None:
    samples = np.frombuffer(build_message_chime(RATE), dtype="<i2")
    assert samples.max() <= 32_767 and samples.min() >= -32_768
    peak = max(abs(int(samples.max())), abs(int(samples.min())))
    assert 0.9 * AMPLITUDE * 32_767 <= peak <= 32_767


def test_chime_rises_softly_from_silence() -> None:
    samples = np.frombuffer(build_message_chime(RATE), dtype="<i2")
    peak = max(abs(int(samples.max())), abs(int(samples.min())))
    start_window = samples[: int(0.005 * RATE)]  # first 5 ms of the attack
    # Nonzero (a real rising ramp) but only a small fraction of the peak.
    start_peak = max(abs(int(start_window.max())), abs(int(start_window.min())))
    assert start_peak > 0
    assert start_peak < 0.25 * peak


def test_chime_tones_join_at_silence_and_ends_silent() -> None:
    """No clicks: the boundary between tones and the end fade sit at ~zero."""
    samples = np.frombuffer(build_message_chime(RATE), dtype="<i2")
    join = samples[int(0.296 * RATE) : int(0.303 * RATE)]  # ~last ms of E5, first of A5
    assert max(abs(int(join.max())), abs(int(join.min()))) < 0.1 * AMPLITUDE * 32_767
    assert samples[-1] == 0


def test_chime_rejects_zero_sample_rate() -> None:
    try:
        build_message_chime(0)
    except ValueError as exc:
        assert "sample_rate_hz" in str(exc)
    else:
        raise AssertionError("expected ValueError")
