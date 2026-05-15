"""State persistence — atomic write, schema, reload."""
import json
import os

import pytest

from live.copytrade import state as state_mod


def test_load_state_missing_returns_default(tmp_path):
    s = state_mod.load_state(str(tmp_path / "state.json"))
    assert s == {"last_seen_ts": {}}


def test_save_state_then_load(tmp_path):
    p = str(tmp_path / "state.json")
    state_mod.save_state(p, {"last_seen_ts": {"0xW": 12345}})
    out = state_mod.load_state(p)
    assert out == {"last_seen_ts": {"0xW": 12345}}


def test_save_state_atomic(tmp_path):
    """save_state writes via tmp + rename — a kill mid-write must not corrupt."""
    p = str(tmp_path / "state.json")
    state_mod.save_state(p, {"last_seen_ts": {"0xA": 1}})
    # No .tmp leftover after success
    assert not os.path.exists(p + ".tmp")
    with open(p) as f:
        assert json.load(f)["last_seen_ts"] == {"0xA": 1}


def test_load_portfolio_missing_returns_empty(tmp_path):
    out = state_mod.load_portfolio(str(tmp_path / "portfolio.json"))
    assert out == {}


def test_save_load_portfolio(tmp_path):
    p = str(tmp_path / "portfolio.json")
    body = {
        "RN1": {"wallet": "RN1", "cash_usd": 333.33, "positions": [], "realized_pnl_usd": 0.0}
    }
    state_mod.save_portfolio(p, body)
    out = state_mod.load_portfolio(p)
    assert out == body


def test_corrupt_state_falls_back_to_default(tmp_path):
    p = str(tmp_path / "state.json")
    with open(p, "w") as f:
        f.write("{not valid json")
    s = state_mod.load_state(p)
    assert s == {"last_seen_ts": {}}


def test_append_decision_creates_jsonl(tmp_path):
    p = str(tmp_path / "decisions.jsonl")
    state_mod.append_decision(p, {"ts": 1, "wallet": "RN1", "action": "executed"})
    state_mod.append_decision(p, {"ts": 2, "wallet": "RN1", "action": "skipped"})
    with open(p) as f:
        lines = f.readlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["ts"] == 1
    assert json.loads(lines[1])["action"] == "skipped"


def test_append_equity_creates_jsonl(tmp_path):
    p = str(tmp_path / "equity.jsonl")
    state_mod.append_equity(p, {"ts": 1, "total_eq": 1000.0})
    with open(p) as f:
        line = f.readline()
    assert json.loads(line)["total_eq"] == 1000.0
