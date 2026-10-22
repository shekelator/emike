from __future__ import annotations

import threading


class ButtonInterface:
    """Abstraction so we can run with real GPIO or a keyboard fallback."""

    def wait_for_press(self) -> None:
        raise NotImplementedError

    def wait_for_release(self) -> None:
        raise NotImplementedError


class GPIOButton(ButtonInterface):
    def __init__(self, pin: int) -> None:
        from gpiozero import Button

        self._button = Button(pin, pull_up=True, bounce_time=0.05)

    def wait_for_press(self) -> None:
        self._button.wait_for_press()

    def wait_for_release(self) -> None:
        self._button.wait_for_release()


class KeyboardButton(ButtonInterface):
    """Developer fallback: press Enter to start and Enter again to stop."""

    _event: threading.Event

    def __init__(self) -> None:
        self._event = threading.Event()

    def wait_for_press(self) -> None:
        input("Press Enter to start recording...")

    def wait_for_release(self) -> None:
        input("Press Enter to stop recording...")
