"""Main loop — VPS Docker variant.

Lecture directe du fichier decisions.jsonl monté en volume (pas de SSH).
Lance via: python -m copytrade_live.poller
"""
import json
import logging
import re
import signal
import sys
import time

from . import config, sizing, state, executor, notifier, status_writer, optionb

config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(config.LOGS_DIR / "runtime.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("copytrade")

_running = True


def _sigterm(_sig, _frame):
    global _running
    _running = False
    log.info("SIGTERM reçu, exit propre au prochain cycle")


def fetch_local_decisions(since_ts: int, tail_lines: int = 200) -> list[dict]:
    """Lit les N dernières lignes de decisions.jsonl plus récentes que since_ts."""
    try:
        with open(config.DECISIONS_PATH) as f:
            all_lines = f.readlines()
    except FileNotFoundError:
        log.warning(f"Decisions file absent: {config.DECISIONS_PATH}")
        return []
    decisions = []
    for line in all_lines[-tail_lines:]:
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("ts", 0) <= since_ts:
            continue
        decisions.append(d)
    return decisions


def filter_relevant(decisions: list[dict]) -> list[dict]:
    return [d for d in decisions
            if d.get("wallet") == config.TARGET_WALLET
            and d.get("action") != "skipped"]


def write_jsonl(filename: str, record: dict) -> None:
    path = config.LOGS_DIR / filename
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def handle_buy(decision: dict, positions: dict) -> None:
    if len(positions) >= config.MAX_POSITIONS:
        log.info(f"SKIP BUY: max_positions={config.MAX_POSITIONS}")
        write_jsonl("trades.jsonl", {**decision, "local_action": "skip_max_positions"})
        return
    if decision.get("target_size_usd", 0) < config.MIN_TARGET_SIZE_USD:
        log.info(f"SKIP BUY: target < ${config.MIN_TARGET_SIZE_USD}")
        write_jsonl("trades.jsonl", {**decision, "local_action": "skip_target_too_small"})
        return
    p = decision.get("price", 1.0)
    if p > config.MAX_ENTRY_PRICE:
        log.info(f"SKIP BUY: entry {p:.3f} > MAX_ENTRY_PRICE {config.MAX_ENTRY_PRICE} (underdog filter)")
        write_jsonl("trades.jsonl", {**decision, "local_action": "skip_price_too_high"})
        return
    if p < config.MIN_ENTRY_PRICE:
        log.info(f"SKIP BUY: entry {p:.3f} < MIN_ENTRY_PRICE {config.MIN_ENTRY_PRICE} (lottery ticket filter)")
        write_jsonl("trades.jsonl", {**decision, "local_action": "skip_lottery_ticket"})
        return

    if config.OPTIONB_FILTERS:
        ok, reason = optionb.optionb_passes(decision)
        if not ok:
            log.info(f"SKIP BUY: {reason}")
            write_jsonl("trades.jsonl", {**decision, "local_action": f"skip_{reason}"})
            return

        # Record this BUY in the conviction rolling window BEFORE checking, so
        # the current chunk contributes to cumulative.
        optionb.record_observation(decision)

        conv_ok, conv_reason = optionb.conviction_passes(decision)
        if not conv_ok:
            log.info(f"SKIP BUY: {conv_reason}")
            write_jsonl("trades.jsonl", {**decision, "local_action": f"skip_{conv_reason}"})
            return

    trade_pct = float(decision.get("trade_pct", 0) or 0)
    tier = sizing.describe_tier(p, trade_pct)
    size_usd = sizing.compute_size_usd(p, trade_pct)
    if size_usd is None:
        log.info(f"SKIP BUY: tier={tier} skipped (px={p:.3f}, trade_pct={trade_pct:.3f})")
        write_jsonl("trades.jsonl", {**decision, "local_action": "skip_tier_conviction", "tier": tier, "trade_pct": trade_pct})
        return

    # Prefer pre-resolved IDs from the decision (already known by bot-cp scanner
    # OR /positions polling fallback) — skips Gamma search and works even when
    # the market is closed/resolved so search returns nothing.
    if decision.get("asset") and decision.get("conditionId"):
        resolved = {
            "token_id": decision["asset"],
            "condition_id": decision["conditionId"],
            "outcome_index": int(decision.get("outcomeIndex", 0)),
            "market_slug": "",
        }
    else:
        resolved = executor.resolve_outcome_to_token_id(decision["market"], decision["outcome"])
        if not resolved:
            log.warning(f"SKIP BUY: outcome non résolu '{decision['market'][:40]}' / '{decision['outcome']}'")
            write_jsonl("trades.jsonl", {**decision, "local_action": "skip_resolve_failed"})
            return

    # Cap per (market, outcome) via token_id — NOT per market — so we can
    # mirror RN1's "reverse conviction" pattern (he buys both sides of a
    # binary, never SELLs the loser). Each binary outcome has a distinct
    # Polymarket token_id, so matching on token_id is the cleanest signal.
    # NOTE: positions dict is keyed by token_id, but other entries may also
    # carry the same token_id field; we use the dict lookup directly.
    target_tok = resolved["token_id"]
    existing_cost_same_outcome = float(positions.get(target_tok, {}).get("cost_usd", 0))
    if existing_cost_same_outcome + size_usd > config.MAX_USD_PER_MARKET:
        log.info(f"SKIP BUY: outcome saturé (${existing_cost_same_outcome:.2f} + ${size_usd:.2f} > ${config.MAX_USD_PER_MARKET}) tier={tier}")
        write_jsonl("trades.jsonl", {**decision, "local_action": "skip_outcome_saturated", "tier": tier})
        return

    his_entry = decision.get("price", 0)
    current_ask = executor.get_market_price(resolved["token_id"], "buy")
    if current_ask and his_entry > 0 and current_ask > his_entry * config.MAX_PRICE_DRIFT:
        log.info(f"SKIP BUY: chasing ({current_ask:.3f} > his_entry {his_entry:.3f} × {config.MAX_PRICE_DRIFT})")
        write_jsonl("trades.jsonl", {**decision, "local_action": "skip_price_drift",
                                      "his_entry": his_entry, "current_ask": current_ask})
        return

    log.info(f"BUY tier={tier} size_usd=${size_usd:.2f} (px={p:.3f}, trade_pct={trade_pct:.3f})")
    result = executor.place_buy(
        token_id=resolved["token_id"],
        size_usd=size_usd,
        max_price=config.MAX_ENTRY_PRICE,
    )
    if result.get("status") in ("dry_run", "submitted"):
        state.record_buy(
            positions, token_id=resolved["token_id"],
            market=decision["market"], outcome=decision["outcome"],
            size_shares=result["size_shares"], avg_price=result["price"],
            cost_usd=result["cost_usd"], target_hash=decision.get("target_hash",""),
            condition_id=resolved.get("condition_id",""),
        )
        state.save_positions(positions)
        if result.get("status") == "submitted":
            notifier.notify_buy(
                market=decision["market"], outcome=decision["outcome"],
                size_shares=result["size_shares"], price=result["price"],
                cost_usd=result["cost_usd"], his_entry=his_entry,
                target_size_usd=decision.get("target_size_usd", 0),
            )
    write_jsonl("trades.jsonl", {**decision, "local_action": "buy", "exec_result": result})


def handle_sell(decision: dict, positions: dict) -> None:
    resolved = executor.resolve_outcome_to_token_id(decision["market"], decision["outcome"])
    if not resolved:
        log.warning(f"SKIP SELL: outcome non résolu")
        write_jsonl("trades.jsonl", {**decision, "local_action": "skip_resolve_failed"})
        return
    token_id = resolved["token_id"]
    pos = positions.get(token_id)
    if not pos:
        log.info(f"SKIP SELL: pas de position locale sur {token_id[:10]}...")
        write_jsonl("trades.jsonl", {**decision, "local_action": "skip_no_position"})
        return

    m = re.search(r"sell_mirrored_fraction=([0-9.]+)", decision.get("rationale", ""))
    fraction = float(m.group(1)) if m else 1.0
    fraction = min(max(fraction, 0.0), 1.0)
    size_to_sell = pos["size_shares"] * fraction
    result = executor.place_sell(token_id=token_id, size_shares=size_to_sell)
    if result.get("status") in ("dry_run", "submitted"):
        _, realized = state.record_sell(positions, token_id=token_id,
                                        size_shares=size_to_sell, exit_price=result["price"])
        state.save_positions(positions)
        result["realized_pnl_usd"] = realized
        if result.get("status") == "submitted":
            notifier.notify_sell(
                market=decision["market"], outcome=decision["outcome"],
                size_shares=result["size_shares"], price=result["price"],
                proceeds_usd=result["proceeds_usd"],
                realized_pnl_usd=realized, fraction=fraction,
            )
    write_jsonl("trades.jsonl", {**decision, "local_action": "sell",
                                  "fraction": fraction, "exec_result": result})


def cycle(meta: dict, positions: dict) -> tuple[int, int]:
    """Returns (n_decisions_new, n_relevant_executed)."""
    decisions = fetch_local_decisions(since_ts=meta["last_seen_ts"])
    if not decisions:
        return 0, 0
    relevant = filter_relevant(decisions)
    log.info(f"Cycle: {len(decisions)} new, {len(relevant)} {config.TARGET_WALLET} executed")
    for d in sorted(relevant, key=lambda x: x["ts"]):
        try:
            if d.get("side") == "BUY":
                handle_buy(d, positions)
            elif d.get("side") == "SELL":
                handle_sell(d, positions)
            meta["last_seen_ts"] = max(meta["last_seen_ts"], d["ts"])
            state.save_meta(meta)
        except Exception as e:
            log.error(f"Erreur ts={d.get('ts')}: {type(e).__name__}: {e}")
    return len(decisions), len(relevant)


def main() -> None:
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)
    config.validate()
    log.info(f"Boot — target={config.TARGET_WALLET}, sizing_mode={config.SIZING_MODE}, "
             f"max_pos={config.MAX_POSITIONS}, kill_eq=${config.KILL_EQUITY_USD}, "
             f"dry_run={config.DRY_RUN}")
    if config.SIZING_MODE == "tiered":
        log.info(f"Tier grid — penny[<{config.TIER_PENNY_MAX}]: ${config.TIER_PENNY_SIZE} if pct>{config.TIER_PENNY_MIN_CONVICTION} | "
                 f"mid[<{config.TIER_MID_MAX}]: ${config.TIER_MID_MIN_SIZE}-${config.TIER_MID_MAX_SIZE} if pct>{config.TIER_MID_MIN_CONVICTION} | "
                 f"fav: ${config.TIER_FAV_SIZE} if pct>{config.TIER_FAV_MIN_CONVICTION}")
    elif config.SIZING_MODE == "absolute_band":
        skip_info = ""
        if config.TIER_SKIP_HIGH > config.TIER_PENNY_MAX:
            skip_info = f" | SKIP[{config.TIER_PENNY_MAX}-{config.TIER_SKIP_HIGH}]"
        log.info(f"Absolute band — penny[<{config.TIER_PENNY_MAX}]: ${config.TIER_PENNY_SIZE}{skip_info} | normal[>={config.TIER_SKIP_HIGH}]: ${config.TIER_NORMAL_SIZE} (MIN_TARGET=${config.MIN_TARGET_SIZE_USD})")
    else:
        log.info(f"Fixed size — ${config.FIXED_SIZE_USD}")
    log.info(f"Filters — entry∈[{config.MIN_ENTRY_PRICE}, {config.MAX_ENTRY_PRICE}], "
             f"max_drift={config.MAX_PRICE_DRIFT}x, "
             f"max_per_market=${config.MAX_USD_PER_MARKET}, "
             f"min_target=${config.MIN_TARGET_SIZE_USD}")
    log.info(f"Decisions source: {config.DECISIONS_PATH}")

    meta = state.load_meta()
    positions = state.load_positions()
    log.info(f"State: last_seen_ts={meta['last_seen_ts']}, {len(positions)} positions")
    boot_removed, boot_added, boot_redeemables = state.reconcile_resolved(positions)
    if boot_removed or boot_added:
        log.info(f"Boot reconcile: -{len(boot_removed)} +{len(boot_added)} -> {len(positions)} positions")
    notified = set(meta.get("notified_redeemables") or [])
    new_redeemables = [p for p in boot_redeemables if p.get("asset") and p["asset"] not in notified]
    if new_redeemables:
        log.info(f"Boot: {len(new_redeemables)} new redeemable position(s) — notifying")
        notifier.notify_redeemable(new_redeemables)
        notified.update(p["asset"] for p in new_redeemables)
    current_assets = {p.get("asset") for p in boot_redeemables if p.get("asset")}
    notified &= current_assets  # forget assets that are no longer redeemable (redeemed by user)
    meta["notified_redeemables"] = sorted(notified)
    state.save_meta(meta)

    clob_bal: float | None = None
    try:
        clob_bal = executor.get_clob_balance_usd()
        log.info(f"CLOB balance: ${clob_bal:.4f}")
        notifier.notify_boot(equity_usd=clob_bal, dry_run=config.DRY_RUN)
        if clob_bal < config.KILL_EQUITY_USD:
            log.warning(f"Solde sous kill_eq (${clob_bal:.2f}) — pause préventive")
    except Exception as e:
        log.error(f"Boot: get_clob_balance échec: {type(e).__name__}: {e}")

    cycle_count = 0
    # Balance is fetched at boot then every BALANCE_REFRESH_CYCLES cycles
    # (Polymarket CLOB API rate-limited; ~5 min refresh is plenty).
    BALANCE_REFRESH_CYCLES = 5
    while _running:
        cycle_count += 1
        last_new = last_exec = 0
        try:
            last_new, last_exec = cycle(meta, positions)
        except Exception as e:
            log.error(f"Cycle exception: {type(e).__name__}: {e}")
        if cycle_count % BALANCE_REFRESH_CYCLES == 0 or last_exec > 0:
            try:
                clob_bal = executor.get_clob_balance_usd()
            except Exception as e:
                log.warning(f"Balance refresh échec: {type(e).__name__}: {e}")
        # Reconcile + redeemable detection every cycle: data-api lag means
        # winning positions can flip redeemable=True and be redeemed via UI
        # within minutes — checking only on 5-cycle cadence misses fast windows.
        try:
            _, _, redeemables = state.reconcile_resolved(positions)
            notified = set(meta.get("notified_redeemables") or [])
            new_redeem = [p for p in redeemables if p.get("asset") and p["asset"] not in notified]
            if new_redeem:
                log.info(f"{len(new_redeem)} new redeemable position(s) — notifying Telegram")
                notifier.notify_redeemable(new_redeem)
                notified.update(p["asset"] for p in new_redeem)
            current_assets = {p.get("asset") for p in redeemables if p.get("asset")}
            notified &= current_assets
            if notified != set(meta.get("notified_redeemables") or []):
                meta["notified_redeemables"] = sorted(notified)
                state.save_meta(meta)
        except Exception as e:
            log.warning(f"reconcile_resolved échec: {type(e).__name__}: {e}")
        try:
            status_writer.write_status(
                meta, positions,
                clob_balance_usd=clob_bal,
                cycle_count=cycle_count,
                last_cycle_decisions=last_new,
                last_cycle_executed=last_exec,
            )
        except Exception as e:
            log.warning(f"status_writer échec: {type(e).__name__}: {e}")
        time.sleep(config.POLL_INTERVAL_SEC)

    log.info("Stopped")


if __name__ == "__main__":
    main()
