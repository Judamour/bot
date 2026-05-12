"""Tests for shadow/risk_guard.py — MaxDD halt and cooldown tracking."""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from shadow.risk_guard import RiskGuard


@pytest.fixture
def tmp_state_path(tmp_path):
    """Provides a temp path for risk_state.json."""
    return str(tmp_path / "risk_state.json")


@pytest.fixture
def t0():
    """Fixed reference timestamp for deterministic tests."""
    return datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)


def test_fresh_state_initialized(tmp_state_path, t0):
    """First load creates fresh state with peak=current equity."""
    rg = RiskGuard.load(tmp_state_path, initial_equity=100_000.0, now=t0)
    assert rg.peak_equity == 100_000.0
    assert rg.halt_until is None
    assert rg.is_halted(now=t0) is False
    assert rg.cooldowns == {}


def test_persists_and_reloads(tmp_state_path, t0):
    """State written to disk survives reload — including peak_equity."""
    rg = RiskGuard.load(tmp_state_path, initial_equity=100_000.0, now=t0)
    rg.register_stop("NVDA", pnl=-500.0, now=t0)
    rg.save()

    # Reload with a DIFFERENT initial_equity — the saved peak must win.
    rg2 = RiskGuard.load(tmp_state_path, initial_equity=99_500.0, now=t0)
    assert rg2.peak_equity == 100_000.0   # persisted, not the new initial_equity
    assert "NVDA" in rg2.cooldowns
    assert rg2.is_in_cooldown("NVDA", now=t0) is True


def test_equity_update_tracks_peak(tmp_state_path, t0):
    """Higher equity raises peak. Lower equity does NOT lower peak."""
    rg = RiskGuard.load(tmp_state_path, initial_equity=100_000.0, now=t0)
    rg.update_equity(105_000.0, now=t0)
    assert rg.peak_equity == 105_000.0
    rg.update_equity(102_000.0, now=t0)
    assert rg.peak_equity == 105_000.0


def test_max_dd_15pct_triggers_halt(tmp_state_path, t0):
    """Equity dropping > 15% below peak triggers 7-day halt."""
    rg = RiskGuard.load(tmp_state_path, initial_equity=100_000.0, now=t0)
    rg.update_equity(100_000.0, now=t0)            # peak = 100k
    rg.update_equity(84_900.0, now=t0)             # DD = -15.1% → halt
    assert rg.is_halted(now=t0) is True
    expected_end = t0 + timedelta(days=7)
    assert rg.halt_until == expected_end


def test_max_dd_just_above_threshold_no_halt(tmp_state_path, t0):
    """Equity at -14.9% from peak does NOT trigger halt (strict <)."""
    rg = RiskGuard.load(tmp_state_path, initial_equity=100_000.0, now=t0)
    rg.update_equity(100_000.0, now=t0)
    rg.update_equity(85_100.0, now=t0)             # DD = -14.9%
    assert rg.is_halted(now=t0) is False


def test_halt_expires_after_7_days(tmp_state_path, t0):
    """is_halted returns False once halt_until has passed (pure query, no mutation)."""
    rg = RiskGuard.load(tmp_state_path, initial_equity=100_000.0, now=t0)
    rg.update_equity(100_000.0, now=t0)
    rg.update_equity(80_000.0, now=t0)             # halt triggered
    assert rg.is_halted(now=t0) is True
    later = t0 + timedelta(days=8)
    assert rg.is_halted(now=later) is False
    # Verify is_halted did NOT mutate halt_until (pure query)
    assert rg.halt_until is not None
    # Pruning happens explicitly when caller invokes prune_expired
    rg.prune_expired(now=later)
    assert rg.halt_until is None


def test_cooldown_pruning_is_explicit(tmp_state_path, t0):
    """is_in_cooldown is a pure query. Pruning must be explicit."""
    rg = RiskGuard.load(tmp_state_path, initial_equity=100_000.0, now=t0)
    rg.register_stop("NVDA", pnl=-200.0, now=t0)
    later = t0 + timedelta(days=6)
    assert rg.is_in_cooldown("NVDA", now=later) is False  # expired
    assert "NVDA" in rg.cooldowns                          # but still in dict
    rg.prune_expired(now=later)
    assert "NVDA" not in rg.cooldowns                      # now cleaned


def test_cooldown_expires_after_5_days(tmp_state_path, t0):
    """Cooldown lifts after COOLDOWN_DAYS."""
    rg = RiskGuard.load(tmp_state_path, initial_equity=100_000.0, now=t0)
    rg.register_stop("NVDA", pnl=-300.0, now=t0)
    assert rg.is_in_cooldown("NVDA", now=t0) is True
    after_4d = t0 + timedelta(days=4)
    assert rg.is_in_cooldown("NVDA", now=after_4d) is True
    after_6d = t0 + timedelta(days=6)
    assert rg.is_in_cooldown("NVDA", now=after_6d) is False


def test_stop_events_capped_at_10(tmp_state_path, t0):
    """Only the last 10 stop events are retained for audit."""
    rg = RiskGuard.load(tmp_state_path, initial_equity=100_000.0, now=t0)
    for i in range(15):
        rg.register_stop(f"SYM{i}", pnl=-100.0, now=t0 + timedelta(hours=i))
    assert len(rg.stop_events) == 10
    # The last 10 should be SYM5 .. SYM14
    syms = [e["sym"] for e in rg.stop_events]
    assert syms == [f"SYM{i}" for i in range(5, 15)]


def test_corrupt_state_resets_safely(tmp_state_path, t0):
    """If state file is unreadable JSON, load creates fresh state."""
    with open(tmp_state_path, "w") as f:
        f.write("{ not valid json ")
    rg = RiskGuard.load(tmp_state_path, initial_equity=100_000.0, now=t0)
    assert rg.peak_equity == 100_000.0
    assert rg.halt_until is None


def test_save_uses_temp_file_and_renames(tmp_state_path, t0, monkeypatch):
    """save() must call os.replace (atomic rename), NOT just write directly."""
    rg = RiskGuard.load(tmp_state_path, initial_equity=100_000.0, now=t0)
    calls = []
    real_replace = os.replace

    def spy_replace(src, dst):
        calls.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr("os.replace", spy_replace)
    rg.save()
    # Verify the atomic-rename code path was taken
    assert len(calls) == 1
    src, dst = calls[0]
    assert src.endswith(".tmp")
    assert dst == tmp_state_path
    # Verify no .tmp leftover after success
    assert not os.path.exists(tmp_state_path + ".tmp")
    # File is valid JSON
    with open(tmp_state_path) as f:
        data = json.load(f)
    assert "peak_equity" in data
