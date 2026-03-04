import os
import requests


def _credentials():
    return os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")


def notify(msg: str):
    """Envoie une notification Telegram. Silencieux si variables absentes."""
    token, chat_id = _credentials()
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


def notify_file(filepath: str, caption: str = ""):
    """Envoie un fichier en pièce jointe Telegram. Silencieux si variables absentes."""
    token, chat_id = _credentials()
    if not token or not chat_id:
        return
    try:
        with open(filepath, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={"chat_id": chat_id, "caption": caption},
                files={"document": f},
                timeout=10,
            )
    except Exception:
        pass
