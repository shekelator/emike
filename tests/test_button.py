import logging
import threading
import time

from emike.button import KeyboardOrGPIOButton


def test_keyboard_or_gpio_button_waits_for_press() -> None:
    button = KeyboardOrGPIOButton.__new__(KeyboardOrGPIOButton)
    button._presses = threading.Semaphore(0)
    button._releases = threading.Semaphore(0)

    def trigger_press() -> None:
        time.sleep(0.05)
        button._presses.release()

    thread = threading.Thread(target=trigger_press)
    thread.start()

    button.wait_for_press()

    thread.join(timeout=1)
    assert not thread.is_alive()


def test_keyboard_or_gpio_button_waits_for_release() -> None:
    button = KeyboardOrGPIOButton.__new__(KeyboardOrGPIOButton)
    button._presses = threading.Semaphore(0)
    button._releases = threading.Semaphore(0)

    def trigger_release() -> None:
        time.sleep(0.05)
        button._releases.release()

    thread = threading.Thread(target=trigger_release)
    thread.start()

    button.wait_for_release()

    thread.join(timeout=1)
    assert not thread.is_alive()


def test_keyboard_or_gpio_button_falls_back_when_gpio_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        "emike.button.GPIOButton",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("gpio init failed")),
    )
    monkeypatch.setattr("emike.button.KeyboardButton.wait_for_press", lambda self: None)
    monkeypatch.setattr("emike.button.KeyboardButton.wait_for_release", lambda self: None)

    button = KeyboardOrGPIOButton(pin=17)

    assert button._gpio_button is None


def test_keyboard_or_gpio_button_logs_pin_factory_help(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        "emike.button.GPIOButton",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("Unable to load any default pin factory!")
        ),
    )
    monkeypatch.setattr("emike.button.KeyboardButton.wait_for_press", lambda self: None)
    monkeypatch.setattr("emike.button.KeyboardButton.wait_for_release", lambda self: None)

    with caplog.at_level(logging.WARNING):
        KeyboardOrGPIOButton(pin=17)

    assert "install a GPIO backend inside your venv" in caplog.text
    assert "pip install rpi-lgpio" in caplog.text
