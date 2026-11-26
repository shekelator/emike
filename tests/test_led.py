from emike.led import LEDPattern, LEDState, NullLED, create_led, pattern_for


def test_pattern_for_state_table() -> None:
    assert pattern_for(LEDState.IDLE) == LEDPattern(mode="off")
    assert pattern_for(LEDState.LISTENING) == LEDPattern(mode="solid")
    thinking = pattern_for(LEDState.THINKING)
    speaking = pattern_for(LEDState.SPEAKING)
    error = pattern_for(LEDState.ERROR)
    assert thinking.mode == speaking.mode == error.mode == "blink"
    # Thinking breathes slowly, speaking flickers fast, error is fastest.
    assert thinking.period_s > speaking.period_s > error.period_s


def test_null_led_records_state_transitions() -> None:
    led = NullLED()
    assert led.last_state == LEDState.IDLE
    led.set_state(LEDState.LISTENING)
    assert led.last_state == LEDState.LISTENING
    led.set_state(LEDState.ERROR)
    assert led.last_state == LEDState.ERROR
    led.close()


def test_create_led_factory_falls_back_without_hardware() -> None:
    assert isinstance(create_led(None), NullLED)


def test_create_led_factory_never_raises_for_bogus_pin() -> None:
    # gpiozero is unavailable on most dev machines; on a Pi, pin 9999 is out of
    # range. Either path must land on NullLED.
    led = create_led(9999)
    assert isinstance(led, NullLED) or type(led).__name__ == "GPIOLEDController"
    led.close()
