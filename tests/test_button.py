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
