"""Status LED feedback for the assistant state machine.

Patterns (per product spec):
- IDLE: off
- LISTENING: solid
- THINKING: slow blink (1.6 s period) — model processing
- SPEAKING: fast blink (0.5 s period) — audio playing
- ERROR: rapid blink (0.25 s period)

``NullLED`` is used on machines without the pin (dev machines, CI) and simply
records transitions, which makes it a convenient assertion target in tests.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass
from typing import Protocol

LOGGER = logging.getLogger(__name__)


class LEDState(enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ERROR = "error"


@dataclass(frozen=True)
class LEDPattern:
    mode: str  # "off" | "solid" | "blink"
    period_s: float = 0.0  # full blink cycle in seconds; 0 for off/solid


def pattern_for(state: LEDState) -> LEDPattern:
    match state:
        case LEDState.IDLE:
            return LEDPattern(mode="off")
        case LEDState.LISTENING:
            return LEDPattern(mode="solid")
        case LEDState.THINKING:
            return LEDPattern(mode="blink", period_s=1.6)
        case LEDState.SPEAKING:
            return LEDPattern(mode="blink", period_s=0.5)
        case LEDState.ERROR:
            return LEDPattern(mode="blink", period_s=0.25)


class LEDController(Protocol):
    def set_state(self, state: LEDState) -> None: ...

    def close(self) -> None: ...


class NullLED:
    """No-hardware implementation; records state transitions."""

    def __init__(self) -> None:
        self.last_state: LEDState = LEDState.IDLE

    def set_state(self, state: LEDState) -> None:
        self.last_state = state

    def close(self) -> None:
        return None


class GPIOLEDController:
    """Blinker over gpiozero.LED driven by a daemon thread.

    Pattern swaps are atomic; when the pattern is solid/off the thread holds
    the LED at a fixed level, and for blink it toggles each half-period.
    """

    def __init__(self, pin: int) -> None:
        from gpiozero import LED

        self._led = LED(pin)
        self._lock = threading.Lock()
        self._pattern = pattern_for(LEDState.IDLE)
        self._state = LEDState.IDLE
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="led-blinker", daemon=True)
        self._thread.start()

    def set_state(self, state: LEDState) -> None:
        with self._lock:
            self._state = state
            self._pattern = pattern_for(state)

    def close(self) -> None:
        self._stopped.set()
        self._thread.join(timeout=2)
        try:
            self._led.close()
        except (OSError, RuntimeError):
            LOGGER.debug("LED close failed", exc_info=True)

    def _run(self) -> None:
        while not self._stopped.is_set():
            with self._lock:
                pattern = self._pattern
            if pattern.mode == "solid":
                self._led.on()
                self._stopped.wait(0.1)
            elif pattern.mode == "off":
                self._led.off()
                self._stopped.wait(0.1)
            else:
                elapsed = time.monotonic() % pattern.period_s
                on = elapsed < pattern.period_s / 2
                if on:
                    self._led.on()
                else:
                    self._led.off()
                # Sleep the remainder until the next toggle (capped so pattern
                # swaps apply quickly).
                half = pattern.period_s / 2
                next_flip = pattern.period_s - elapsed if on else half - elapsed
                self._stopped.wait(min(max(next_flip, 0.02), 0.1))


def create_led(pin: int | None) -> LEDController:
    """Factory: GPIO LED when possible, NullLED otherwise (never raises)."""
    if pin is None:
        return NullLED()
    try:
        return GPIOLEDController(pin)
    except (ImportError, OSError, RuntimeError) as exc:
        LOGGER.warning("GPIO LED unavailable for pin %s; using no-op LED (%s)", pin, exc)
        return NullLED()
