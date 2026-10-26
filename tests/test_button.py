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
