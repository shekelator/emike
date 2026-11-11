from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from emike.assistant import AssistantRuntime
from emike.button import KeyboardButton, KeyboardOrGPIOButton
from emike.config import DEFAULT_CONFIG_PATH, load_config
from emike.led import create_led
from emike.logging_utils import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Run emike voice assistant")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to YAML config",
    )
    parser.add_argument(
        "--keyboard",
        action="store_true",
        help="Use keyboard simulation instead of GPIO",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Force DEBUG-level logging (overrides debug_mode in the config)",
    )
    args = parser.parse_args()

    load_dotenv()
    config = load_config(args.config)
    configure_logging(debug_mode=config.debug_mode or args.debug)

    if not args.keyboard and os.name != "posix":
        raise RuntimeError("GPIO mode requires Linux on Raspberry Pi. Use --keyboard.")

    button = (
        KeyboardButton() if args.keyboard else KeyboardOrGPIOButton(config.hardware.button_gpio)
    )
    led = create_led(config.hardware.led_gpio)
    runtime = AssistantRuntime(config=config, button=button, led=led)
    try:
        runtime.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        button.close()
        led.close()


if __name__ == "__main__":
    main()
