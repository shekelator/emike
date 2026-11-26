import threading
import time

import numpy as np

from emike.timer_alarm import AMPLITUDE, build_timer_alarm_pcm
from emike.tools import TimerManager

RATE = 24_000


def test_alarm_pcm_length_matches_sample_math() -> None:
    for seconds in (1, 2, 6):
        pcm = build_timer_alarm_pcm(RATE, seconds)
        assert len(pcm) == seconds * RATE * 2  # int16 = 2 bytes/sample, mono


def test_alarm_pcm_int16_range_and_audible_peak() -> None:
    samples = np.frombuffer(build_timer_alarm_pcm(RATE, 3), dtype="<i2")
    assert samples.max() <= 32_767 and samples.min() >= -32_768
    peak = max(abs(int(samples.max())), abs(int(samples.min())))
    # Loud — around 1/3 of full scale — but with headroom, never clipping.
    full_scale = AMPLITUDE * 32_767
    assert 0.9 * full_scale <= peak <= 32_767


def test_alarm_pcm_alternates_beep_bursts_with_silence() -> None:
    samples = np.frombuffer(build_timer_alarm_pcm(RATE, 6), dtype="<i2")

    def window_peak(start_seconds: float, seconds: float) -> int:
        start = int(start_seconds * RATE)
        end = start + int(seconds * RATE)
        window = samples[start:end]
        return int(max(abs(window.max()), abs(window.min()))) if len(window) else 0

    # First beep (880 Hz, 180 ms) is well inside the first tone burst.
    assert window_peak(0.02, 0.1) > 10_000
    # The short gap between beeps 1 and 2 is a proper hole of silence.
    assert window_peak(0.19, 0.12) == 0
    # The long gap between beep groups (3 x 180 ms + 2 x 140 ms = 820 ms)
    # is silent too, so the repetition shape is audible.
    assert window_peak(0.83, 0.6) == 0
    # The pattern repeats: the second group starts beeping again
    # (one cycle = 3 x 180 ms beeps + 3 x 140 ms gaps + 750 ms group gap
    # = 1.71 s, so group 2 begins at 1.71 s).
    assert window_peak(1.72, 0.1) > 10_000


def test_alarm_pcm_differing_beep_tones() -> None:
    samples = np.frombuffer(build_timer_alarm_pcm(RATE, 1), dtype="<i2")
    beep_one = samples[int(0.02 * RATE) : int(0.18 * RATE)]  # 880 Hz burst
    beep_two = samples[int(0.32 * RATE) : int(0.48 * RATE)]  # 1175 Hz burst

    def frequency_hz(window: np.ndarray) -> float:
        crossings = np.sum((window[:-1] > 0) != (window[1:] > 0))
        return float(crossings) / 2.0 / (len(window) / RATE)  # 2 crossings per cycle

    first_rate = frequency_hz(beep_one)
    second_rate = frequency_hz(beep_two)
    assert first_rate < second_rate
    assert 700 < first_rate < 1100
    assert 1000 < second_rate < 1350


def test_alarm_pcm_rejects_zero_duration() -> None:
    try:
        build_timer_alarm_pcm(RATE, 0)
    except ValueError as exc:
        assert "duration" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_timer_manager_calls_back_with_fired_timer() -> None:
    received: list[object] = []
    done = threading.Event()

    def on_elapsed(timer: object) -> None:
        received.append(timer)
        done.set()

    manager = TimerManager(on_elapsed)
    active = manager.create_timer(0.05, "tea")
    assert done.wait(2.0)
    assert received[0] is active
    assert active.label == "tea" and active.duration_seconds == 0.05
    # The fired timer is removed from the active list.
    assert manager.list_timers() == []


def test_timer_manager_cancelled_timer_never_fires() -> None:
    received: list[object] = []
    manager = TimerManager(received.append)

    active = manager.create_timer(0.05, "")
    assert manager.cancel_timer(active.timer_id) is True
    assert manager.cancel_timer(active.timer_id) is False
    time.sleep(0.3)
    assert received == []
    assert manager.list_timers() == []
