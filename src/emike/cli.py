from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from emike.assistant import AssistantRuntime
from emike.button import KeyboardButton, KeyboardOrGPIOButton
from emike.config import DEFAULT_CONFIG_PATH, load_config
from emike.led import create_led

LOGGER = logging.getLogger(__name__)
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
        "--push-to-talk",
        action="store_true",
        help="Keyboard fallback: end the user turn on each Enter instead of "
        "running one continuous conversation (matches GPIO behavior)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Force DEBUG-level logging (overrides debug_mode in the config)",
    )
    args = parser.parse_args()

    env_loaded = load_dotenv()
    config = load_config(args.config)
    configure_logging(debug_mode=config.debug_mode or args.debug)
    # Names only, never values — this answers "did the credentials even reach
    # the process" (the classic case: launching from a directory without .env,
    # or a systemd unit whose EnvironmentFile path is wrong), which otherwise
    # shows up as open timeouts from IdentityChain probing IMDS.
    LOGGER.info(
        "Startup environment check",
        extra={
            "event": "startup_env",
            "env_file_loaded": bool(env_loaded),
            "aws_key_id_present": bool(os.getenv("AWS_ACCESS_KEY_ID")),
            "aws_secret_present": bool(os.getenv("AWS_SECRET_ACCESS_KEY")),
            "aws_session_token_present": bool(os.getenv("AWS_SESSION_TOKEN")),
            "telegram_token_present": bool(os.getenv("TELEGRAM_BOT_TOKEN")),
        },
    )

    if not args.keyboard and os.name != "posix":
        raise RuntimeError("GPIO mode requires Linux on Raspberry Pi. Use --keyboard.")

    button = (
        KeyboardButton(alternating=args.push_to_talk)
        if args.keyboard
        else KeyboardOrGPIOButton(config.hardware.button_gpio)
    )
    led = create_led(config.hardware.led_gpio)
    runtime = AssistantRuntime(
        config=config,
        button=button,
        led=led,
        # Keyboard development runs one continuous conversation (Enter starts,
        # Enter again ends); GPIO stays push-to-talk per the hardware design.
        conversation=args.keyboard and not args.push_to_talk,
    )
    try:
        runtime.run_forever()
        # Normally reached: the runtime's loop-level handler cancels its main
        # task on SIGINT/SIGTERM and run_forever returns cleanly, so the
        # finally below (and the runtime's own close-path guards) always run.
        # This matters for the button: close() releases its state for the
        # *next* run of the process. The KeyboardInterrupt branch is only the
        # fallback for platforms without loop signal handlers (Windows).
    except KeyboardInterrupt:
        pass
    finally:
        button.close()
        led.close()


if __name__ == "__main__":
    main()
