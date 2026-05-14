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

# Abréviation courte de stratégie utilisée dans le préfixe `[BotA·ST]`.
# Max 4 chars pour rester compact sur mobile (38 chars/ligne iPhone).
BOT_STRATEGY_ABBR = {
    "a": "ST",   # Supertrend
    "b": "DM",   # Dual Momentum
    "c": "DB",   # Donchian Breakout
    "g": "CTA",  # Trend CTA
    "h": "VCB",  # VCB Breakout
    "i": "RS",   # RS Leaders
    "j": "MR",   # Mean Reversion
    "z": "Z",    # Bot Z meta
}

# ── Convention émojis Freqtrade-style ────────────────────────────────────────
# Hiérarchie de sévérité standardisée. NE PAS dévier — l'utilisateur pattern-
# match l'émoji avant le texte. Inconsistance détruit l'avantage scan visuel.
ICON_BUY_FILL       = "✅"   # Ordre BUY fillé
ICON_BUY_PENDING    = "🔵"   # Ordre BUY placé mais pas encore fillé
ICON_EXIT_BIG_WIN   = "🚀"   # Exit ≥ +5%
ICON_EXIT_WIN       = "✳️"   # Exit 0 à +5%
ICON_STOP_LOSS      = "⚠️"   # Stop loss déclenché (comportement attendu)
ICON_EXIT_LOSS      = "❌"   # Exit en perte hors stop / échec ordre
ICON_CRITICAL       = "🔥"   # Kill switch / broker down / catastrophique
ICON_INFO           = "ℹ️"   # Info non-trade (engine switch, etc.)
ICON_CYCLE          = "📊"   # Résumé de cycle 4h
ICON_DAILY          = "📅"   # Rapport quotidien
ICON_SHADOW         = "🔬"   # Préfixe Shadow bot (banc de test)


def _attribution(bot_id: str, strategy: str = None) -> str:
    """Préfixe d'attribution compact: `[BotA·ST]` ou `[SHADOW]`.

    Args:
        bot_id: 'a'..'j', 'z' pour Bot Z, 'shadow' pour Shadow bot.
        strategy: override d'abréviation (sinon dérive de BOT_STRATEGY_ABBR).
    """
    bid = (bot_id or "").lower()
    if bid in ("shadow", "shadow_bot"):
        return "[SHADOW]"
    if bid == "z":
        return "[Z]"
    abbr = strategy or BOT_STRATEGY_ABBR.get(bid, "?")
    return f"[Bot{bid.upper()}·{abbr}]"


def _fmt_money(amount: float, ccy: str = "USD", sign: bool = False) -> str:
    """Format monétaire avec séparateur d'espace fine: `$67,420.00` ou `+$143.20`.

    Force USD par défaut (Alpaca paper unifié). Le signe est inclus si sign=True
    OU si amount < 0 (toujours montrer le moins).
    """
    symbol = {"USD": "$", "EUR": "€"}.get((ccy or "USD").upper(), "")
    sign_str = ""
    if sign and amount >= 0:
        sign_str = "+"
    elif amount < 0:
        sign_str = "−"  # vrai minus, pas hyphen — meilleure lisibilité
        amount = abs(amount)
    formatted = f"{amount:,.2f}".replace(",", " ")  # espace fine séparateur
    return f"{sign_str}{symbol}{formatted}"


def _fmt_pct(pct: float, sign: bool = True, decimals: int = 1) -> str:
    """Format pourcentage avec signe: `+14.5%` ou `−4.8%`.

    Utilise vrai minus (−) au lieu d'hyphen (-) pour lisibilité.
    """
    if pct < 0:
        return f"−{abs(pct):.{decimals}f}%"
    if sign:
        return f"+{pct:.{decimals}f}%"
    return f"{pct:.{decimals}f}%"


def _fmt_duration(seconds_or_delta) -> str:
    """Durée compacte: `2d 4h`, `18h`, `45m`, `30s`.

    Accepte int/float (secondes) ou timedelta.
    """
    from datetime import timedelta
    if isinstance(seconds_or_delta, timedelta):
        secs = int(seconds_or_delta.total_seconds())
    else:
        secs = int(seconds_or_delta or 0)
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        rem_min = mins % 60
        return f"{hours}h {rem_min}m" if rem_min else f"{hours}h"
    days = hours // 24
    rem_h = hours % 24
    return f"{days}d {rem_h}h" if rem_h else f"{days}d"


def _escape_html(text: str) -> str:
    """Échappe les entités HTML qui cassent silencieusement les messages Telegram."""
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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


# ── Cycle batching ───────────────────────────────────────────────────────────
# Accumule les events trade d'un cycle dans un buffer global, flush en 1 message
# à la fin du cycle. Réduit la pression de notifs de 5× sur les cycles chargés.
# Toggle via env: NOTIF_BATCH_CYCLE=true|false (défaut true).

_CYCLE_BUFFER = {
    "executed": [],   # [{action, symbol, price, pct_capital, pnl_pct, reason, bot_id}]
    "skipped": [],    # [{symbol, reason}]
}


def _batching_enabled() -> bool:
    return os.getenv("NOTIF_BATCH_CYCLE", "true").lower() in ("true", "1", "yes")


def buffer_buy(bot_id: str, symbol: str, price: float, size_units: float,
               size_usd: float, stop: float = None, risk_usd: float = None,
               capital_total: float = None, strategy: str = None,
               queued: bool = False, ccy: str = "USD"):
    """Bufferise un BUY dans le cycle courant (ou l'envoie direct si batching off).

    Si NOTIF_BATCH_CYCLE=false, équivalent à notify_buy() immédiat.
    Sinon, accumule dans _CYCLE_BUFFER, à flusher avec flush_cycle().
    """
    if not _batching_enabled():
        notify_buy(bot_id, symbol, price, size_units, size_usd, stop,
                   risk_usd, strategy, queued, ccy)
        return
    pct_cap = (size_usd / capital_total * 100) if (capital_total and capital_total > 0) else 0
    _CYCLE_BUFFER["executed"].append({
        "action": "BUY",
        "bot_id": bot_id,
        "strategy": strategy,
        "symbol": symbol,
        "price": price,
        "pct_capital": pct_cap,
        "size_units": size_units,
        "size_usd": size_usd,
        "stop": stop,
        "risk_usd": risk_usd,
        "queued": queued,
        "ccy": ccy,
    })


def buffer_sell(bot_id: str, symbol: str, entry_price: float, exit_price: float,
                pnl_usd: float, pnl_pct: float, reason: str,
                duration_sec: float = None, strategy: str = None,
                ccy: str = "USD"):
    """Bufferise un SELL/EXIT dans le cycle courant (ou l'envoie direct si batching off)."""
    if not _batching_enabled():
        notify_sell(bot_id, symbol, entry_price, exit_price, pnl_usd, pnl_pct,
                    reason, duration_sec, strategy, ccy)
        return
    _CYCLE_BUFFER["executed"].append({
        "action": "SELL",
        "bot_id": bot_id,
        "strategy": strategy,
        "symbol": symbol,
        "price": exit_price,
        "entry_price": entry_price,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "reason": reason,
        "duration_sec": duration_sec,
        "ccy": ccy,
    })


def buffer_skip(symbol: str, reason: str):
    """Bufferise un skip (signal rejeté) pour information dans le résumé cycle."""
    if not _batching_enabled():
        return  # En mode immédiat, on ne notifie pas les skips (trop de bruit)
    _CYCLE_BUFFER["skipped"].append({
        "symbol": symbol,
        "reason": reason,
    })


def flush_cycle(hour_utc: int, engine: str,
                capital_deployed: float = None, capital_total: float = None,
                vix: float = None, regime: str = None, ccy: str = "USD"):
    """Flushe le buffer cycle vers 1 message batch. Vide le buffer après envoi.

    Si batching off, vide juste le buffer (déjà notifié individuellement).
    Si buffer vide, ne notifie rien (silence = santé).
    """
    executed = _CYCLE_BUFFER["executed"]
    skipped = _CYCLE_BUFFER["skipped"]

    if _batching_enabled() and (executed or skipped):
        notify_cycle_batch(
            hour_utc=hour_utc, engine=engine,
            executed=executed, skipped=skipped,
            capital_deployed=capital_deployed, capital_total=capital_total,
            vix=vix, regime=regime, ccy=ccy,
        )

    # Reset
    _CYCLE_BUFFER["executed"] = []
    _CYCLE_BUFFER["skipped"] = []


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


# ── Trades : BUY / SELL structurés (Freqtrade-style) ─────────────────────────

def notify_buy(bot_id: str, symbol: str, price: float, size_units: float,
               size_usd: float, stop: float = None, risk_usd: float = None,
               strategy: str = None, queued: bool = False, ccy: str = "USD"):
    """Notification BUY fillée — format multi-ligne avec attribution.

    Format:
        ✅ [BotA·ST]  BUY  BTC/USD

        Price:    $67,420.00
        Size:     0.0148 BTC  ($998)
        Stop:     $64,200  (−4.8%)
        Risk:     $46  (1R)

    Args:
        bot_id:      'a'..'j', 'z', ou 'shadow'
        symbol:      'BTC/USD', 'NVDA', etc.
        price:       prix d'exécution
        size_units:  taille en unités de l'actif (0.0148 BTC, 100 NVDA)
        size_usd:    notional en USD
        stop:        prix du stop loss (optionnel)
        risk_usd:    montant à risque jusqu'au stop (optionnel, affiche '(1R)')
        strategy:    override abréviation stratégie (sinon dérive de bot_id)
        queued:      True si l'ordre est en queue (hors marché)
        ccy:         devise (défaut USD)
    """
    attr = _attribution(bot_id, strategy)
    icon = ICON_BUY_PENDING if queued else ICON_BUY_FILL
    sym_clean = _escape_html(symbol)
    base_unit = symbol.split("/")[0] if "/" in symbol else "sh"  # 'shares' pour stocks

    lines = [
        f"{icon} {attr}  <b>BUY</b>  <code>{sym_clean}</code>",
        "",
        f"Price:    <code>{_fmt_money(price, ccy)}</code>",
        f"Size:     <code>{size_units:.4f} {base_unit}</code>  ({_fmt_money(size_usd, ccy)})",
    ]
    if stop is not None and stop > 0:
        stop_pct = (stop - price) / price * 100 if price else 0
        lines.append(f"Stop:     <code>{_fmt_money(stop, ccy)}</code>  ({_fmt_pct(stop_pct)})")
    if risk_usd is not None:
        lines.append(f"Risk:     <code>{_fmt_money(risk_usd, ccy)}</code>  (1R)")
    if queued:
        lines.append("")
        lines.append("<i>Queued — fill prochaine session</i>")
    notify("\n".join(lines))


def notify_sell(bot_id: str, symbol: str, entry_price: float, exit_price: float,
                pnl_usd: float, pnl_pct: float, reason: str,
                duration_sec: float = None, strategy: str = None,
                ccy: str = "USD"):
    """Notification SELL/EXIT — format multi-ligne, P&L en première position.

    Format:
        🚀 [BotA·ST]  SELL  BTC/USD

        P&L:      +$143.20  (+14.5%)
        Duration: 2d 4h
        Exit:     trailing_stop
        Entry→Exit: $67,420 → $77,060

    L'icône reflète l'outcome:
        🚀 exit ≥ +5%   |   ✳️ exit 0 à +5%
        ⚠️ stop_loss     |   ❌ autre exit en perte
    """
    attr = _attribution(bot_id, strategy)
    sym_clean = _escape_html(symbol)

    # Choix icône selon résultat — convention Freqtrade
    if reason in ("stop_loss", "broker_stop_fill"):
        icon = ICON_STOP_LOSS
    elif pnl_pct >= 5.0:
        icon = ICON_EXIT_BIG_WIN
    elif pnl_usd >= 0:
        icon = ICON_EXIT_WIN
    else:
        icon = ICON_EXIT_LOSS

    lines = [
        f"{icon} {attr}  <b>SELL</b>  <code>{sym_clean}</code>",
        "",
        f"P&amp;L:      <code>{_fmt_money(pnl_usd, ccy, sign=True)}</code>  ({_fmt_pct(pnl_pct)})",
    ]
    if duration_sec is not None and duration_sec > 0:
        lines.append(f"Duration: <code>{_fmt_duration(duration_sec)}</code>")
    lines.append(f"Exit:     <code>{_escape_html(reason)}</code>")
    lines.append(
        f"Entry→Exit: <code>{_fmt_money(entry_price, ccy)}</code> → "
        f"<code>{_fmt_money(exit_price, ccy)}</code>"
    )
    notify("\n".join(lines))


def notify_cycle_batch(hour_utc: int, engine: str,
                       executed: list, skipped: list = None,
                       capital_deployed: float = None,
                       capital_total: float = None,
                       vix: float = None, regime: str = None,
                       ccy: str = "USD"):
    """Résumé d'un cycle 4h: 1 message au lieu de N notifs individuelles.

    Args:
        hour_utc:        heure UTC du cycle (3, 7, 11, 15, 19, 23)
        engine:          BULL / BALANCED / PARITY / SHIELD
        executed:        list of dict {action: 'BUY'|'SELL', symbol, price, pct_capital, pnl_pct (sell only)}
        skipped:         list of dict {symbol, reason} (signaux ignorés)
        capital_deployed: USD effectivement engagé en positions
        capital_total:    USD capital total
        vix:             VIX au moment du cycle
        regime:          BULL / BEAR / NEUTRAL

    Format:
        📊 Cycle 11:00 UTC  |  Z → BALANCED

        Executed:
          ✅ BUY   NVDA     $876.20  (2.5%)
          🚀 SELL  BTC/USD  $77,060  (+14.5%)
          — SKIP  META     score 48 < 55

        Capital: $24,800 / $25,000  (99%)
        Regime: BALANCED  |  VIX: 18.4
    """
    engine_icon = {"BULL": "🟢", "BALANCED": "🔵", "PARITY": "🟡", "SHIELD": "🔴"}.get(engine, "⚪")
    lines = [f"{ICON_CYCLE} Cycle <b>{hour_utc:02d}:00 UTC</b>  |  Z → {engine_icon} <b>{engine}</b>"]

    if executed:
        lines.append("")
        lines.append("<b>Executed:</b>")
        for ev in executed:
            action = ev.get("action", "?").upper()
            sym = _escape_html(ev.get("symbol", "?"))
            price = ev.get("price", 0)
            if action == "BUY":
                pct_cap = ev.get("pct_capital", 0)
                lines.append(
                    f"  {ICON_BUY_FILL} BUY   <code>{sym:<8}</code> "
                    f"<code>{_fmt_money(price, ccy)}</code>  ({pct_cap:.1f}%)"
                )
            elif action == "SELL":
                pnl_pct = ev.get("pnl_pct", 0)
                reason = ev.get("reason", "")
                # Icône selon outcome
                if reason in ("stop_loss", "broker_stop_fill"):
                    sicon = ICON_STOP_LOSS
                elif pnl_pct >= 5.0:
                    sicon = ICON_EXIT_BIG_WIN
                elif pnl_pct >= 0:
                    sicon = ICON_EXIT_WIN
                else:
                    sicon = ICON_EXIT_LOSS
                lines.append(
                    f"  {sicon} SELL  <code>{sym:<8}</code> "
                    f"<code>{_fmt_money(price, ccy)}</code>  ({_fmt_pct(pnl_pct)})"
                )

    if skipped:
        if not executed:
            lines.append("")
        for sk in skipped[:5]:  # cap à 5 pour éviter le wall of text
            sym = _escape_html(sk.get("symbol", "?"))
            reason = _escape_html(sk.get("reason", ""))
            lines.append(f"  — SKIP  <code>{sym:<8}</code> {reason}")
        if len(skipped) > 5:
            lines.append(f"  …  +{len(skipped) - 5} skips")

    # Capital + régime
    if capital_deployed is not None and capital_total is not None and capital_total > 0:
        deploy_pct = capital_deployed / capital_total * 100
        lines.append("")
        lines.append(
            f"Capital: <code>{_fmt_money(capital_deployed, ccy)}</code> / "
            f"<code>{_fmt_money(capital_total, ccy)}</code>  ({deploy_pct:.0f}%)"
        )

    ctx_parts = []
    if regime:
        regime_icon = {"BULL": "🐂", "BEAR": "🐻", "NEUTRAL": "➡️"}.get(regime.upper(), "")
        ctx_parts.append(f"Regime: {regime_icon} <b>{regime}</b>")
    if vix is not None:
        ctx_parts.append(f"VIX: <code>{vix:.1f}</code>")
    if ctx_parts:
        if capital_deployed is None:
            lines.append("")
        lines.append("  |  ".join(ctx_parts))

    # Si rien à reporter (cycle vide sans exec/skip), ne pas envoyer
    if not executed and not skipped:
        return

    notify("\n".join(lines))


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


# ── Shadow bot — notifications dédiées ───────────────────────────────────────

def notify_shadow_buy(symbol: str, strategy_name: str, price: float,
                       size_units: float, size_usd: float, stop: float = None,
                       score: float = None, rationale: str = None,
                       queued: bool = False, ccy: str = "USD"):
    """Shadow bot BUY — même structure que notify_buy, préfixe [SHADOW]."""
    attr = "[SHADOW]"
    abbr = strategy_name[:4].upper() if strategy_name else None
    if abbr:
        attr = f"[SHADOW·{abbr}]"
    icon = ICON_BUY_PENDING if queued else ICON_BUY_FILL
    sym_clean = _escape_html(symbol)
    base_unit = symbol.split("/")[0] if "/" in symbol else "sh"

    lines = [
        f"{icon} {attr}  <b>BUY</b>  <code>{sym_clean}</code>",
        "",
        f"Price:    <code>{_fmt_money(price, ccy)}</code>",
        f"Size:     <code>{size_units:.4f} {base_unit}</code>  ({_fmt_money(size_usd, ccy)})",
    ]
    if stop is not None and stop > 0:
        stop_pct = (stop - price) / price * 100 if price else 0
        lines.append(f"Stop:     <code>{_fmt_money(stop, ccy)}</code>  ({_fmt_pct(stop_pct)})")
    if score is not None:
        lines.append(f"Score:    <code>{score:.0f}/100</code>")
    if rationale:
        lines.append(f"<i>{_escape_html(rationale[:120])}</i>")
    if queued:
        lines.append("")
        lines.append("<i>Queued — fill prochaine session</i>")
    notify("\n".join(lines))


def notify_shadow_sell(symbol: str, entry_price: float, exit_price: float,
                       pnl_usd: float, pnl_pct: float, reason: str,
                       duration_sec: float = None, strategy_name: str = None,
                       ccy: str = "USD"):
    """Shadow bot SELL — même structure que notify_sell, préfixe [SHADOW]."""
    attr = "[SHADOW]"
    abbr = strategy_name[:4].upper() if strategy_name else None
    if abbr:
        attr = f"[SHADOW·{abbr}]"
    sym_clean = _escape_html(symbol)

    if reason in ("stop_loss", "broker_stop_fill"):
        icon = ICON_STOP_LOSS
    elif pnl_pct >= 5.0:
        icon = ICON_EXIT_BIG_WIN
    elif pnl_usd >= 0:
        icon = ICON_EXIT_WIN
    else:
        icon = ICON_EXIT_LOSS

    lines = [
        f"{icon} {attr}  <b>SELL</b>  <code>{sym_clean}</code>",
        "",
        f"P&amp;L:      <code>{_fmt_money(pnl_usd, ccy, sign=True)}</code>  ({_fmt_pct(pnl_pct)})",
    ]
    if duration_sec is not None and duration_sec > 0:
        lines.append(f"Duration: <code>{_fmt_duration(duration_sec)}</code>")
    lines.append(f"Exit:     <code>{_escape_html(reason)}</code>")
    lines.append(
        f"Entry→Exit: <code>{_fmt_money(entry_price, ccy)}</code> → "
        f"<code>{_fmt_money(exit_price, ccy)}</code>"
    )
    notify("\n".join(lines))


# ── Rapport quotidien ────────────────────────────────────────────────────────

def _ascii_bar(pct: float, width: int = 10) -> str:
    """Barre ASCII pour visualisation mobile-safe: `███████░░░ 78%`.

    Args:
        pct: 0-100. Tronqué à [0, 100].
        width: nombre de cellules (défaut 10).
    """
    pct = max(0, min(100, pct))
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def notify_daily_report(
    date_str: str,
    pnl_today_usd: float, pnl_today_pct: float, pnl_today_trades: int,
    pnl_open_usd: float, pnl_open_pct: float, open_positions: int,
    attribution: list,
    win_rate_today: tuple = None,  # (wins, losses)
    win_rate_7d_pct: float = None,
    peak_dd_pct: float = None, dd_limit_pct: float = -25.0,
    capital_at_risk_usd: float = None, capital_total_usd: float = None,
    regime_today: str = None, regime_yesterday: str = None,
    pnl_yesterday_usd: float = None,
    ccy: str = "USD",
):
    """Rapport quotidien enrichi (Freqtrade-style + attribution bars).

    Args:
        date_str:        '2026-05-14'
        pnl_today_usd:   P&L USD réalisé aujourd'hui (trades clos)
        pnl_today_pct:   P&L % réalisé aujourd'hui
        pnl_today_trades: nombre de trades clos aujourd'hui
        pnl_open_usd:    P&L USD non réalisé (positions ouvertes)
        pnl_open_pct:    P&L % non réalisé
        open_positions:  nombre de positions ouvertes
        attribution:     list of dict {id, label, pnl_usd, pnl_pct_of_total}
                         exemple: [{'id': 'a', 'label': 'BotA (ST)', 'pnl_usd': 318.4, 'pnl_pct_of_total': 78}, ...]
        win_rate_today:  (n_wins, n_losses) pour aujourd'hui
        win_rate_7d_pct: win rate moyen sur 7 jours
        peak_dd_pct:     drawdown peak depuis init (négatif)
        dd_limit_pct:    seuil kill switch (défaut -25%)
        capital_at_risk_usd: somme MTM positions ouvertes
        capital_total_usd:   capital Z total
        regime_today:    engine au moment du report
        regime_yesterday: pour delta
        pnl_yesterday_usd: pour delta visuel

    Format mockup E (~600 chars):
        📅 Daily Report — 2026-05-14

        P&L Today
          Closed:  +$318.40  (+3.2%)  3 trades
          Open:    +$87.20   (+0.9%)  2 positions
          Net:     +$405.60  (+4.1%)
          vs hier: +$87.40 (+27%)

        Attribution
          BotA (ST):   +$318.40  ███████░░░ 78%
          BotG (CTA):    +$0.00  ░░░░░░░░░░  0%
          Shadow:       +$87.20  ██░░░░░░░░ 22%

        Win Rate  3W / 0L (100%)  — 7d avg: 62%

        Risk
          Peak DD:    −3.1%  🟢 vs −25% limit
          At risk:    $998 / $25K  (4%)

        Regime  BALANCED → BALANCED (stable)
    """
    lines = [f"{ICON_DAILY} <b>Daily Report — {_escape_html(date_str)}</b>", ""]

    # ── Bloc P&L ──
    pnl_net_usd = (pnl_today_usd or 0) + (pnl_open_usd or 0)
    pnl_net_pct = (pnl_today_pct or 0) + (pnl_open_pct or 0)

    lines.append("<b>P&amp;L Today</b>")
    lines.append(
        f"  Closed:  <code>{_fmt_money(pnl_today_usd, ccy, sign=True)}</code>  "
        f"({_fmt_pct(pnl_today_pct)})  {pnl_today_trades} trades"
    )
    lines.append(
        f"  Open:    <code>{_fmt_money(pnl_open_usd, ccy, sign=True)}</code>  "
        f"({_fmt_pct(pnl_open_pct)})  {open_positions} positions"
    )
    lines.append(
        f"  Net:     <code>{_fmt_money(pnl_net_usd, ccy, sign=True)}</code>  "
        f"({_fmt_pct(pnl_net_pct)})"
    )

    # Delta vs hier
    if pnl_yesterday_usd is not None:
        delta_usd = pnl_today_usd - pnl_yesterday_usd
        if pnl_yesterday_usd != 0:
            delta_pct = (pnl_today_usd - pnl_yesterday_usd) / abs(pnl_yesterday_usd) * 100
            lines.append(
                f"  vs hier: <code>{_fmt_money(delta_usd, ccy, sign=True)}</code> "
                f"({_fmt_pct(delta_pct, decimals=0)})"
            )
        else:
            lines.append(
                f"  vs hier: <code>{_fmt_money(delta_usd, ccy, sign=True)}</code> (hier flat)"
            )

    # ── Bloc Attribution ──
    if attribution:
        lines.append("")
        lines.append("<b>Attribution</b>")
        for item in attribution:
            label = item.get("label", item.get("id", "?"))
            pnl = item.get("pnl_usd", 0)
            pct_of_total = item.get("pnl_pct_of_total", 0)
            bar = _ascii_bar(abs(pct_of_total))
            # Pad label à 12 chars pour alignement
            lines.append(
                f"  <code>{label:<13}</code> "
                f"<code>{_fmt_money(pnl, ccy, sign=True):>9}</code>  "
                f"<code>{bar}</code> {pct_of_total:.0f}%"
            )

    # ── Bloc Win Rate ──
    if win_rate_today:
        wins, losses = win_rate_today
        total = wins + losses
        wr_pct = (wins / total * 100) if total > 0 else 0
        wr_line = f"<b>Win Rate</b>  {wins}W / {losses}L ({wr_pct:.0f}%)"
        if win_rate_7d_pct is not None:
            wr_line += f"  —  7d avg: {win_rate_7d_pct:.0f}%"
        lines.append("")
        lines.append(wr_line)

    # ── Bloc Risk ──
    if peak_dd_pct is not None or capital_at_risk_usd is not None:
        lines.append("")
        lines.append("<b>Risk</b>")
        if peak_dd_pct is not None:
            # Icône proximité kill switch
            ratio = peak_dd_pct / dd_limit_pct if dd_limit_pct != 0 else 0
            if ratio < 0.5:
                dd_icon = "🟢"
            elif ratio < 0.8:
                dd_icon = "🟡"
            else:
                dd_icon = "🔴"
            lines.append(
                f"  Peak DD:    <code>{_fmt_pct(peak_dd_pct)}</code>  "
                f"{dd_icon} vs {_fmt_pct(dd_limit_pct, decimals=0)} limit"
            )
        if capital_at_risk_usd is not None and capital_total_usd is not None and capital_total_usd > 0:
            risk_pct = capital_at_risk_usd / capital_total_usd * 100
            # Format compact pour le total (K si > 10000)
            if capital_total_usd >= 10000:
                cap_str = f"${capital_total_usd/1000:.0f}K"
            else:
                cap_str = _fmt_money(capital_total_usd, ccy)
            lines.append(
                f"  At risk:    <code>{_fmt_money(capital_at_risk_usd, ccy)}</code> / "
                f"<code>{cap_str}</code>  ({risk_pct:.0f}%)"
            )

    # ── Régime ──
    if regime_today:
        lines.append("")
        if regime_yesterday and regime_yesterday != regime_today:
            lines.append(
                f"<b>Regime</b>  {_escape_html(regime_yesterday)} → "
                f"{_escape_html(regime_today)}"
            )
        else:
            lines.append(f"<b>Regime</b>  {_escape_html(regime_today)} (stable)")

    notify("\n".join(lines))


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
