import os
import requests


def notify(msg: str):
    """
    Envoie une notification Telegram.
    Silencieux si TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID ne sont pas définis.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass
