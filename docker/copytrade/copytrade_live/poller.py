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

from . import config, state, executor

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
    if decision.get("price", 1.0) > config.MAX_ENTRY_PRICE:
        log.info(f"SKIP BUY: prix > {config.MAX_ENTRY_PRICE}")
        write_jsonl("trades.jsonl", {**decision, "local_action": "skip_price_too_high"})
        return

    resolved = executor.resolve_outcome_to_token_id(decision["market"], decision["outcome"])
    if not resolved:
        log.warning(f"SKIP BUY: outcome non résolu '{decision['market'][:40]}' / '{decision['outcome']}'")
        write_jsonl("trades.jsonl", {**decision, "local_action": "skip_resolve_failed"})
        return

    result = executor.place_buy(
        token_id=resolved["token_id"],
        size_usd=config.FIXED_SIZE_USD,
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
    write_jsonl("trades.jsonl", {**decision, "local_action": "sell",
                                  "fraction": fraction, "exec_result": result})


def cycle(meta: dict, positions: dict) -> None:
    decisions = fetch_local_decisions(since_ts=meta["last_seen_ts"])
    if not decisions:
        return
    relevant = filter_relevant(decisions)
    log.info(f"Cycle: {len(decisions)} new, {len(relevant)} surfandturf executed")
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


def main() -> None:
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)
    config.validate()
    log.info(f"Boot — target={config.TARGET_WALLET}, size=${config.FIXED_SIZE_USD}, "
             f"max_pos={config.MAX_POSITIONS}, kill_eq=${config.KILL_EQUITY_USD}, "
             f"dry_run={config.DRY_RUN}")
    log.info(f"Decisions source: {config.DECISIONS_PATH}")

    meta = state.load_meta()
    positions = state.load_positions()
    log.info(f"State: last_seen_ts={meta['last_seen_ts']}, {len(positions)} positions")

    try:
        clob_bal = executor.get_clob_balance_usd()
        log.info(f"CLOB balance: ${clob_bal:.4f}")
        if clob_bal < config.KILL_EQUITY_USD:
            log.warning(f"Solde sous kill_eq (${clob_bal:.2f}) — pause préventive")
    except Exception as e:
        log.error(f"Boot: get_clob_balance échec: {type(e).__name__}: {e}")

    while _running:
        try:
            cycle(meta, positions)
        except Exception as e:
            log.error(f"Cycle exception: {type(e).__name__}: {e}")
        time.sleep(config.POLL_INTERVAL_SEC)

    log.info("Stopped")


if __name__ == "__main__":
    main()
