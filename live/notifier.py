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
    except Exception as e:
        import logging
        logging.warning(f"[notifier] Telegram indisponible : {e}")


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


def notify_z_dispatch(budget: dict, z_capital: float, engine: str,
                      prev_weights: dict = None, target_weights: dict = None,
                      weight_caps_hit: list = None):
    """Notifie le dispatch de budget Bot Z vers les sub-bots."""
    total_dispatched = sum(budget.values())
    lines = [f"💰 <b>Bot Z — Budget dispatché</b>",
             f"Engine: <b>{engine}</b> | Capital: <b>{z_capital:.0f}€</b>",
             ""]
    for bot_id, amount in sorted(budget.items()):
        pct = amount / z_capital * 100 if z_capital > 0 else 0
        filled = min(10, max(0, int(pct / 10)))  # BUG-31 : clamp entre 0 et 10 (évite barre cassée si pct>100%)
        bar = "█" * filled + "░" * (10 - filled)
        cap_marker = " [CAP]" if weight_caps_hit and bot_id in weight_caps_hit else ""
        lines.append(f"Bot {bot_id.upper()}: <b>{amount:.0f}€</b> ({pct:.0f}%) {bar}{cap_marker}")

    # Transition info si les poids bougent encore
    if prev_weights and target_weights:
        transitioning = []
        for b in sorted(prev_weights.keys()):
            prev = prev_weights.get(b, 0) * 100
            tgt  = target_weights.get(b, 0) * 100
            diff = tgt - prev
            if abs(diff) >= 1.5:  # Seuil : 1.5% de mouvement restant
                arrow = "↗" if diff > 0 else "↘"
                transitioning.append(f"{b.upper()}: {prev:.0f}%→{tgt:.0f}% {arrow}")
        if transitioning:
            lines.append(f"\n🔄 <i>Transition en cours: {' | '.join(transitioning)}</i>")

    lines.append(f"\nTotal: <b>{total_dispatched:.0f}€</b> / {z_capital:.0f}€")
    notify("\n".join(lines))


def notify_cycle_summary(engine: str, vix: float, regime: str, z_capital: float,
                         perf_pct: float, budget: dict,
                         obs_bots: dict = None):
    """
    Résumé compact envoyé à chaque cycle Bot Z.
    obs_bots : {h: {trades, positions, blocked}, i: {...}, j: {...}}
    """
    perf_sign = "+" if perf_pct >= 0 else ""
    engine_icons = {"BULL": "🟢", "BALANCED": "🔵", "PARITY": "🟡", "SHIELD": "🔴"}
    icon = engine_icons.get(engine, "⚪")

    lines = [
        f"{icon} <b>Bot Z — Cycle</b> [{engine}]",
        f"VIX: <b>{vix:.1f}</b> | Régime: {regime} | Capital: <b>{z_capital:.0f}€</b> ({perf_sign}{perf_pct:.2f}%)",
    ]

    # Budget dispatch compact
    if budget:
        dispatch_parts = [f"{b.upper()}:{v:.0f}€" for b, v in sorted(budget.items())]
        lines.append("Dispatch: " + " | ".join(dispatch_parts))

    # Observation bots H/I/J
    if obs_bots:
        obs_lines = []
        for bid, info in sorted(obs_bots.items()):
            blocked = info.get("blocked", False)
            trades = info.get("total_trades", 0)
            positions = info.get("open_trades", 0)
            if blocked:
                obs_lines.append(f"  {bid.upper()}: ⛔ bloqué ({engine})")
            else:
                obs_lines.append(f"  {bid.upper()}: {positions} pos | {trades} trades")
        if obs_lines:
            lines.append("\n👁 <i>Observation (H/I/J):</i>")
            lines.extend(obs_lines)

    notify("\n".join(lines))


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
