"""Print the Telegram chat ids your bot can observe (one-time setup helper).

Run inside the project venv (from the repo root) after sending your bot any
message from your phone:
    .venv/bin/python scripts/telegram/print-chat-ids.py

It performs one long-poll getUpdates call and prints each observed chat id,
first name, and a short text preview — copy the chat id into
`messaging.chat_allowlist` in config/assistant.yaml. The bot token is never
printed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_DOTENV = Path(__file__).resolve().parents[2] / ".env"
API_BASE = "https://api.telegram.org"


def main() -> None:
    load_dotenv(REPO_DOTENV)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN is not set. Add it to .env (see .env.example).")

    params = {"timeout": 10, "allowed_updates": json.dumps(["message"])}
    print("Long-polling Telegram for ~10 s (send your bot a message from your phone)...")
    try:
        response = requests.get(
            f"{API_BASE}/bot{token}/getUpdates", params=params, timeout=20
        )
    except requests.RequestException:
        sys.exit("Request to Telegram failed — check network access to api.telegram.org.")
    if response.status_code != 200:
        sys.exit(f"getUpdates returned HTTP {response.status_code} — check the bot token.")

    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("ok"):
        sys.exit("getUpdates returned an unexpected response — check the bot token.")

    observations: dict[int, tuple[str, str]] = {}
    for update in payload.get("result", []):
        if not isinstance(update, dict):
            continue
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("id") is None:
            continue
        name = str(chat.get("first_name", str(chat.get("title", "unknown"))))
        preview = str(message.get("text", "")).strip()[:40]
        observations.setdefault(int(chat["id"]), (name, preview))

    if not observations:
        print("No updates yet. Open Telegram, find your bot, send it any message, "
              "then run this script again.")
        return

    print("\nObserved chats — copy the chat_id you need into config/assistant.yaml:")
    for chat_id, (name, preview) in sorted(observations.items()):
        print(f"  chat_id: {chat_id}  first_name: {name}  text: {preview!r}")


if __name__ == "__main__":
    main()
