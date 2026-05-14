"""One-shot: recompute chandelier stops and reset them for all held stock
positions in shadow. Useful when DAY stops got renewed at stale levels and the
main 4h cycle hasn't trailed them yet.

Usage: python3 -m shadow.force_trail_stocks

Effect:
- For each held stock (non-crypto) position on Alpaca shadow:
  1. Fetch 4h OHLCV (55 days)
  2. Compute chandelier_high(22) - 4×ATR(14)
  3. If new_stop > current stop on broker → cancel existing stop, place GTC
     stop at new level
  4. Update pos_meta + notify Telegram with summary
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Load .env early so broker credentials resolve
from dotenv import load_dotenv

load_dotenv()

from shadow import broker
from shadow.constants_v2 import ATR_MULT_STOP_INIT, ATR_MULT_TRAIL, PROFIT_LOOSEN_PCT
from data.market_snapshot import fetch_ohlcv_cache
from strategies.supertrend import compute_atr
from live.notifier import notify

META_PATH = Path("/home/botuser/bot-trading/logs/shadow/meta.json")


def _is_crypto(symbol: str) -> bool:
    return any(c in symbol for c in ("BTC", "ETH", "SOL", "AVAX", "LINK"))


def _load_meta() -> dict:
    if not META_PATH.exists():
        return {}
    return json.loads(META_PATH.read_text())


def _save_meta(meta: dict) -> None:
    META_PATH.write_text(json.dumps(meta, indent=2))


def main() -> int:
    broker.validate_isolation()
    positions = broker.get_positions()
    stocks = [p for p in positions if not _is_crypto(p["symbol"])]
    if not stocks:
        print("[FORCE-TRAIL] aucune position stock — rien à faire.")
        return 0

    syms = [p["symbol"] for p in stocks]
    print(f"[FORCE-TRAIL] positions stocks: {syms}")

    caches = fetch_ohlcv_cache(syms, timeframe="4h", days=55)
    meta = _load_meta()
    positions_meta = meta.setdefault("positions_meta", {})

    open_orders = broker.get_open_orders()
    stops_by_sym = {
        o["symbol"]: o for o in open_orders
        if o.get("side") == "sell" and o.get("type") in ("stop", "stop_limit")
    }

    updates: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    for p in stocks:
        sym = p["symbol"]
        df = caches.get(sym)
        if df is None or len(df) < 22:
            errors.append(f"{sym}: OHLCV insuffisant ({len(df) if df is not None else 0} bars)")
            continue

        atr = float(compute_atr(df["high"], df["low"], df["close"], 14).iloc[-1] or 0)
        if atr <= 0:
            errors.append(f"{sym}: ATR=0")
            continue

        close = float(df["close"].iloc[-1])
        entry = float(p.get("avg_entry_price") or 0)
        qty = float(p.get("qty") or 0)
        pnl_pct = (close - entry) / entry if entry > 0 else 0
        atr_mult = ATR_MULT_TRAIL if pnl_pct >= PROFIT_LOOSEN_PCT else ATR_MULT_STOP_INIT
        chandelier_high = float(df["high"].tail(22).max())
        new_stop = round(chandelier_high - atr_mult * atr, 2)

        existing_order = stops_by_sym.get(sym)
        current_stop = float(existing_order.get("stop_price") or 0) if existing_order else 0

        if new_stop <= current_stop:
            skipped.append(f"{sym}: new={new_stop} ≤ old={current_stop}")
            continue

        # PATCH first (safe — pas de fenêtre d'unprotection). Cancel+create
        # uniquement si PATCH refuse (rare : ordre déjà rempli/expiré).
        new_order_id = None
        if existing_order:
            patch_res = broker.replace_stop(existing_order["id"], new_stop)
            if patch_res.get("ok"):
                new_order_id = patch_res["id"]
            else:
                # Fallback: cancel + create. Si create échoue, on garde
                # l'orphan flag pour que stop_monitor retente au prochain tick.
                broker.cancel_order(existing_order["id"])
                place_res = broker.place_stop(sym, qty, new_stop)
                if place_res.get("ok"):
                    new_order_id = place_res["id"]
                else:
                    errors.append(f"{sym}: patch={patch_res.get('error')}, place={place_res.get('error')}")
                    continue
        else:
            place_res = broker.place_stop(sym, qty, new_stop)
            if not place_res.get("ok"):
                errors.append(f"{sym}: place échoué ({place_res.get('error')})")
                continue
            new_order_id = place_res["id"]

        # Persist new state in meta
        m = positions_meta.get(sym, {}) or {}
        m["stop"] = new_stop
        m["stop_order_id"] = new_order_id
        if entry > 0:
            m.setdefault("entry_price", entry)
            m.setdefault("qty", qty)
        positions_meta[sym] = m

        delta = new_stop - current_stop if current_stop > 0 else new_stop
        updates.append(
            f"{sym}: {current_stop:.2f}→{new_stop:.2f} (+{delta:.2f}, close={close:.2f}, pnl={pnl_pct*100:+.1f}%)"
        )
        print(f"[FORCE-TRAIL] {sym} stop updated: {current_stop:.2f} → {new_stop:.2f} (atr_mult={atr_mult}, atr={atr:.2f})")

    _save_meta(meta)

    summary = [f"🔧 Shadow force-trail stocks"]
    if updates:
        summary.append(f"\n✅ Updated ({len(updates)}):")
        summary.extend(f"  • {u}" for u in updates)
    if skipped:
        summary.append(f"\n⏸ Skipped (new ≤ old) ({len(skipped)}):")
        summary.extend(f"  • {s}" for s in skipped)
    if errors:
        summary.append(f"\n❌ Errors ({len(errors)}):")
        summary.extend(f"  • {e}" for e in errors)
    msg = "\n".join(summary)
    print()
    print(msg)
    notify(msg)
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
