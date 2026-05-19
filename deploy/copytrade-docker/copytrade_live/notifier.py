"""Telegram notifier — alertes BUY/SELL réelles.

Silent fail si Telegram creds absentes (option). Pas d'exception si l'envoi
échoue (on ne veut pas bloquer le trading).
"""
import logging
import os

import httpx

log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def _send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        log.debug("Telegram skip (creds missing)")
        return
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if r.status_code != 200:
            log.warning(f"Telegram {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"Telegram envoi échoué: {type(e).__name__}: {e}")


def notify_buy(market: str, outcome: str, size_shares: float, price: float, cost_usd: float,
               his_entry: float, target_size_usd: float) -> None:
    msg = (
        f"🟢 <b>BUY</b> Polymarket\n"
        f"<b>{market[:80]}</b>\n"
        f"→ <b>{outcome}</b>\n"
        f"Size: {size_shares:.2f} @ ${price:.3f} = <b>${cost_usd:.2f}</b>\n"
        f"His entry: ${his_entry:.3f} ({(price/his_entry - 1)*100:+.1f}%)\n"
        f"His size: ${target_size_usd:.0f}"
    )
    _send(msg)


def notify_sell(market: str, outcome: str, size_shares: float, price: float, proceeds_usd: float,
                realized_pnl_usd: float, fraction: float) -> None:
    pnl_emoji = "📈" if realized_pnl_usd >= 0 else "📉"
    msg = (
        f"🔴 <b>SELL</b> Polymarket {pnl_emoji}\n"
        f"<b>{market[:80]}</b>\n"
        f"→ <b>{outcome}</b>\n"
        f"Size: {size_shares:.2f} @ ${price:.3f} = <b>${proceeds_usd:.2f}</b>\n"
        f"Fraction closed: {fraction*100:.0f}%\n"
        f"Realized PnL: <b>{realized_pnl_usd:+.2f} USD</b>"
    )
    _send(msg)


def notify_error(context: str, err: str) -> None:
    msg = f"⚠️ Copytrade ERR\n<b>{context}</b>\n<code>{err[:300]}</code>"
    _send(msg)


def notify_redeemable(positions: list[dict]) -> None:
    """Alert when winning positions are ready to redeem on Polymarket UI."""
    if not positions:
        return
    total = sum(float(p.get("currentValue") or 0) for p in positions)
    lines = [f"🎯 <b>{len(positions)} position(s) ready to REDEEM</b>",
             f"Total payout: <b>${total:.2f}</b>", ""]
    for p in positions:
        title = (p.get("title") or "?")[:55]
        outcome = p.get("outcome", "?")
        cv = float(p.get("currentValue") or 0)
        pnl = float(p.get("cashPnl") or 0)
        lines.append(f"• {title} / <b>{outcome}</b>")
        lines.append(f"  ${cv:.2f} (PnL {pnl:+.2f})")
    lines.append("")
    lines.append("→ Open Polymarket UI and click <b>Redeem</b>")
    _send("\n".join(lines))


def notify_boot(equity_usd: float, dry_run: bool) -> None:
    mode = "DRY-RUN" if dry_run else "LIVE"
    msg = (
        f"🤖 Copytrade bot boot ({mode})\n"
        f"Target: surfandturf\n"
        f"Equity: ${equity_usd:.2f}"
    )
    _send(msg)
