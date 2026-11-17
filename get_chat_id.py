#!/usr/bin/env python3
"""Read the private chat ID after /start, without placing the bot token in shell history."""

import getpass
import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    token = getpass.getpass("Paste the BotFather token (input is hidden): ").strip()
    if not token:
        print("No token entered.", file=sys.stderr)
        return 1
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getUpdates",
        headers={"User-Agent": "TelegramChatIdHelper/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        print(f"Could not read Telegram updates: {type(error).__name__}", file=sys.stderr)
        return 1
    if not payload.get("ok"):
        print("Telegram rejected the request. Check the token locally and try again.", file=sys.stderr)
        return 1

    chats = {}
    for update in payload.get("result", []):
        message = update.get("message") or update.get("edited_message") or {}
        if (message.get("text") or "").split()[0:1] != ["/start"]:
            continue
        chat = message.get("chat") or {}
        if chat.get("type") == "private" and chat.get("id") is not None:
            chats[str(chat["id"])] = chat.get("first_name") or "private chat"

    if not chats:
        print("No private /start update found. Open your bot in Telegram, press Start, then run this helper again.")
        return 2
    if len(chats) > 1:
        print("More than one private chat was found. Ask each person not to use /start during setup, then run again.")
        return 3
    chat_id, label = next(iter(chats.items()))
    print(f"Your private chat ID ({label}): {chat_id}")
    print("Add this value to GitHub Actions Secrets as TELEGRAM_CHAT_ID. Do not post it in a public file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
