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

    def __init__(self) -> None:
        self._event = threading.Event()

    def wait_for_press(self) -> None:
        input("Press Enter to start recording...")

    def wait_for_release(self) -> None:
        input("Press Enter to stop recording...")


class KeyboardOrGPIOButton(ButtonInterface):
    """Accept either keyboard input or the real GPIO button as a trigger."""

    def __init__(self, pin: int) -> None:
        self._gpio_button = GPIOButton(pin)
        self._keyboard_button = KeyboardButton()
        self._press_event = threading.Event()
        self._release_event = threading.Event()

        for target in (
            self._wait_for_gpio_press,
            self._wait_for_gpio_release,
            self._wait_for_keyboard_press,
            self._wait_for_keyboard_release,
        ):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()

    def _wait_for_gpio_press(self) -> None:
        self._gpio_button.wait_for_press()
        self._press_event.set()

    def _wait_for_gpio_release(self) -> None:
        self._gpio_button.wait_for_release()
        self._release_event.set()

    def _wait_for_keyboard_press(self) -> None:
        self._keyboard_button.wait_for_press()
        self._press_event.set()

    def _wait_for_keyboard_release(self) -> None:
        self._keyboard_button.wait_for_release()
        self._release_event.set()

    def wait_for_press(self) -> None:
        self._press_event.clear()
        self._press_event.wait()

    def wait_for_release(self) -> None:
        self._release_event.clear()
        self._release_event.wait()
