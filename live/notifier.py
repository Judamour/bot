import json
import os
import requests
from datetime import datetime

ALERTS_FILE = "logs/api_alerts.json"

# Mots-clés indiquant une erreur de crédit/quota (Anthropic, DeepSeek...)
_CREDIT_KEYWORDS = [
    "credit", "billing", "insufficient", "balance",
    "quota", "402", "payment", "funds", "overdue",
]


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


# ── Alertes persistantes (crédit API épuisé) ─────────────────────────────────

def _load_alerts() -> dict:
    if os.path.exists(ALERTS_FILE):
        try:
            with open(ALERTS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_alerts(alerts: dict):
    os.makedirs("logs", exist_ok=True)
    with open(ALERTS_FILE, "w") as f:
        json.dump(alerts, f, indent=2)


def is_credit_error(e: Exception) -> bool:
    """Retourne True si l'exception indique un problème de crédit/quota API."""
    msg = str(e).lower()
    return any(k in msg for k in _CREDIT_KEYWORDS)


def set_api_alert(api: str, error_msg: str):
    """
    Enregistre une alerte de crédit épuisé pour l'API donnée.
    Envoie immédiatement une notification Telegram.
    Idempotent : n'envoie qu'une seule fois par api jusqu'à résolution.
    """
    alerts = _load_alerts()
    if api in alerts:
        return  # Alerte déjà active — pas de spam

    alerts[api] = {
        "message": error_msg,
        "ts": datetime.now().isoformat(),
    }
    _save_alerts(alerts)

    notify(
        f"🚨 <b>ALERTE — Crédits {api.upper()} épuisés</b>\n\n"
        f"<code>{error_msg[:200]}</code>\n\n"
        f"⚠️ Le bot continue mais les appels {api.upper()} sont désactivés.\n"
        f"🔄 <b>Ce message se répètera à chaque cycle jusqu'au rechargement.</b>"
    )


def clear_api_alert(api: str):
    """
    Supprime l'alerte quand l'API fonctionne à nouveau.
    Envoie une notification de confirmation.
    """
    alerts = _load_alerts()
    if api not in alerts:
        return  # Pas d'alerte active — rien à faire

    alerts.pop(api)
    _save_alerts(alerts)

    notify(
        f"✅ <b>API {api.upper()} — Crédits rechargés</b>\n"
        f"Bot pleinement opérationnel."
    )


def resend_pending_alerts():
    """
    Renvoie toutes les alertes actives (appelé au début de chaque cycle).
    Garantit que l'alerte ne passe pas inaperçue.
    """
    alerts = _load_alerts()
    if not alerts:
        return

    for api, data in alerts.items():
        ts = data.get("ts", "")[:16].replace("T", " ")
        notify(
            f"🔁 <b>RAPPEL — Crédits {api.upper()} épuisés</b>\n\n"
            f"Signalé depuis : {ts}\n"
            f"<code>{data.get('message', '')[:200]}</code>\n\n"
            f"Rechargez votre compte {api.upper()} pour arrêter ces alertes."
        )
