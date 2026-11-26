from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


def _is_pin_factory_error(exc: BaseException) -> bool:
    return "Unable to load any default pin factory" in str(exc)


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
        if self._button.is_pressed:
            # A stuck button would otherwise start (or end) turn after turn
            # from the moment the service boots, reading as mysterious
            # session churn; surface the wiring problem loudly instead.
            logger.warning(
                "GPIO button on pin %s reads pressed at startup — it will act "
                "as if constantly held; check the button wiring",
                pin,
                extra={"event": "gpio_pressed_at_startup", "pin": pin},
            )

    def wait_for_press(self) -> None:
        self._button.wait_for_press()

    def wait_for_release(self) -> None:
        self._button.wait_for_release()

    def close(self) -> None:
        self._button.close()


class KeyboardButton(ButtonInterface):
    """Keyboard trigger.

    In conversation mode (default for --keyboard) **every** Enter is a
    toggle: first press starts the conversation, the next one ends it.
    A single reader thread owns stdin, so an Enter can never be stolen by
    a competing release reader. In alternating (push-to-talk) mode, each
    Enter starts one turn and the next one releases it, mimicking GPIO.
    """

    def __init__(self, alternating: bool = False) -> None:
        self._alternating = alternating
        self._presses = threading.Semaphore(0)
        self._releases = threading.Semaphore(0)
        self._listening = False  # push-to-talk: which half of the toggle we are in
        self._closed = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while not self._closed.is_set():
            try:
                prompt = (
                    "Press Enter to start or end the conversation..."
                    if not self._alternating
                    else "Press Enter to start recording..."
                    if not self._listening
                    else "Press Enter to stop recording..."
                )
                input(prompt)
            except EOFError:
                print("Keyboard input ended (EOF); re-run for a new session.")
                return
            except OSError:
                # EOF is not the only way stdin dies: a closed or non-tty
                # descriptor raises OSError (pytest capture, a service unit
                # with no controlling terminal). Same outcome as EOF.
                print("Keyboard input unavailable; re-run for a new session.")
                return
            self._listening = not self._listening
            if self._alternating:
                # Each Enter is one edge of a turn: the start edge signals the
                # press, the stop edge signals the release. Releasing both per
                # Enter would park a release token until ReleaseCounter's
                # loop consumes it, ending the turn on its first audio chunk.
                if self._listening:
                    self._presses.release()
                else:
                    self._releases.release()
            else:
                # Conversation mode: every Enter is a press (start or end a
                # conversation — the runtime's cancel path treats it the same).
                self._presses.release()

    def wait_for_press(self) -> None:
        self._presses.acquire()

    def wait_for_release(self) -> None:
        self._releases.acquire()

    def close(self) -> None:
        self._closed.set()


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
            if _is_pin_factory_error(exc):
                logger.warning(
                    "GPIO pin factory is unavailable. On Raspberry Pi, install a GPIO backend "
                    "inside your venv (for example: pip install rpi-lgpio) and verify wiring "
                    "with: python3 -c \"from gpiozero import Button; b=Button(%s); "
                    "b.wait_for_press(); print('pressed'); b.wait_for_release(); print('released')\"",
                    pin,
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
        gpio_button = self._gpio_button
        if gpio_button is None:
            return

        while True:
            gpio_button.wait_for_press()
            self._presses.release()

    def _wait_for_gpio_release(self) -> None:
        gpio_button = self._gpio_button
        if gpio_button is None:
            return

        while True:
            gpio_button.wait_for_release()
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
