"""Overview aggregator for all bots — Bot Z, Shadow, CopyTrade, Freqtrade.

Read-only. Reads JSON/JSONL state files for bots living under this repo,
and calls the Freqtrade REST API (Docker container, basic auth from env).

Used by the `/api/overview` Flask route to power the dashboard's at-a-glance tab.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.request
from base64 import b64encode
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _systemctl_active(service: str) -> bool:
    """True iff `systemctl is-active <service>` prints "active". 3s timeout."""
    try:
        out = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip() == "active"
    except Exception:
        return False


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _read_last_jsonl(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        with open(path) as f:
            lines = f.readlines()
        if not lines:
            return default
        return json.loads(lines[-1])
    except Exception:
        return default


def _read_tail_jsonl(path: Path, n: int = 30) -> list[dict]:
    """Return last n parsed lines from a JSONL file (or []) for sparklines."""
    if not path.exists():
        return []
    try:
        with open(path) as f:
            lines = f.readlines()[-n:]
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
        return out
    except Exception:
        return []


# ─── Bot Z (multi_runner OMEGA) ──────────────────────────────────────────

def _bot_z(base: Path) -> dict:
    s = _read_json(base / "logs/bot_z/state.json", {})
    b = _read_json(base / "logs/bot_z/budget.json", {})
    budget = b.get("budget", {}) if isinstance(b, dict) else {}

    capital = float(s.get("z_capital") or s.get("total_simulated_eur") or 0)
    if capital == 0 and budget:
        capital = float(sum(budget.values()))

    positions = s.get("last_positions") or {}
    if isinstance(positions, dict):
        n_positions = sum(
            len(v) if isinstance(v, (list, dict)) else 0
            for v in positions.values()
        )
    else:
        n_positions = 0

    engine = s.get("current_engine") or "?"
    days = int(s.get("days_running") or 0)

    history = s.get("z_capital_history") or []
    if isinstance(history, list):
        sparkline = [float(x) for x in history[-30:] if isinstance(x, (int, float))]
    else:
        sparkline = []

    return {
        "id": "bot",
        "name": "Bot Z",
        "service": "bot.service",
        "active": _systemctl_active("bot"),
        "capital_usd": capital,
        "open_positions": n_positions,
        "pnl_total_pct": float(s.get("perf_pct") or 0),
        "tab": "portfolio",
        "details": f"OMEGA · {engine} · {len(budget)} sub-bots · {days}j paper",
        "sparkline": sparkline,
        "subtitle": f"Multi-bot Alpaca paper",
    }


# ─── Shadow Bot (single-engine on Alpaca paper compte #2) ────────────────

def _shadow(base: Path) -> dict:
    equity_path = base / "logs/shadow/equity.jsonl"
    has_data = equity_path.exists()
    eq = _read_last_jsonl(equity_path, {})
    n_pos = int(eq.get("n_positions") or 0)
    equity = float(eq.get("equity") or 0)
    initial = float(os.getenv("SHADOW_INITIAL", "100000"))
    if has_data and equity > 0:
        pnl_pct = float(eq.get("perf_pct") or ((equity - initial) / initial * 100))
    else:
        pnl_pct = 0.0

    tail = _read_tail_jsonl(equity_path, 30)
    sparkline = [float(p.get("equity", 0)) for p in tail if p.get("equity")]

    return {
        "id": "shadow",
        "name": "Shadow",
        "service": "shadow.service",
        "active": _systemctl_active("shadow"),
        "capital_usd": equity,
        "open_positions": n_pos,
        "pnl_total_pct": pnl_pct,
        "tab": "strategies",
        "details": "Single-engine top-N · Alpaca paper #2" if has_data else "Shadow — no data yet",
        "subtitle": "Alpaca paper sim",
        "last_activity": eq.get("ts"),
        "sparkline": sparkline,
    }


# ─── Bot CopyTrade (Polymarket paper) ────────────────────────────────────

def _copytrade(base: Path) -> dict:
    pf_path = base / "logs/copytrade/portfolio.json"
    has_data = pf_path.exists()
    pf = _read_json(pf_path, {})
    eq = _read_last_jsonl(base / "logs/copytrade/equity.jsonl", {})

    if isinstance(pf, dict) and pf:
        total_positions = sum(len(p.get("positions", [])) for p in pf.values())
        total_realized = sum(float(p.get("realized_pnl_usd", 0)) for p in pf.values())
        mtm = sum(
            float(p.get("cash_usd", 0)) + sum(
                float(pos.get("size", 0)) * float(pos.get("avg_price", 0))
                for pos in p.get("positions", [])
            )
            for p in pf.values()
        )
    else:
        total_positions = 0
        total_realized = 0.0
        mtm = float(eq.get("total_eq") or 0)

    initial = float(os.getenv("BOT_CP_CAPITAL_USD", "1000.0"))
    if has_data and mtm > 0:
        pnl_pct = (mtm - initial) / initial * 100 if initial else 0
    else:
        pnl_pct = 0.0

    tail = _read_tail_jsonl(base / "logs/copytrade/equity.jsonl", 30)
    sparkline = [float(p.get("total_eq", 0)) for p in tail if p.get("total_eq") is not None]

    return {
        "id": "bot-cp",
        "name": "CopyTrade",
        "service": "bot-cp.service",
        "active": _systemctl_active("bot-cp"),
        "capital_usd": mtm,
        "open_positions": total_positions,
        "pnl_total_pct": pnl_pct,
        "realized_pnl_usd": total_realized,
        "tab": "copytrade",
        "details": (
            f"Polymarket · {len(pf) if isinstance(pf, dict) else 0} wallets · {total_positions} pos"
            if has_data else "Bot CopyTrade — no data yet"
        ),
        "subtitle": "Polymarket paper mirror",
        "last_activity": eq.get("date") if isinstance(eq, dict) else None,
        "sparkline": sparkline,
    }


# ─── Freqtrade (Docker container, REST API) ──────────────────────────────

def _freqtrade() -> dict:
    base_url = os.getenv("FREQTRADE_API_URL", "http://localhost:8080")
    user = os.getenv("FREQTRADE_API_USER")
    pwd = os.getenv("FREQTRADE_API_PASS")

    result = {
        "id": "freqtrade",
        "name": "Freqtrade",
        "service": "docker:freqtrade-bot",
        "active": False,
        "capital_usd": 0,
        "open_positions": 0,
        "pnl_total_pct": 0,
        "tab": None,
        "details": "Freqtrade",
    }

    if not user or not pwd:
        result["details"] = "Freqtrade — creds non configurés"
        return result

    auth = b64encode(f"{user}:{pwd}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}

    def _call(path: str) -> Any:
        req = urllib.request.Request(f"{base_url}{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read())

    try:
        cfg = _call("/api/v1/show_config")
    except Exception as e:
        log.warning("freqtrade show_config failed: %s", e)
        return result

    result["active"] = cfg.get("state") == "running"
    strategy = cfg.get("strategy", "?")
    exchange = cfg.get("exchange", "?")
    dry = cfg.get("dry_run", True)
    stake = cfg.get("stake_currency", "?")
    result["details"] = f"{strategy} · {exchange} · {'paper' if dry else 'LIVE'} {stake}"
    result["subtitle"] = f"{strategy[:25]}"
    result["stake_currency"] = stake
    result["name"] = "Freqtrade"

    try:
        bal = _call("/api/v1/balance")
        result["capital_usd"] = float(bal.get("total", 0))
    except Exception as e:
        log.warning("freqtrade balance failed: %s", e)

    try:
        st = _call("/api/v1/status")
        result["open_positions"] = len(st) if isinstance(st, list) else 0
    except Exception as e:
        log.warning("freqtrade status failed: %s", e)

    try:
        pr = _call("/api/v1/profit")
        result["pnl_total_pct"] = float(pr.get("profit_all_percent") or 0)
        result["trade_count"] = pr.get("trade_count", 0)
        latest = pr.get("latest_trade_date") or pr.get("first_trade_date")
        if latest:
            result["last_activity"] = latest
    except Exception as e:
        log.warning("freqtrade profit failed: %s", e)

    # Daily equity history for sparkline
    try:
        daily = _call("/api/v1/daily?timescale=30")
        data = daily.get("data") if isinstance(daily, dict) else None
        if isinstance(data, list) and data:
            # Each entry has rel_profit (cumulative pct). Reconstruct equity ≈ capital * (1+rel).
            cap = result.get("capital_usd", 0) or 1.0
            # data is ordered newest-first → reverse for chronological sparkline
            spark = []
            for d in reversed(data):
                rel = float(d.get("rel_profit", 0) or 0)
                spark.append(cap * (1 + rel))
            result["sparkline"] = spark
    except Exception as e:
        log.warning("freqtrade daily failed: %s", e)

    return result


# ─── Top-level ───────────────────────────────────────────────────────────

def build_overview(base_dir: str | None = None) -> dict:
    """Aggregate status of all 4 bots into a single dict for the dashboard."""
    base = Path(base_dir or os.getenv("BOT_BASE_DIR", "."))
    return {"bots": [_bot_z(base), _shadow(base), _copytrade(base), _freqtrade()]}
