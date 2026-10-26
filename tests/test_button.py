import threading
import time

from emike.button import KeyboardOrGPIOButton


def test_keyboard_or_gpio_button_waits_for_press() -> None:
    button = KeyboardOrGPIOButton.__new__(KeyboardOrGPIOButton)
    button._press_event = threading.Event()
    button._release_event = threading.Event()

    def trigger_press() -> None:
        time.sleep(0.05)
        button._press_event.set()

    thread = threading.Thread(target=trigger_press)
    thread.start()

    button.wait_for_press()

    assert button._press_event.is_set()
    thread.join(timeout=1)


def test_keyboard_or_gpio_button_waits_for_release() -> None:
    button = KeyboardOrGPIOButton.__new__(KeyboardOrGPIOButton)
    button._press_event = threading.Event()
    button._release_event = threading.Event()

    def trigger_release() -> None:
        time.sleep(0.05)
        button._release_event.set()

    thread = threading.Thread(target=trigger_release)
    thread.start()

    button.wait_for_release()

    assert button._release_event.is_set()
    thread.join(timeout=1)


def test_keyboard_or_gpio_button_falls_back_when_gpio_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        "emike.button.GPIOButton",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("gpio init failed")),
    )
    monkeypatch.setattr("emike.button.KeyboardButton.wait_for_press", lambda self: None)
    monkeypatch.setattr("emike.button.KeyboardButton.wait_for_release", lambda self: None)

    button = KeyboardOrGPIOButton(pin=17)

    assert button._gpio_button is None
