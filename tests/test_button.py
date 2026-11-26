import threading
import time

from emike.button import KeyboardButton, KeyboardOrGPIOButton


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


def test_keyboard_push_to_talk_releases_only_on_the_stop_edge(monkeypatch) -> None:
    """Enter #1 must read as a press only; Enter #2 is the turn's release.

    Releasing both semaphores per Enter parks a release token that the
    ReleaseCounter pump consumes instantly, ending every push-to-talk turn
    on its first audio chunk.
    """
    enter_2 = threading.Event()
    calls: list[int] = []

    def fake_input(_prompt: str) -> str:
        calls.append(1)
        if len(calls) == 1:
            return ""  # Enter #1
        enter_2.wait()  # Enter #2 waits for the main thread's assertions
        if len(calls) == 2:
            return ""
        raise EOFError  # Enter #3 ends the reader (never reached in this test)

    monkeypatch.setattr("builtins.input", fake_input)
    button = KeyboardButton(alternating=True)

    button.wait_for_press()  # blocks until the reader processes Enter #1
    released_early = button._releases.acquire(timeout=0.1)
    assert not released_early, "the start edge must not signal the release"

    enter_2.set()
    released = button._releases.acquire(timeout=2.0)
    assert released, "the stop edge must signal the release"


def test_keyboard_conversation_mode_one_press_per_enter(monkeypatch) -> None:
    calls: list[int] = []

    def fake_input(_prompt: str) -> str:
        calls.append(1)
        if len(calls) == 1:
            return ""  # Enter #1
        raise EOFError  # reader stops; this test sends exactly one Enter

    monkeypatch.setattr("builtins.input", fake_input)
    button = KeyboardButton(alternating=False)
    button.wait_for_press()  # Enter #1 processed

    pressed_again = button._presses.acquire(timeout=0.1)
    released = button._releases.acquire(timeout=0.1)
    assert not pressed_again and not released


def test_keyboard_or_gpio_button_falls_back_when_gpio_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        "emike.button.GPIOButton",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("gpio init failed")),
    )
    monkeypatch.setattr("emike.button.KeyboardButton.wait_for_press", lambda self: None)
    monkeypatch.setattr("emike.button.KeyboardButton.wait_for_release", lambda self: None)

    button = KeyboardOrGPIOButton(pin=17)

    assert button._gpio_button is None

