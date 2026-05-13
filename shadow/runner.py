"""Shadow bot runner — cycle 4h en parallèle de la prod.

Mode : compte Alpaca paper SÉPARÉ (ALPACA_SHADOW_*). Les ordres sont placés
RÉELLEMENT chez Alpaca paper (sur un compte distinct du prod), pas simulés.
Le state authoritative est chez Alpaca, on log juste les décisions localement
pour analyse.

Isolation :
- Lit UNIQUEMENT ALPACA_SHADOW_* (validate_isolation() au boot)
- N'importe PAS live/alpaca_executor.py (qui utilise les credentials prod)
"""
from __future__ import annotations
import sys
import os
import json
import time
import signal as sig_module
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Charge .env (mêmes vars que le bot prod, mais on n'utilise que les ALPACA_SHADOW_*)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import config
from data.market_snapshot import fetch_macro_context, fetch_ohlcv_cache
from shadow.scorer import compute_score
from shadow.strategies import ALL_DETECTORS as _ALL_DETECTORS
from shadow import broker
from shadow.constants_v2 import (
    SCORE_FLOOR,
    TOP_N_SIGNALS,
    MAX_OPEN_POSITIONS,
    ACTIVE_DETECTORS,
    ATR_MULT_STOP_INIT,
    ATR_MULT_TRAIL,
    PROFIT_LOOSEN_PCT,
    DEFENSIVE_SYMBOLS,
    EQUITY_BEAR_SIZE_FACTOR,
    SECTOR_MAP,
    MAX_PER_SECTOR,
    MACRO_EXIT_PROFIT_PCT,
)
from shadow.regime import shield_active, equity_bear_active
from shadow.quality_gate import passes as gate_passes
from shadow.risk_guard import RiskGuard
from shadow.sizing import compute_size

# Filter detectors to v2 active subset (drops bleeders like supertrend, mean_reversion on 4h)
ALL_DETECTORS = [d for d in _ALL_DETECTORS
                 if d.__name__.replace("detect_", "") in ACTIVE_DETECTORS]

# Paths
LOG_DIR = "logs/shadow"
DECISIONS_LOG = f"{LOG_DIR}/decisions.jsonl"
EQUITY_LOG = f"{LOG_DIR}/equity.jsonl"
LOCAL_META = f"{LOG_DIR}/meta.json"  # méta locale (stops trailing, etc.)
RISK_STATE = f"{LOG_DIR}/risk_state.json"

# Cycle config — synchronisé sur les heartbeats prod (03/07/11/15/19/23 UTC)
CYCLE_HOURS_UTC = [3, 7, 11, 15, 19, 23]

_stop = False


def _sig_handler(signum, frame):
    global _stop
    _stop = True
    print(f"[SHADOW] signal {signum} reçu — arrêt propre…", flush=True)


def log_event(kind: str, data: dict):
    os.makedirs(LOG_DIR, exist_ok=True)
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **data}
    with open(DECISIONS_LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def log_equity(equity: float, cash: float, n_positions: int):
    os.makedirs(LOG_DIR, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "n_positions": n_positions,
        "perf_pct": round((equity - 100_000) / 100_000 * 100, 3),
    }
    with open(EQUITY_LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def load_meta() -> dict:
    if os.path.exists(LOCAL_META):
        with open(LOCAL_META) as f:
            return json.load(f)
    return {"positions_meta": {}}  # {symbol: {strategy, score, stop, stop_order_id}}


def save_meta(meta: dict):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOCAL_META, "w") as f:
        json.dump(meta, f, indent=2, default=str)


def _next_cycle_dt() -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for h in CYCLE_HOURS_UTC:
        cand = now.replace(hour=h, minute=3, second=0, microsecond=0)  # +3 min après prod
        if cand > now:
            return cand
    tom = (now + timedelta(days=1)).replace(hour=CYCLE_HOURS_UTC[0], minute=3, second=0, microsecond=0)
    return tom


def _alpaca_symbol(sym: str) -> str:
    """Forme normalisée Alpaca pour les positions/orders."""
    return sym.replace("/", "")


def run_cycle():
    now = datetime.now(timezone.utc)
    print(f"\n=== [SHADOW] Cycle {now.strftime('%Y-%m-%d %H:%M')} UTC ===", flush=True)

    # 1. Account state via Alpaca shadow
    try:
        account = broker.get_account()
        equity = float(account.get("equity", 0))
        cash = float(account.get("cash", 0))
        positions = broker.get_positions()
        pos_by_sym = {_alpaca_symbol(p["symbol"]): p for p in positions}
        print(f"[SHADOW] Equity: ${equity:.2f} | Cash: ${cash:.2f} | Positions: {len(positions)}", flush=True)
    except Exception as e:
        print(f"[SHADOW] account fetch échec: {e}", flush=True)
        return

    meta = load_meta()
    pos_meta = meta.setdefault("positions_meta", {})

    # 1b. Risk guard — MaxDD halt + cooldown tracking (v2)
    rg = RiskGuard.load(state_path=RISK_STATE, initial_equity=equity, now=now)
    rg.update_equity(equity, now=now)

    # Detect positions closed since last cycle (stop fired or manual exit) → cooldown
    closed_syms = set(pos_meta.keys()) - set(pos_by_sym.keys())
    for sym in closed_syms:
        rg.register_stop(sym, pnl=0.0, now=now)  # pnl=0 placeholder, only date matters for G4
        log_event("exit_detected", {"symbol": sym, "reason": "absent_from_alpaca"})
        del pos_meta[sym]
    if closed_syms:
        print(f"[SHADOW] {len(closed_syms)} positions closed → cooldown: {sorted(closed_syms)}", flush=True)

    # 2. Macro context
    try:
        macro = fetch_macro_context()
    except Exception as e:
        print(f"[SHADOW] macro échec: {e}", flush=True)
        macro = {"vix": 18, "btc_trend": "bull", "qqq_regime_ok": True}
    ctx = {
        "vix": macro.get("vix", 18),
        "btc_trend": macro.get("btc_trend", "bull"),
        "qqq_ok": macro.get("qqq_regime_ok", True),
        "qqq_full_uptrend": macro.get("qqq_full_uptrend", True),
    }
    print(f"[SHADOW] VIX={ctx['vix']:.1f} BTC={ctx['btc_trend']} QQQ_ok={ctx['qqq_ok']} full_uptrend={ctx['qqq_full_uptrend']}", flush=True)

    # 2b. SHIELD regime + halt check — skip new entries if either active.
    # equity_bear (lighter trigger: just SPY/QQQ < SMA200) rotates to defensives.
    macro_ctx = {"vix": ctx["vix"], "btc_trend": ctx["btc_trend"],
                 "qqq_regime_ok": ctx["qqq_ok"],
                 "qqq_full_uptrend": ctx["qqq_full_uptrend"]}
    shielded = shield_active(macro_ctx)
    halted = rg.is_halted(now=now)
    skip_new_entries = shielded or halted
    equity_bear = equity_bear_active(macro_ctx)
    rotate_defensives = equity_bear and not skip_new_entries
    size_factor = EQUITY_BEAR_SIZE_FACTOR if rotate_defensives else 1.0
    if shielded:
        print(f"[SHADOW] SHIELD active (VIX>{macro.get('vix', 0):.1f} or bear macro) — no new entries", flush=True)
    if halted:
        print(f"[SHADOW] HALT active until {rg.halt_until} (MaxDD breached) — no new entries", flush=True)
    if rotate_defensives:
        print(f"[SHADOW] EQUITY_BEAR — rotation défensive: scan restreint à {DEFENSIVE_SYMBOLS}, sizing × {size_factor}", flush=True)

    # 3. Fetch OHLCV
    symbols = list(getattr(config, "SYMBOLS", []) or
                   (list(getattr(config, "CRYPTO", [])) + list(getattr(config, "STOCKS", []))))
    try:
        cache_4h = fetch_ohlcv_cache(symbols, timeframe="4h", days=55)
        cache_1d = fetch_ohlcv_cache(symbols, timeframe="1d", days=220)
    except Exception as e:
        print(f"[SHADOW] OHLCV échec: {e}", flush=True)
        return
    print(f"[SHADOW] OHLCV: {len(cache_4h)} 4h | {len(cache_1d)} 1d", flush=True)

    # 4. Trailing stops update sur positions ouvertes (adaptive — v2)
    from strategies.supertrend import compute_atr
    for sym, p in list(pos_by_sym.items()):
        # Alpaca retourne "BTCUSD" (sans slash) ; notre cache OHLCV utilise "BTC/USD"
        df = cache_4h.get(sym)
        if df is None:
            for k, v in cache_4h.items():
                if k.replace("/", "") == sym:
                    df = v
                    break
        if df is None or len(df) < 15:
            continue
        atr = float(compute_atr(df["high"], df["low"], df["close"], 14).iloc[-1] or 0)
        close = float(df["close"].iloc[-1])
        if atr <= 0:
            continue
        # Macro-aware exit (iter-6 #3): SHIELD/HALT + position at ≥ +15%
        # → lock the gain at market. +5% was too tight; +15% protects only
        # strong winners while normal trends keep running.
        m = pos_meta.get(sym, {})
        entry = float(m.get("entry_price") or 0)
        if (shielded or halted) and entry > 0 and (close - entry) / entry >= MACRO_EXIT_PROFIT_PCT:
            qty_avail = float(p.get("qty_available") or p.get("qty") or 0)
            if qty_avail > 0:
                old_id = m.get("stop_order_id")
                if old_id:
                    broker.cancel_order(old_id)
                orig_sym = p["symbol"]
                if any(c in orig_sym for c in ["BTC", "ETH", "SOL", "AVAX", "LINK"]) and "/" not in orig_sym:
                    orig_sym = orig_sym[:-3] + "/" + orig_sym[-3:]
                sell_res = broker.market_sell(orig_sym, qty_avail)
                if sell_res.get("ok"):
                    pnl_pct = (close - entry) / entry
                    log_event("macro_take_profit", {
                        "symbol": sym, "reason": "SHIELD" if shielded else "HALT",
                        "entry": entry, "exit": sell_res.get("filled_avg", close),
                        "pnl_pct": pnl_pct,
                    })
                    print(f"[SHADOW] MACRO_TP {sym} ({'SHIELD' if shielded else 'HALT'}) entry={entry:.4f} exit≈{close:.4f} pnl=+{pnl_pct*100:.1f}%", flush=True)
                    pos_meta.pop(sym, None)
                    continue
                else:
                    print(f"[SHADOW] MACRO_TP {sym} échec: {sell_res.get('error')}", flush=True)
        # Adaptive trailing: tight (4.0x) until +5% profit, then loosen to 5.0x
        if entry > 0:
            pnl_pct = (close - entry) / entry
            atr_mult = ATR_MULT_TRAIL if pnl_pct >= PROFIT_LOOSEN_PCT else ATR_MULT_STOP_INIT
        else:
            atr_mult = ATR_MULT_STOP_INIT  # entry unknown (queued) → use init
        new_stop = round(close - atr_mult * atr, 2)
        old_stop = m.get("stop", 0)
        existing_stop_id = m.get("stop_order_id")

        # BUG FIX (iter-6 #5): if no stop_order_id exists, position is UNPROTECTED
        # (filled after queued buy + price dropped slightly → trailing guard
        # refused to place initial stop). Force-place stop regardless of trailing.
        needs_initial_stop = existing_stop_id is None
        should_update = needs_initial_stop or (new_stop > old_stop)

        if should_update:
            # Annuler ancien stop si présent + replacer
            if existing_stop_id:
                broker.cancel_order(existing_stop_id)
            qty = float(p.get("qty_available") or p.get("qty") or 0)
            if qty > 0:
                # Cherche le symbole original (avec slash si crypto)
                orig_sym = p["symbol"]
                # Alpaca retourne BTCUSD sans slash — pour les ordres, accepte BTC/USD avec slash
                if any(c in orig_sym for c in ["BTC", "ETH", "SOL", "AVAX", "LINK"]) and "/" not in orig_sym:
                    orig_sym = orig_sym[:-3] + "/" + orig_sym[-3:]
                # For "needs_initial_stop" case, anchor to entry - ATR_MULT_STOP_INIT
                # to prevent a too-tight stop right after a drawn-down fill.
                target_stop = new_stop
                if needs_initial_stop and entry > 0:
                    initial_anchor = round(entry - ATR_MULT_STOP_INIT * atr, 2)
                    target_stop = max(new_stop, initial_anchor)
                res = broker.place_stop(orig_sym, qty, target_stop)
                if res.get("ok"):
                    m["stop"] = target_stop
                    m["stop_order_id"] = res["id"]
                    pos_meta[sym] = m
                    reason = "INIT" if needs_initial_stop else "TRAIL"
                    log_event(reason.lower(), {"symbol": sym, "new_stop": target_stop, "qty": qty})
                    print(f"[SHADOW] {reason} {sym} → {target_stop}", flush=True)
                else:
                    print(f"[SHADOW] {('INIT_STOP' if needs_initial_stop else 'TRAIL')} {sym} échec: {res.get('error')}", flush=True)

    # 5. Récupère les ordres BUY en cours (queued/accepted) pour ne pas re-trigger
    # sur des actifs dont l'ordre est déjà placé mais pas encore fillé (typique pour
    # stocks hors marché : entry placée à 20h UTC, fill au lendemain 13:30 UTC ;
    # entre les deux on aurait une position fantôme et le scan refirait le signal).
    try:
        open_orders = broker.get_open_orders()
        pending_buy_syms = {_alpaca_symbol(o["symbol"]) for o in open_orders
                            if o.get("side") == "buy" and
                            o.get("status") in ("accepted", "new", "pending_new", "partially_filled")}
    except Exception as e:
        print(f"[SHADOW] open_orders fetch échec: {e} — skip cycle pour éviter doublons", flush=True)
        return

    # 6. Skip scan if SHIELD or HALT active (v2)
    if skip_new_entries:
        print(f"[SHADOW] skip scan (shielded={shielded} halted={halted})", flush=True)
    else:
        # 6a. Scan signaux pour symboles sans position et sans ordre buy en cours
        # En equity_bear: scan restreint au subset défensif (gold/healthcare/energy/consumer)
        scan_universe = (
            [s for s in symbols if s in DEFENSIVE_SYMBOLS]
            if rotate_defensives else symbols
        )
        candidates = []
        for sym in scan_universe:
            alp_sym = _alpaca_symbol(sym)
            if alp_sym in pos_by_sym or alp_sym in pending_buy_syms:
                continue
            df_4h = cache_4h.get(sym)
            df_1d = cache_1d.get(sym)
            if df_4h is None:
                continue
            for detector in ALL_DETECTORS:
                try:
                    sig = detector(sym, df_4h, df_1d)
                    if sig is None:
                        continue
                    sig.score = compute_score(sig, ctx)
                    if sig.score >= SCORE_FLOOR:
                        candidates.append(sig)
                except Exception as e:
                    print(f"[SHADOW] detector {detector.__name__} {sym}: {e}", flush=True)

        # Dédup intra-cycle par symbole : un actif ne peut générer qu'UN seul ordre par cycle.
        # On garde le meilleur score.
        best_by_symbol: dict[str, "Signal"] = {}
        for sig in candidates:
            if sig.symbol not in best_by_symbol or sig.score > best_by_symbol[sig.symbol].score:
                best_by_symbol[sig.symbol] = sig
        sorted_cands = sorted(best_by_symbol.values(), key=lambda s: s.score, reverse=True)

        # 6b. Quality gate (G1 score, G2 MTF, G3 volume, G4 cooldown) + sector diversification
        gate_passed = [s for s in sorted_cands if gate_passes(s, rg, now=now)]
        # Already-open positions count against their sector's quota
        sector_count: dict[str, int] = {}
        for sym_alp in pos_by_sym.keys():
            # Reverse-map Alpaca symbol (BTCUSD) back to our internal format (BTC/USD)
            internal = sym_alp
            for s in SECTOR_MAP:
                if s.replace("/", "") == sym_alp:
                    internal = s
                    break
            sec = SECTOR_MAP.get(internal, "other")
            sector_count[sec] = sector_count.get(sec, 0) + 1
        accepted = []
        for sig in gate_passed:
            sec = SECTOR_MAP.get(sig.symbol, "other")
            if sector_count.get(sec, 0) >= MAX_PER_SECTOR:
                continue
            accepted.append(sig)
            sector_count[sec] = sector_count.get(sec, 0) + 1
            if len(accepted) >= TOP_N_SIGNALS:
                break
        rejected_n = len(sorted_cands) - len(gate_passed)
        sector_skipped = len(gate_passed) - len(accepted)
        print(f"[SHADOW] {len(sorted_cands)} signaux ≥ {SCORE_FLOOR} → quality_gate {len(gate_passed)} (rejetés {rejected_n}) → sector_filter {len(accepted)} (skipped {sector_skipped})", flush=True)

        # 7. Ouvrir positions — score-weighted sizing (v2) × size_factor (rotation)
        n_open = len(pos_by_sym)
        for rank, sig in enumerate(accepted):
            if n_open >= MAX_OPEN_POSITIONS:
                break
            # Score-weighted sizing × size_factor (vol-adjust disabled live too)
            size_res = compute_size(rank=rank, cash=cash,
                                    entry_price=sig.entry_price)
            if size_factor != 1.0:
                size_res = type(size_res)(qty=size_res.qty * size_factor,
                                          notional=size_res.notional * size_factor)
            if size_res.qty <= 0:
                continue
            # Floor décimales
            is_crypto = "/" in sig.symbol
            import math
            decimals = 6 if is_crypto else 5
            factor = 10 ** decimals
            size = math.floor(size_res.qty * factor) / factor
            if size <= 0:
                continue
            order_value = size * sig.entry_price
            if is_crypto and order_value < 10:
                continue  # min Alpaca crypto

            # Stop initial = entry - ATR_MULT_STOP_INIT × ATR (override sig.stop_price)
            stop_initial = round(sig.entry_price - ATR_MULT_STOP_INIT * sig.atr, 2)

            # Place market buy sur compte shadow
            res = broker.market_buy(sig.symbol, size)
            if not res.get("ok"):
                print(f"[SHADOW] BUY {sig.symbol} échec: {res.get('error')}", flush=True)
                continue

            fill_price = res.get("filled_avg") or sig.entry_price
            fill_qty = res.get("filled_qty") or size
            status = res.get("status", "filled")
            queued = (status != "filled")

            alp_sym = _alpaca_symbol(sig.symbol)
            pos_meta[alp_sym] = {
                "strategy": sig.strategy,
                "score": sig.score,
                "entry_price": fill_price if not queued else None,
                "stop": stop_initial,
                "stop_order_id": None,
                "buy_order_id": res.get("id"),
                "queued": queued,
                "entry_date": now.isoformat(),
                "rationale": sig.rationale,
                "rank": rank,
                "notional": round(size_res.notional, 2),
            }
            # Si fillé immédiatement, placer le stop. Sinon stop placé au prochain cycle.
            if not queued:
                stop_res = broker.place_stop(sig.symbol, fill_qty, stop_initial)
                pos_meta[alp_sym]["stop_order_id"] = stop_res.get("id") if stop_res.get("ok") else None

            log_event("entry", {
                "symbol": sig.symbol, "strategy": sig.strategy, "score": round(sig.score, 1),
                "rank": rank, "weight": size_res.notional / cash if cash > 0 else 0,
                "size": fill_qty, "price": fill_price, "stop": stop_initial,
                "status": status,
                "rationale": sig.rationale,
            })
            suffix = f" [QUEUED — fill prochaine session]" if queued else ""
            print(f"[SHADOW] BUY rank={rank} {sig.symbol} ({sig.strategy}) score={sig.score:.1f} @ {fill_price:.4f} qty={fill_qty:.6f} stop={stop_initial:.4f} notional=${size_res.notional:.0f}{suffix}", flush=True)
            n_open += 1

    # 8. Sauve meta + persiste risk guard (prune expired entries pour garder l'état compact)
    save_meta(meta)
    rg.prune_expired(now=now)
    rg.save()
    try:
        account = broker.get_account()
        equity = float(account.get("equity", 0))
        cash = float(account.get("cash", 0))
        log_equity(equity, cash, n_open)
        print(f"[SHADOW] Final: equity ${equity:.2f} ({(equity-100000)/1000:.2f}%) | cash ${cash:.2f} | positions {n_open}", flush=True)
    except Exception as e:
        print(f"[SHADOW] final account fetch échec: {e}", flush=True)


def main():
    sig_module.signal(sig_module.SIGINT, _sig_handler)
    sig_module.signal(sig_module.SIGTERM, _sig_handler)

    # ── ISOLATION CHECK ── fail-fast si credentials shadow corrompus
    try:
        broker.validate_isolation()
        print("[SHADOW] ✓ Isolation OK : credentials shadow distincts du prod", flush=True)
    except Exception as e:
        print(f"[SHADOW] ⛔ ISOLATION FAILED : {e}", flush=True)
        sys.exit(1)

    print(f"[SHADOW] Démarré — compte Alpaca paper isolé — cycle 4h", flush=True)
    try:
        run_cycle()
    except Exception as e:
        print(f"[SHADOW] cycle initial échec: {e}", flush=True)
        import traceback; traceback.print_exc()

    while not _stop:
        next_dt = _next_cycle_dt()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        wait_s = max(1, int((next_dt - now).total_seconds()))
        print(f"[SHADOW] Prochain cycle: {next_dt.strftime('%H:%M')} UTC (dans {wait_s//60} min)", flush=True)
        slept = 0
        while slept < wait_s and not _stop:
            chunk = min(60, wait_s - slept)
            time.sleep(chunk)
            slept += chunk
        if _stop:
            break
        try:
            run_cycle()
        except Exception as e:
            print(f"[SHADOW] cycle échec: {e}", flush=True)
            import traceback; traceback.print_exc()

    print("[SHADOW] Arrêté.", flush=True)


if __name__ == "__main__":
    main()
