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

# ── Identité des bots — source unique pour tous les messages ─────────────────
BOT_INFO = {
    "a": ("Supertrend ATR",     "📈"),
    "b": ("Dual Momentum",      "🔄"),
    "c": ("Donchian Breakout",  "🚀"),
    "g": ("Trend CTA",          "📊"),
    "h": ("VCB Breakout",       "💎"),
    "i": ("RS Leaders",         "🏆"),
    "j": ("Mean Reversion",     "🎯"),
}


def _bot_label(bot_id: str) -> str:
    """Retourne 'Bot C (Donchian Breakout)' à partir de 'c'."""
    bid = bot_id.lower()
    name, _ = BOT_INFO.get(bid, (bid.upper(), ""))
    return f"Bot {bid.upper()} ({name})"


def _format_positions(state: dict, max_show: int = 3) -> str:
    """Formate les positions ouvertes : 'BTC, ETH, SOL +1' ou 'aucune'."""
    positions = state.get("positions", {}) or {}
    if not positions:
        return "aucune"
    syms = [k.split("/")[0].replace("x", "") for k in positions.keys()]
    if len(syms) <= max_show:
        return ", ".join(syms)
    return f"{', '.join(syms[:max_show])} +{len(syms) - max_show}"


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
        f"🚨 <b>API {api.upper()} — Crédits épuisés</b>\n"
        f"<code>{error_msg[:200]}</code>\n\n"
        f"⚠️ Le bot continue, mais les appels {api.upper()} sont désactivés.\n"
        f"🔁 Rappel toutes les 6h jusqu'au rechargement."
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


# ── Bot Z dispatch ───────────────────────────────────────────────────────────

def notify_z_dispatch(budget: dict, z_capital: float, engine: str,
                      prev_weights: dict = None, target_weights: dict = None,
                      weight_caps_hit: list = None, prev_budget: dict = None,
                      perf_pct: float = None):
    """Dispatch Z compact (2 lignes). Suppression: barres ASCII, transition,
    total redondant. Garde: engine, capital, perf, allocation par bot."""
    engine_icons = {"BULL": "🟢", "BALANCED": "🔵", "PARITY": "🟡", "SHIELD": "🔴"}
    icon = engine_icons.get(engine, "⚪")
    perf_str = f" ({'+' if (perf_pct or 0) >= 0 else ''}{perf_pct:.1f}%)" if perf_pct is not None else ""

    alloc_parts = []
    for bot_id in sorted(budget.keys()):
        amount = budget[bot_id]
        pct = amount / z_capital * 100 if z_capital > 0 else 0
        if pct < 0.5:  # skip near-zero allocations (B/C usuellement à 0%)
            continue
        cap = "*" if weight_caps_hit and bot_id in weight_caps_hit else ""
        alloc_parts.append(f"{bot_id.upper()}:{pct:.0f}%{cap}")

    notify(f"{icon} Z {engine} €{z_capital:,.0f}{perf_str} | {' '.join(alloc_parts)}".replace(",", " "))


# ── Cycle summary ────────────────────────────────────────────────────────────

def notify_cycle_summary(engine: str, vix: float, regime: str, z_capital: float,
                         perf_pct: float, budget: dict,
                         obs_bots: dict = None, main_bots: dict = None):
    """Cycle summary Z compact (2-3 lignes max).

    Format:
        🔵 Z BALANCED €101.5K (+1.5%) | VIX 17.9 🐂
        A:2pos G:1pos B:gelé | H:5tr I:0tr J:2tr
    """
    perf_sign = "+" if (perf_pct or 0) >= 0 else ""
    engine_icons = {"BULL": "🟢", "BALANCED": "🔵", "PARITY": "🟡", "SHIELD": "🔴"}
    icon = engine_icons.get(engine, "⚪")
    regime_icon = {"BULL": "🐂", "BEAR": "🐻", "NEUTRAL": "➡️"}.get((regime or "").upper(), "")

    line1 = f"{icon} Z {engine} €{z_capital:,.0f} ({perf_sign}{perf_pct:.1f}%) | VIX {vix:.1f} {regime_icon}".replace(",", " ")

    # Compact contest line: only bots that have positions or are frozen
    parts = []
    if main_bots:
        for bid in sorted(main_bots.keys()):
            info = main_bots[bid]
            pos = info.get("positions", 0)
            if info.get("dd_frozen"):
                parts.append(f"{bid.upper()}:🧊")
            elif pos > 0:
                parts.append(f"{bid.upper()}:{pos}pos")

    if obs_bots:
        for bid in sorted(obs_bots.keys()):
            info = obs_bots[bid]
            if info.get("blocked"):
                continue  # silence: bots bloqués pas intéressants chaque cycle
            tr = info.get("total_trades", 0)
            po = info.get("open_trades", 0)
            if po > 0 or tr > 0:
                parts.append(f"{bid.upper()}:{po}p/{tr}tr")

    msg = line1
    if parts:
        msg += "\n" + " ".join(parts)
    notify(msg)


# ── Évènements de bot ────────────────────────────────────────────────────────

def notify_data_stale(symbol: str, timeframe: str, age_hours: float):
    """Alerte quand les données OHLCV sont périmées."""
    notify(
        f"⚠️ <b>Données périmées</b> — {symbol} ({timeframe})\n"
        f"Dernière bougie : il y a <b>{age_hours:.1f}h</b>\n"
        f"Le bot continue avec des données potentiellement obsolètes."
    )


def notify_winrate_drop(bot_id: str, winrate: float, last_n: int):
    """Alerte quand le winrate chute sous un seuil critique."""
    notify(
        f"📉 <b>Win rate critique — {_bot_label(bot_id)}</b>\n"
        f"<b>{winrate:.0f}%</b> sur les {last_n} derniers trades\n"
        f"⚠️ Vérifier la stratégie ou les conditions de marché."
    )


def notify_exposure_high(total_pct: float, details: str):
    """Alerte quand l'exposition totale du portfolio dépasse le seuil."""
    notify(
        f"🔥 <b>Exposition élevée</b> — {total_pct:.0f}% du capital\n"
        f"{details}\n"
        f"Les nouvelles entrées sont suspendues."
    )


def notify_bot_death(bot_id: str, capital: float, will_inject: float = None):
    """Alerte quand un bot perd tout son capital."""
    inject_str = (
        f"\n💵 Sera re-capitalisé à <b>{will_inject:.0f}€</b> au prochain dispatch"
        if will_inject else
        "\nSera re-capitalisé au prochain dispatch Bot Z."
    )
    notify(
        f"💀 <b>{_bot_label(bot_id)} — Capital épuisé</b>\n"
        f"Capital restant : <b>{capital:.2f}€</b>{inject_str}"
    )


def notify_bot_revived(bot_id: str, new_capital: float):
    """Alerte quand un bot mort reçoit du capital frais."""
    notify(
        f"🔄 <b>{_bot_label(bot_id)} — Re-capitalisé</b>\n"
        f"Nouveau capital : <b>{new_capital:.0f}€</b>\n"
        f"Trades reprennent au prochain signal."
    )


def notify_bot_frozen(bot_id: str, dd: float, threshold: float, state: dict,
                      vix: float = None, regime: str = None, engine: str = None,
                      unfreeze_threshold: float = -0.08):
    """Alerte enrichie quand un bot atteint son drawdown max."""
    capital = state.get("capital", 0)
    positions = state.get("positions", {}) or {}
    pos_value = sum(p.get("entry", 0) * p.get("size", 0) for p in positions.values())
    total = capital + pos_value
    initial = state.get("original_capital", state.get("initial_capital", 1000))
    loss_eur = total - initial
    pos_str = _format_positions(state)

    lines = [
        f"⛔ <b>{_bot_label(bot_id)} — GELÉ</b>",
        f"Drawdown: <b>{dd*100:.1f}%</b> (seuil: {threshold*100:.0f}%)",
        f"Capital: <b>{total:.0f}€</b> ({loss_eur:+.0f}€ vs {initial:.0f}€)",
        f"Positions conservées: <b>{len(positions)}</b> ({pos_str})",
        f"Dégel auto à DD &gt; {unfreeze_threshold*100:.0f}%",
    ]

    if vix is not None or regime or engine:
        ctx_parts = []
        if vix is not None:
            ctx_parts.append(f"VIX {vix:.1f}")
        if regime:
            ctx_parts.append(f"Régime {regime}")
        if engine:
            ctx_parts.append(f"Engine {engine}")
        lines.append(f"Contexte: {' | '.join(ctx_parts)}")

    lines.append("ℹ️ Les autres bots continuent de trader.")
    notify("\n".join(lines))


def notify_bot_unfrozen(bot_id: str, dd: float, unfreeze_threshold: float, state: dict):
    """Alerte enrichie quand un bot est dégelé après reprise."""
    capital = state.get("capital", 0)
    positions = state.get("positions", {}) or {}
    pos_value = sum(p.get("entry", 0) * p.get("size", 0) for p in positions.values())
    total = capital + pos_value

    lines = [
        f"🔥 <b>{_bot_label(bot_id)} — DÉGELÉ</b>",
        f"Drawdown: <b>{dd*100:.1f}%</b> (seuil dégel: {unfreeze_threshold*100:.0f}%)",
        f"Capital: <b>{total:.0f}€</b> | Positions: {len(positions)}",
        f"✅ Reprise des trades au prochain signal.",
    ]
    notify("\n".join(lines))


def notify_token_warning(hours_remaining: float, refresh_ok: bool):
    """Alerte quand le token OAuth approche de l'expiration."""
    if refresh_ok:
        notify(
            f"🔑 <b>Token Claude — Rafraîchi</b>\n"
            f"(restait {hours_remaining:.1f}h avant expiration)"
        )
    else:
        notify(
            f"🚨 <b>Token Claude — Refresh échoué</b>\n"
            f"Expire dans <b>{hours_remaining:.1f}h</b>\n"
            f"⚠️ Le filtre Claude sera indisponible après expiration.\n"
            f"Action: <code>ssh ubuntu@VPS 'claude auth login'</code>"
        )


# ── Rapport quotidien ────────────────────────────────────────────────────────

def notify_daily_health(bots_status: list[dict], z_capital: float, engine: str, days_running: int):
    """Rapport quotidien envoyé 1x/jour à 19h UTC.
    bots_status: [{id, name, capital, positions, trades, dd_frozen, pnl_pct}]
    """
    engine_icons = {"BULL": "🟢", "BALANCED": "🔵", "PARITY": "🟡", "SHIELD": "🔴"}
    icon = engine_icons.get(engine, "⚪")

    # Stats agrégées
    total_positions = sum(b.get("positions", 0) for b in bots_status)
    total_trades = sum(b.get("trades", 0) for b in bots_status)
    n_frozen = sum(1 for b in bots_status if b.get("dd_frozen"))
    n_dead = sum(1 for b in bots_status if b.get("capital", 0) < 5 and b.get("positions", 0) == 0)
    n_active = len(bots_status) - n_frozen - n_dead

    # Top / bottom performers
    perfs = [(b["id"].upper(), b.get("pnl_pct", 0)) for b in bots_status]
    perfs.sort(key=lambda x: x[1], reverse=True)
    best = perfs[0] if perfs else None
    worst = perfs[-1] if perfs else None

    lines = [
        f"📊 <b>Rapport quotidien — Jour {days_running}</b>",
        f"{icon} Bot Z: <b>{z_capital:,.0f}€</b> | Engine: <b>{engine}</b>".replace(",", " "),
        f"État: {n_active} actifs | {n_frozen} gelés | {n_dead} morts",
        f"Positions ouvertes: <b>{total_positions}</b> | Trades cumulés: {total_trades}",
        "",
    ]

    warnings = []
    for b in bots_status:
        bid = b["id"].lower()
        name, emoji = BOT_INFO.get(bid, (b.get("name", bid.upper()), "•"))
        n_pos = b.get("positions", 0)
        n_trades = b.get("trades", 0)
        pnl = b.get("pnl_pct", 0)
        capital = b.get("capital", 0)

        # Status icon
        if capital < 5 and n_pos == 0:
            status = "💀"
            warnings.append(f"💀 {_bot_label(bid)} — capital épuisé ({capital:.0f}€)")
        elif b.get("dd_frozen"):
            status = "🧊"
            warnings.append(f"🧊 {_bot_label(bid)} — gelé (DD {pnl:+.1f}%)")
        elif n_trades == 0 and n_pos == 0:
            status = "😴"
            warnings.append(f"😴 {_bot_label(bid)} — 0 trades depuis le début")
        elif pnl >= 5:
            status = "🚀"
        elif pnl >= 0:
            status = "✅"
        elif pnl >= -5:
            status = "🟡"
        else:
            status = "🔻"

        sign = "+" if pnl >= 0 else ""
        lines.append(
            f"{status} {emoji} {name} (Bot {bid.upper()}): "
            f"<b>{capital:.0f}€</b> ({sign}{pnl:.1f}%) | {n_pos} pos | {n_trades} tr"
        )

    if best and worst and best != worst:
        lines.append("")
        lines.append(f"🥇 Best: Bot {best[0]} ({best[1]:+.1f}%)  —  🔻 Worst: Bot {worst[0]} ({worst[1]:+.1f}%)")

    if warnings:
        lines.append("")
        lines.append("<b>⚠️ Alertes:</b>")
        lines.extend(warnings)

    notify("\n".join(lines))


def resend_pending_alerts():
    """
    Renvoie toutes les alertes actives (appelé au début de chaque cycle).
    Garantit que l'alerte ne passe pas inaperçue.
    Limite : 1 rappel toutes les 6h par alerte pour éviter le spam.
    """
    alerts = _load_alerts()
    if not alerts:
        return

    now = datetime.now()
    updated = False
    for api, data in alerts.items():
        # Anti-spam : 1 rappel toutes les 6h max
        last_resend = data.get("last_resend", "")
        if last_resend:
            try:
                last_dt = datetime.fromisoformat(last_resend)
                if (now - last_dt).total_seconds() < 6 * 3600:
                    continue
            except ValueError:
                pass

        ts = data.get("ts", "")[:16].replace("T", " ")
        notify(
            f"🔁 <b>RAPPEL — {api.upper()} indisponible</b>\n"
            f"Signalé depuis : {ts}\n"
            f"<code>{data.get('message', '')[:200]}</code>"
        )
        data["last_resend"] = now.isoformat()
        updated = True

    if updated:
        _save_alerts(alerts)
