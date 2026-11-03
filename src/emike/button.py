from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class ButtonInterface:
    """Abstraction so we can run with real GPIO or a keyboard fallback."""

    def wait_for_press(self) -> None:
        raise NotImplementedError

    def wait_for_release(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        """Release any underlying resources."""


class GPIOButton(ButtonInterface):
    def __init__(self, pin: int) -> None:
        from gpiozero import Button

        self._button = Button(pin, pull_up=True, bounce_time=0.05)

    def wait_for_press(self) -> None:
        self._button.wait_for_press()

    def wait_for_release(self) -> None:
        self._button.wait_for_release()

    def close(self) -> None:
        self._button.close()


class KeyboardButton(ButtonInterface):
    """Developer fallback: press Enter to start and Enter again to stop."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def wait_for_press(self) -> None:
        input("Press Enter to start recording...")

    def wait_for_release(self) -> None:
        input("Press Enter to stop recording...")

    def close(self) -> None:
        return None


class KeyboardOrGPIOButton(ButtonInterface):
    """Accept either keyboard input or the real GPIO button as a trigger."""

    def __init__(self, pin: int) -> None:
        self._gpio_button: GPIOButton | None
        try:
            self._gpio_button = GPIOButton(pin)
        except (ImportError, OSError, RuntimeError) as exc:
            self._gpio_button = None
            logger.warning(
                "GPIO button unavailable for pin %s; using keyboard fallback only (%s)",
                pin,
                exc,
            )
        self._keyboard_button = KeyboardButton()
        self._presses = threading.Semaphore(0)
        self._releases = threading.Semaphore(0)

        targets = [self._wait_for_keyboard_press, self._wait_for_keyboard_release]
        if self._gpio_button is not None:
            targets.extend((self._wait_for_gpio_press, self._wait_for_gpio_release))

        for target in targets:
            thread = threading.Thread(target=target, daemon=True)
            thread.start()

    def _wait_for_gpio_press(self) -> None:
        while True:
            self._gpio_button.wait_for_press()
            self._presses.release()

    def _wait_for_gpio_release(self) -> None:
        while True:
            self._gpio_button.wait_for_release()
            self._releases.release()

    def _wait_for_keyboard_press(self) -> None:
        while True:
            self._keyboard_button.wait_for_press()
            self._presses.release()

    def _wait_for_keyboard_release(self) -> None:
        while True:
            self._keyboard_button.wait_for_release()
            self._releases.release()

    def wait_for_press(self) -> None:
        self._presses.acquire()

    def wait_for_release(self) -> None:
        self._releases.acquire()

    def close(self) -> None:
        if self._gpio_button is not None:
            self._gpio_button.close()
