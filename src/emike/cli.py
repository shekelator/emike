from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from emike.assistant import AssistantRuntime
from emike.button import GPIOButton, KeyboardButton
from emike.config import DEFAULT_CONFIG_PATH, load_config
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
    args = parser.parse_args()

    load_dotenv()
    config = load_config(args.config)
    configure_logging(debug_mode=config.debug_mode)

    if not args.keyboard and os.name != "posix":
        raise RuntimeError("GPIO mode requires Linux on Raspberry Pi. Use --keyboard.")

    button = KeyboardButton() if args.keyboard else GPIOButton(config.hardware.button_gpio)
    runtime = AssistantRuntime(config=config, button=button)
    runtime.run_forever()


if __name__ == "__main__":
    main()
