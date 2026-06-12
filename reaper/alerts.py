"""Telegram alerts — fire-and-forget, never blocks the trading loop.

No-op unless HL_REAPER_TG_TOKEN and HL_REAPER_TG_CHAT are set in .env
(create a bot via @BotFather, message it once, get the chat id from
https://api.telegram.org/bot<TOKEN>/getUpdates)."""
import os
import threading

import requests

from reaper.logger import get_logger

log = get_logger("alerts")

_TOKEN = os.environ.get("HL_REAPER_TG_TOKEN", "")
_CHAT = os.environ.get("HL_REAPER_TG_CHAT", "")
enabled = bool(_TOKEN and _CHAT)


def _post(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
            json={"chat_id": _CHAT, "text": f"🤖 HL Reaper\n{text}"},
            timeout=10,
        )
    except Exception as e:
        log.warning("telegram send failed: %s", e)


def send(text: str):
    """Send asynchronously; logs locally either way."""
    log.info("ALERT | %s", text.replace("\n", " | "))
    if enabled:
        threading.Thread(target=_post, args=(text,), daemon=True).start()
