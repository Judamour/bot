# Shadow Bot v2 Concentrated High-Conviction — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the shadow bot from +3.5% CAGR (3y backtest) to ≥+30% CAGR (target +43%+ to beat Bot Z v2 QualityScore), via 5 levers: 4h backtest timeframe alignment, top-3 score-weighted sizing, hard mechanical quality gate (no LLM), regime SHIELD hard cutoff, MaxDD halt circuit breaker.

**Architecture:** Single-engine scan-all retained (shadow's identity), but augmented with 3 new pure-Python modules (`regime.py`, `quality_gate.py`, `risk_guard.py`) + a constants module (`constants_v2.py`). Modules are independently testable via pytest. The runner (prod) and backtest both consume the same modules to guarantee backtest↔prod parity.

**Tech Stack:** Python 3.12, pandas, numpy, ccxt (via `data.fetcher`), Alpaca paper REST API (via `shadow.broker`), pytest (new dependency).

**Spec reference:** `docs/superpowers/specs/2026-05-12-shadow-concentrated-high-conviction-design.md`

---

## File Structure

**New files (create):**
- `shadow/constants_v2.py` — Centralized tunables consumed by runner + backtest
- `shadow/regime.py` — `shield_active(macro)` pure function
- `shadow/risk_guard.py` — Stateful tracker (MaxDD halt + cooldowns), persists to `logs/shadow/risk_state.json`
- `shadow/quality_gate.py` — 4 hard gates, pure function `passes(sig, risk_guard, now)`
- `tests/__init__.py` — Empty (marks tests dir as Python package)
- `tests/shadow/__init__.py` — Empty
- `tests/shadow/test_regime.py` — Unit tests for regime
- `tests/shadow/test_risk_guard.py` — Unit tests for risk_guard
- `tests/shadow/test_quality_gate.py` — Unit tests for quality_gate
- `tests/shadow/test_sizing.py` — Unit test for score-weighted sizing helper

**Modified files:**
- `requirements.txt` — Add `pytest>=8.0.0`
- `backtest/run_shadow.py` — Switch to 4h via `data.fetcher.fetch_ohlcv`, TOP_N=3, score-weighted sizing, hook gates/regime/risk_guard, adaptive trailing
- `shadow/runner.py` — Same hooks + stop-trigger detection via meta diff + score-weighted sizing + adaptive trailing

**State file (runtime, gitignored):**
- `logs/shadow/risk_state.json` — Created on first cycle by risk_guard

---

## Chunk 1: Foundation modules + tests

### Task 1: Bootstrap pytest infrastructure

**Files:**
- Modify: `requirements.txt`
- Create: `tests/__init__.py`
- Create: `tests/shadow/__init__.py`
- Create: `pytest.ini`

- [ ] **Step 1: Add pytest to requirements.txt**

Modify `requirements.txt`, add the line:
```
pytest>=8.0.0
```

- [ ] **Step 2a: Verify venv exists**

Run:
```bash
cd "/home/damoria/Developpement REACT/bot trading"
ls venv/bin/python3 && ls venv/bin/pip
```
Expected: both paths exist. If not, abort and ask the user where Python lives (system Python, `.venv/`, conda env, etc.).

- [ ] **Step 2b: Install pytest in the venv**

Quote the version specifier so the shell doesn't interpret `>` as redirection:
```bash
venv/bin/pip install 'pytest>=8.0.0'
```
Expected output: `Successfully installed pytest-X.X.X ...`

- [ ] **Step 3: Create empty `tests/__init__.py` and `tests/shadow/__init__.py`**

Both files have empty content. They mark the directories as Python packages so pytest discovery works cleanly.

- [ ] **Step 4: Create `pytest.ini` at repo root**

Content:
```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
filterwarnings =
    ignore::DeprecationWarning
    ignore::PendingDeprecationWarning
```

- [ ] **Step 5: Sanity-check pytest discovery**

Run:
```bash
venv/bin/pytest --collect-only -q
```
Expected: `no tests ran` (no test files yet, but no errors).

- [ ] **Step 6: Commit**

```bash
git add requirements.txt pytest.ini tests/__init__.py tests/shadow/__init__.py
git commit -m "test(shadow): bootstrap pytest infrastructure for shadow v2 tests"
```

---

### Task 1.5: Audit detector rationale keys (spec section 3.4 prerequisite)

The quality gate G2/G3 read `sig.rationale["mtf_aligned"]` and `sig.rationale["volume_ratio"]` for all 5 detectors. If a detector doesn't populate these keys, the gate fails-closed (defensive default), which may silently kill valid signals. Spec section 3.4 mandates explicit handling.

**Files:**
- Read only: `shadow/strategies.py`

- [ ] **Step 1: Grep both keys across all 5 detectors**

Run:
```bash
cd "/home/damoria/Developpement REACT/bot trading"
grep -n "mtf_aligned\|volume_ratio" shadow/strategies.py
```
Expected: each of `detect_supertrend`, `detect_donchian`, `detect_mean_reversion`, `detect_momentum`, `detect_trend_multi_asset` populates BOTH keys in their `Signal(... rationale={...})` return.

- [ ] **Step 2: If a detector is missing a key, decide explicitly**

If a key is missing for a detector:
- **Option A (auto-pass)**: hardcode `"mtf_aligned": True` for that detector in `shadow/strategies.py` (e.g. mean_reversion already does this — it pre-checks 1d alignment so MTF is implicitly true). Add a code comment explaining why.
- **Option B (auto-fail)**: leave missing → the gate fails it (current default) → that detector's signals never enter top-3.

Pick the option per-detector based on whether MTF/volume meaningfully apply. Commit the change as `fix(shadow): expose rationale keys X for detector Y so quality gate G2/G3 can evaluate`.

At time of writing (verified by the planner), all 5 detectors populate both keys. This step is a safety net in case spec or detectors evolve.

- [ ] **Step 3: No commit needed if all 5 detectors already populate the keys**

If grep in Step 1 confirms all 5 detectors populate both keys, mark this task done and proceed.

---

### Task 2: Centralized tunables module

**Files:**
- Create: `shadow/constants_v2.py`

- [ ] **Step 1: Create `shadow/constants_v2.py` with the tunables block**

Full content:
```python
"""Centralized tunable constants for shadow v2 (concentrated high-conviction).

Imported by BOTH:
  - shadow/runner.py (prod live cycle on Alpaca paper)
  - backtest/run_shadow.py (3y historical replay)

This single source of truth guarantees backtest ↔ prod parity.
"""
from __future__ import annotations

# ── Quality gate ─────────────────────────────────────────────────────────────
SCORE_FLOOR = 65              # G1: signal must score ≥ this
COOLDOWN_DAYS = 5             # G4: forbid re-entry on a symbol N days after a stop

# ── Concentration / sizing ───────────────────────────────────────────────────
TOP_N_SIGNALS = 3             # number of candidates considered per cycle (top by score)
MAX_OPEN_POSITIONS = 10       # hard cap on concurrent positions across cycles
WEIGHT_BY_RANK = [0.30, 0.20, 0.15]  # % of available cash by rank in the cycle's top-3
                                      # remainder ≥35% stays as cash buffer
RISK_PARITY_PCT_FALLBACK = 0.01      # fallback if score-weighted sizing not applicable

# ── Trailing stop adaptatif ──────────────────────────────────────────────────
ATR_MULT_STOP_INIT = 1.5      # initial stop = entry - 1.5 × ATR(14)
ATR_MULT_TRAIL = 3.0          # trailing widens to 3.0 × ATR once position is up > +5%
PROFIT_LOOSEN_PCT = 0.05      # threshold to switch from tight → loose trailing

# ── Régime SHIELD ────────────────────────────────────────────────────────────
VIX_SHIELD_THRESHOLD = 30.0   # VIX > this → SHIELD active (no new entries)

# ── Risk guard (MaxDD halt) ──────────────────────────────────────────────────
HALT_DD_PCT = -0.15           # rolling DD ≤ this → halt new entries
HALT_DURATION_DAYS = 7        # halt lasts this many days after triggering
```

- [ ] **Step 2: Verify the module imports clean**

Run:
```bash
venv/bin/python3 -c "from shadow.constants_v2 import SCORE_FLOOR, WEIGHT_BY_RANK, HALT_DD_PCT; print('OK', SCORE_FLOOR, sum(WEIGHT_BY_RANK), HALT_DD_PCT)"
```
Expected output: `OK 65 0.65 -0.15`

- [ ] **Step 3: Commit**

```bash
git add shadow/constants_v2.py
git commit -m "feat(shadow): centralized tunables for v2 (constants_v2.py)"
```

---

### Task 3: regime.py with TDD

**Files:**
- Create: `shadow/regime.py`
- Create: `tests/shadow/test_regime.py`

- [ ] **Step 1: Write the failing tests first**

Create `tests/shadow/test_regime.py`:
```python
"""Tests for shadow/regime.py — SHIELD truth table."""
from shadow.regime import shield_active


def test_normal_market_no_shield():
    """VIX=18, BTC bull, QQQ ok → no SHIELD."""
    macro = {"vix": 18.0, "btc_trend": "bull", "qqq_regime_ok": True}
    assert shield_active(macro) is False


def test_high_vix_triggers_shield():
    """VIX > 30 → SHIELD regardless of other signals."""
    macro = {"vix": 31.0, "btc_trend": "bull", "qqq_regime_ok": True}
    assert shield_active(macro) is True


def test_vix_at_threshold_no_shield():
    """VIX = 30 exactly → no SHIELD (strict >)."""
    macro = {"vix": 30.0, "btc_trend": "bull", "qqq_regime_ok": True}
    assert shield_active(macro) is False


def test_btc_bear_and_qqq_bad_triggers_shield():
    """BTC bear AND QQQ < SMA200 → SHIELD."""
    macro = {"vix": 18.0, "btc_trend": "bear", "qqq_regime_ok": False}
    assert shield_active(macro) is True


def test_btc_bear_alone_no_shield():
    """BTC bear but QQQ ok → no SHIELD (both required)."""
    macro = {"vix": 18.0, "btc_trend": "bear", "qqq_regime_ok": True}
    assert shield_active(macro) is False


def test_qqq_bad_alone_no_shield():
    """QQQ bad but BTC bull → no SHIELD (both required)."""
    macro = {"vix": 18.0, "btc_trend": "bull", "qqq_regime_ok": False}
    assert shield_active(macro) is False


def test_missing_keys_safe_defaults():
    """Missing macro keys → assume neutre (no SHIELD)."""
    assert shield_active({}) is False
    assert shield_active({"vix": 18}) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
venv/bin/pytest tests/shadow/test_regime.py -v
```
Expected: All 7 tests FAIL with `ModuleNotFoundError: No module named 'shadow.regime'`.

- [ ] **Step 3: Implement `shadow/regime.py`**

Full content:
```python
"""Regime detection for shadow v2 — hard SHIELD cutoff.

shield_active(macro) returns True when the market is too risky for new entries.
This is a HARD GATE at cycle level: when True, the runner skips the scan/entry
phase and only manages existing positions.
"""
from __future__ import annotations
from shadow.constants_v2 import VIX_SHIELD_THRESHOLD


def shield_active(macro: dict) -> bool:
    """Return True if SHIELD should suppress new entries this cycle.

    Conditions (OR-combined):
      1. VIX strictly above VIX_SHIELD_THRESHOLD (default 30)
      2. BTC trend is bear AND QQQ is below its 200-day SMA

    Missing keys default to neutral values (vix=18, btc=bull, qqq_ok=True) so
    incomplete macro snapshots do NOT trigger SHIELD by mistake.
    """
    vix = macro.get("vix", 18.0)
    btc_trend = macro.get("btc_trend", "bull")
    qqq_ok = macro.get("qqq_regime_ok", True)

    if vix > VIX_SHIELD_THRESHOLD:
        return True
    if btc_trend == "bear" and not qqq_ok:
        return True
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
venv/bin/pytest tests/shadow/test_regime.py -v
```
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add shadow/regime.py tests/shadow/test_regime.py
git commit -m "feat(shadow): regime SHIELD detector with 7 unit tests"
```

---

### Task 4: risk_guard.py with TDD

This module is the most stateful — it persists to disk and tracks time-based windows. Tests use a fake `now` parameter (dependency injection) instead of mocking `datetime.now()`.

**Files:**
- Create: `shadow/risk_guard.py`
- Create: `tests/shadow/test_risk_guard.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/shadow/test_risk_guard.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
venv/bin/pytest tests/shadow/test_risk_guard.py -v
```
Expected: All 10 tests FAIL (module missing).

- [ ] **Step 3: Implement `shadow/risk_guard.py`**

Full content:
```python
"""Risk guard for shadow v2 — MaxDD halt + per-symbol cooldown tracking.

Persists state to logs/shadow/risk_state.json (gitignored).
Atomic write via temp file + rename.

Time is injected via `now` parameter for testability — never reads datetime.now()
internally. Callers pass datetime.now(timezone.utc) at runtime.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from shadow.constants_v2 import (
    COOLDOWN_DAYS,
    HALT_DD_PCT,
    HALT_DURATION_DAYS,
)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    return datetime.fromisoformat(s)


@dataclass
class RiskGuard:
    """Stateful tracker. Use `RiskGuard.load(path, ...)` to instantiate."""

    state_path: str
    peak_equity: float
    peak_date: datetime
    halt_until: Optional[datetime] = None
    cooldowns: dict[str, datetime] = field(default_factory=dict)
    stop_events: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, state_path: str, initial_equity: float, now: datetime) -> "RiskGuard":
        """Load from disk, or initialize fresh if file is missing/corrupt."""
        if os.path.exists(state_path):
            try:
                with open(state_path) as f:
                    data = json.load(f)
                return cls(
                    state_path=state_path,
                    peak_equity=float(data.get("peak_equity", initial_equity)),
                    peak_date=_parse_iso(data.get("peak_date")) or now,
                    halt_until=_parse_iso(data.get("halt_until")),
                    cooldowns={k: _parse_iso(v) for k, v in (data.get("cooldowns") or {}).items()},
                    stop_events=list(data.get("stop_events") or []),
                )
            except (json.JSONDecodeError, ValueError, KeyError, TypeError):
                pass  # fall through to fresh state
        return cls(
            state_path=state_path,
            peak_equity=initial_equity,
            peak_date=now,
        )

    # ── Equity / halt ─────────────────────────────────────────────────────────
    def update_equity(self, equity: float, now: datetime) -> None:
        """Track new peak, trigger halt if drawdown breaches threshold."""
        if equity > self.peak_equity:
            self.peak_equity = equity
            self.peak_date = now
        if self.peak_equity > 0:
            dd_pct = (equity - self.peak_equity) / self.peak_equity
            if dd_pct < HALT_DD_PCT and self.halt_until is None:
                self.halt_until = now + timedelta(days=HALT_DURATION_DAYS)

    def is_halted(self, now: datetime) -> bool:
        """Pure query: True iff halt is currently active. Does NOT mutate state.

        Call `prune_expired(now)` separately if you want to clear expired entries.
        """
        if self.halt_until is None:
            return False
        return now < self.halt_until

    # ── Cooldowns ─────────────────────────────────────────────────────────────
    def register_stop(self, symbol: str, pnl: float, now: datetime) -> None:
        """Record a stop-loss event and start a cooldown on the symbol."""
        self.cooldowns[symbol] = now + timedelta(days=COOLDOWN_DAYS)
        self.stop_events.append({"sym": symbol, "ts": _iso(now), "pnl": round(pnl, 2)})
        # Keep only the last 10 for audit
        if len(self.stop_events) > 10:
            self.stop_events = self.stop_events[-10:]

    def is_in_cooldown(self, symbol: str, now: datetime) -> bool:
        """Pure query: True iff symbol is in active cooldown. Does NOT mutate state."""
        end = self.cooldowns.get(symbol)
        if end is None:
            return False
        return now < end

    def prune_expired(self, now: datetime) -> None:
        """Clean up expired halt + cooldown entries. Call once per cycle (after
        all queries) to keep the persisted state compact."""
        if self.halt_until is not None and now >= self.halt_until:
            self.halt_until = None
        for sym, end in list(self.cooldowns.items()):
            if now >= end:
                del self.cooldowns[sym]

    # ── Persistence ───────────────────────────────────────────────────────────
    def save(self) -> None:
        """Atomic write via temp file + rename."""
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        data = {
            "peak_equity": self.peak_equity,
            "peak_date": _iso(self.peak_date),
            "halt_until": _iso(self.halt_until),
            "cooldowns": {k: _iso(v) for k, v in self.cooldowns.items()},
            "stop_events": self.stop_events,
        }
        tmp_path = self.state_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, self.state_path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
venv/bin/pytest tests/shadow/test_risk_guard.py -v
```
Expected: All 10 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add shadow/risk_guard.py tests/shadow/test_risk_guard.py
git commit -m "feat(shadow): risk_guard with MaxDD halt + 5d cooldowns (10 unit tests)"
```

---

### Task 5: quality_gate.py with TDD

**Files:**
- Create: `shadow/quality_gate.py`
- Create: `tests/shadow/test_quality_gate.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/shadow/test_quality_gate.py`:
```python
"""Tests for shadow/quality_gate.py — 4 hard gates."""
from datetime import datetime, timezone

import pytest

from shadow.scorer import Signal
from shadow.quality_gate import passes
from shadow.risk_guard import RiskGuard


@pytest.fixture
def t0():
    return datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def rg(tmp_path, t0):
    return RiskGuard.load(str(tmp_path / "rs.json"), initial_equity=100_000.0, now=t0)


def _sig(score=80, mtf=True, vol=1.5, symbol="NVDA"):
    return Signal(
        symbol=symbol,
        strategy="supertrend",
        side="long",
        entry_price=100.0,
        atr=2.0,
        stop_price=97.0,
        rationale={"mtf_aligned": mtf, "volume_ratio": vol, "adx": 30, "rsi": 55},
        score=score,
    )


def test_strong_signal_passes(rg, t0):
    """High score + MTF + volume + no cooldown → pass."""
    assert passes(_sig(), rg, now=t0) is True


def test_score_below_floor_fails(rg, t0):
    """Score < SCORE_FLOOR (65) fails G1."""
    assert passes(_sig(score=60), rg, now=t0) is False


def test_score_exactly_at_floor_passes(rg, t0):
    """Score == SCORE_FLOOR passes (≥, not strict)."""
    assert passes(_sig(score=65), rg, now=t0) is True


def test_mtf_not_aligned_fails(rg, t0):
    """mtf_aligned False fails G2."""
    assert passes(_sig(mtf=False), rg, now=t0) is False


def test_low_volume_fails(rg, t0):
    """volume_ratio < 1.0 fails G3."""
    assert passes(_sig(vol=0.8), rg, now=t0) is False


def test_volume_exactly_at_floor_passes(rg, t0):
    """volume_ratio == 1.0 passes (≥)."""
    assert passes(_sig(vol=1.0), rg, now=t0) is True


def test_symbol_in_cooldown_fails(rg, t0):
    """Cooldown on the symbol → fail G4."""
    rg.register_stop("NVDA", pnl=-200, now=t0)
    assert passes(_sig(symbol="NVDA"), rg, now=t0) is False
    # Different symbol still passes
    assert passes(_sig(symbol="GOOGL"), rg, now=t0) is True


def test_missing_rationale_keys_fail_safe(rg, t0):
    """If a detector omits mtf_aligned or volume_ratio, gate must NOT silently pass."""
    sig = Signal(
        symbol="X", strategy="momentum", side="long",
        entry_price=100, atr=2, stop_price=98,
        rationale={},  # missing both keys
        score=80,
    )
    assert passes(sig, rg, now=t0) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
venv/bin/pytest tests/shadow/test_quality_gate.py -v
```
Expected: All 8 tests FAIL (module missing).

- [ ] **Step 3: Implement `shadow/quality_gate.py`**

Full content:
```python
"""Hard quality gate for shadow v2 — 4 mechanical filters replacing LLM veto.

passes(sig, risk_guard, now) is a pure boolean: True iff the signal clears all
4 gates. Missing rationale keys default to FAIL (safer than silent pass).
"""
from __future__ import annotations
from datetime import datetime
from shadow.constants_v2 import SCORE_FLOOR
from shadow.scorer import Signal
from shadow.risk_guard import RiskGuard


def passes(sig: Signal, risk_guard: RiskGuard, now: datetime) -> bool:
    """Return True iff signal clears all 4 hard gates.

    G1 score plancher : sig.score ≥ SCORE_FLOOR
    G2 MTF alignment  : rationale["mtf_aligned"] is True
    G3 Volume réel    : rationale["volume_ratio"] ≥ 1.0
    G4 Cooldown stop  : symbol is not in active risk_guard cooldown

    Missing rationale keys → fail (defensive default).
    """
    # G1
    if sig.score < SCORE_FLOOR:
        return False
    # G2: explicit True check; missing key → fail
    if not sig.rationale.get("mtf_aligned"):
        return False
    # G3: missing key → fail
    vol = sig.rationale.get("volume_ratio")
    if vol is None or vol < 1.0:
        return False
    # G4
    if risk_guard.is_in_cooldown(sig.symbol, now=now):
        return False
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
venv/bin/pytest tests/shadow/test_quality_gate.py -v
```
Expected: All 8 tests PASS.

- [ ] **Step 5: Run the full test suite to check no regressions**

Run:
```bash
venv/bin/pytest tests/ -v
```
Expected: **26 tests PASS** (7 regime + 11 risk_guard + 8 quality_gate). The risk_guard suite has 11 tests because the design now separates query (is_halted / is_in_cooldown) from mutation (prune_expired) — `test_cooldown_pruning_is_explicit` was added.

- [ ] **Step 6: Commit**

```bash
git add shadow/quality_gate.py tests/shadow/test_quality_gate.py
git commit -m "feat(shadow): quality_gate with 4 hard gates (8 unit tests)"
```

---

## Chunk 2: Integration (backtest + runner) + validation + deploy

### Task 6: Sizing helper module + tests

Score-weighted sizing is used by both backtest and runner. Centralize it as a pure function for testability.

**Files:**
- Modify: `shadow/constants_v2.py` (already done in Task 2 — no change here)
- Create: `shadow/sizing.py`
- Create: `tests/shadow/test_sizing.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/shadow/test_sizing.py`:
```python
"""Tests for shadow/sizing.py — score-weighted position sizing."""
import pytest
from shadow.sizing import compute_size, SizeResult


def test_top_1_gets_30_pct():
    res = compute_size(rank=0, cash=100_000.0, entry_price=100.0)
    assert res.notional == pytest.approx(30_000.0)
    assert res.qty == pytest.approx(300.0)


def test_top_2_gets_20_pct():
    res = compute_size(rank=1, cash=100_000.0, entry_price=100.0)
    assert res.notional == pytest.approx(20_000.0)


def test_top_3_gets_15_pct():
    res = compute_size(rank=2, cash=100_000.0, entry_price=100.0)
    assert res.notional == pytest.approx(15_000.0)


def test_rank_out_of_range_returns_zero():
    """rank >= 3 → no position."""
    res = compute_size(rank=3, cash=100_000.0, entry_price=100.0)
    assert res.qty == 0.0


def test_zero_cash_returns_zero():
    res = compute_size(rank=0, cash=0.0, entry_price=100.0)
    assert res.qty == 0.0


def test_zero_entry_price_returns_zero():
    """Defensive: bad price should not crash with div-by-zero."""
    res = compute_size(rank=0, cash=100_000.0, entry_price=0.0)
    assert res.qty == 0.0


def test_total_top_3_capped_at_65_pct():
    """Sum of top-3 weights = 30 + 20 + 15 = 65, leaves 35% cash buffer."""
    total = sum(compute_size(r, 100_000.0, 100.0).notional for r in range(3))
    assert total == pytest.approx(65_000.0)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
venv/bin/pytest tests/shadow/test_sizing.py -v
```
Expected: All 7 tests FAIL.

- [ ] **Step 3: Implement `shadow/sizing.py`**

Full content:
```python
"""Score-weighted sizing helper for shadow v2.

compute_size(rank, cash, entry_price) returns the qty + notional value for
position at given rank (0-indexed) in the cycle's top-3.

Pure function — no I/O, no broker calls. Suitable for backtest and live.
"""
from __future__ import annotations
from dataclasses import dataclass
from shadow.constants_v2 import WEIGHT_BY_RANK


@dataclass
class SizeResult:
    qty: float        # number of units / shares
    notional: float   # qty × entry_price (USD)


def compute_size(rank: int, cash: float, entry_price: float) -> SizeResult:
    """Return the size for the rank-th candidate of the cycle's top-N.

    Args:
        rank: 0-indexed position in the sorted top-N (0 = best score).
        cash: available cash (USD) at the moment of sizing decision.
        entry_price: signal's entry price (USD).

    Returns:
        SizeResult with qty=0.0 if rank is out of range, cash≤0, or price≤0.
    """
    if rank < 0 or rank >= len(WEIGHT_BY_RANK):
        return SizeResult(qty=0.0, notional=0.0)
    if cash <= 0 or entry_price <= 0:
        return SizeResult(qty=0.0, notional=0.0)
    weight = WEIGHT_BY_RANK[rank]
    notional = cash * weight
    qty = notional / entry_price
    return SizeResult(qty=qty, notional=notional)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
venv/bin/pytest tests/shadow/test_sizing.py -v
```
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add shadow/sizing.py tests/shadow/test_sizing.py
git commit -m "feat(shadow): score-weighted sizing helper (7 unit tests)"
```

---

### Task 6.5: Note on INITIAL_CAPITAL scale (1000 vs 100K)

The current `backtest/run_shadow.py` imports `INITIAL_CAPITAL = INITIAL` from `backtest/multi_backtest.py` where `INITIAL = 1000.0`. The Alpaca shadow paper account uses $100K. The 100x scale ratio is intentional for backtest: all percentages (CAGR, MaxDD) are scale-invariant, so a $1K backtest produces identical metrics to a $100K backtest, and runs faster on small floats.

**However**, the G4 accounting assertion `gap_pct < 1.0` uses `INITIAL_CAPITAL` as denominator. At $1K base, 1% tolerance = $10 absolute. This is intentionally tight to catch any future regression of the bug fixed in commit 7803182 today.

**Do NOT change `INITIAL = 1000`**. Keep it as the reference scale. If the assertion fires in real backtest runs due to legitimate float accumulation (>$10 gap on $1K base), the implementer must investigate the comptable trail BEFORE relaxing the tolerance.

---

### Task 7: Rewrite backtest/run_shadow.py for v2

This is the biggest change. The backtest must:
1. Fetch 4h data via `data.fetcher.fetch_ohlcv` (not yfinance daily)
2. Iterate at 4h granularity (≈6 bars/day) for ~3 years
3. Use the new modules: regime, quality_gate, risk_guard, sizing
4. Use adaptive trailing
5. Output the same JSON format as before

**Files:**
- Modify: `backtest/run_shadow.py` (full rewrite of main simulation loop)

- [ ] **Step 1: Read current `backtest/run_shadow.py` to understand what to keep**

Sections to keep:
- Header / imports / config block
- `fetch_daily` (rename + adapt to fetch 4h)
- The metrics / JSON output structure

Sections to rewrite:
- The simulation loop (lines ~97-225 in current file)

- [ ] **Step 2: Replace `fetch_daily` with `fetch_bars` using `data.fetcher`**

The new fetch uses Binance for crypto (BTC/USD → BTC/USDT internally) and Alpaca data API for stocks (with yfinance fallback). `data.fetcher.fetch_ohlcv(symbol, "4h", days)` already handles all routing.

Replace the `fetch_daily` function with:
```python
def fetch_bars(symbol_internal: str, timeframe: str, days: int) -> pd.DataFrame | None:
    """Fetch OHLCV via prod data.fetcher (Binance crypto / Alpaca-or-yf stocks)."""
    from data.fetcher import fetch_ohlcv
    try:
        df = fetch_ohlcv(symbol_internal, timeframe, days)
        if df is None or len(df) < 50:
            return None
        df.columns = [c.lower() for c in df.columns]
        return df[["open", "high", "low", "close", "volume"]].dropna()
    except Exception as e:
        print(f"  [skip] {symbol_internal} {timeframe}: {e}")
        return None
```

- [ ] **Step 3: Update the data loading section to fetch BOTH 4h and 1d caches**

In `main()`, after the imports, replace:
```python
cache = {}
for sym in ALL_SYMBOLS:
    df = fetch_daily(sym)
    ...
```
with:
```python
print("Chargement OHLCV 4h (signaux) + 1d (MTF + régime QQQ)…")
cache_4h, cache_1d = {}, {}
for sym in ALL_SYMBOLS:
    df_4h = fetch_bars(sym, "4h", days=DAYS_4H)
    df_1d = fetch_bars(sym, "1d", days=DAYS_1D)
    if df_4h is not None and df_1d is not None:
        cache_4h[sym] = df_4h
        cache_1d[sym] = df_1d
        print(f"  ✓ {sym:10}: {len(df_4h):4} bars 4h / {len(df_1d):4} bars 1d")
if not cache_4h:
    print("Aucune donnée chargée")
    return
```

And update the constants:
```python
DAYS_4H = 365 * 3 + 60          # 3 ans 4h + warmup
DAYS_1D = 365 * 3 + 220         # 3 ans + 220 jours pour SMA200
```

- [ ] **Step 4: Replace the simulation loop with the v2 logic**

First, **add the new imports at the MODULE TOP** (alongside existing imports near the top of the file, NOT inside `main()` — keeps imports unified and avoids per-iteration import overhead):

```python
from datetime import datetime, timezone
from shadow.constants_v2 import (
    SCORE_FLOOR, TOP_N_SIGNALS, MAX_OPEN_POSITIONS,
    ATR_MULT_STOP_INIT, ATR_MULT_TRAIL, PROFIT_LOOSEN_PCT,
)
from shadow.regime import shield_active
from shadow.quality_gate import passes as gate_passes
from shadow.risk_guard import RiskGuard
from shadow.sizing import compute_size
from shadow.scorer import compute_score, Signal
from shadow.strategies import ALL_DETECTORS
from strategies.supertrend import compute_atr  # already used in v1 loop, hoist to top
```

Then replace the loop body (`for i, day in enumerate(...)`) with this structure. **Full code block** to substitute:

```python

# Date timeline: intersection of all 4h indices
common_bars = sorted(set.intersection(*[set(df.index) for df in cache_4h.values()]))
print(f"\n{len(common_bars)} barres 4h communes\n")

# Warmup : need 220 1d bars for SMA200 in detectors → skip first ~330 4h bars
WARMUP_BARS_4H = 330
if len(common_bars) <= WARMUP_BARS_4H:
    print(f"Pas assez d'historique ({len(common_bars)} bars)")
    return

capital = INITIAL_CAPITAL
positions = {}                       # sym → dict with strategy, entry, size, stop, atr, entry_ts, fee_entry, score
trades = []
equity_curve = []
equity_ts = []

# Risk guard state (in-memory only for backtest — no persistence)
rg = RiskGuard(state_path="/tmp/__backtest_risk_state__.json",
               peak_equity=INITIAL_CAPITAL,
               peak_date=common_bars[WARMUP_BARS_4H])

ctx_default = {"vix": 18.0, "btc_trend": "bull", "qqq_regime_ok": True}

for i, bar_ts in enumerate(common_bars[WARMUP_BARS_4H:], start=WARMUP_BARS_4H):
    # 1. Macro context (approx via SPY trend on 1d cache)
    ctx = dict(ctx_default)
    if "SPY" in cache_1d:
        spy = cache_1d["SPY"]
        spy_slice = spy.loc[:bar_ts.normalize()] if hasattr(bar_ts, "normalize") else spy.loc[:bar_ts]
        if len(spy_slice) >= 200:
            ctx["qqq_regime_ok"] = bool(spy_slice["close"].iloc[-1] > spy_slice["close"].tail(200).mean())

    # 2. Check halt / SHIELD
    halted = rg.is_halted(now=bar_ts)
    shielded = shield_active(ctx)
    skip_new_entries = halted or shielded

    # 3. Update trailing stops + check stops for open positions
    for sym in list(positions.keys()):
        df = cache_4h[sym]
        if bar_ts not in df.index:
            continue
        bar = df.loc[bar_ts]
        pos = positions[sym]
        low, close = float(bar["low"]), float(bar["close"])
        # Stop hit?
        if low <= pos["stop"]:
            exit_price = pos["stop"] * (1 - SLIPPAGE)
            proceeds = exit_price * pos["size"]
            fee_exit = proceeds * FEE
            capital += proceeds - fee_exit
            pnl = (exit_price - pos["entry"]) * pos["size"] - pos["fee_entry"] - fee_exit
            trades.append({
                "symbol": sym, "strategy": pos["strategy"],
                "entry": pos["entry"], "exit": exit_price,
                "entry_ts": pos["entry_ts"], "exit_ts": bar_ts,
                "pnl": round(pnl, 2), "reason": "stop_loss", "score": pos["score"],
            })
            rg.register_stop(sym, pnl, now=bar_ts)
            del positions[sym]
            continue
        # Trailing update (adaptive: tight if <+5%, loose if ≥+5%)
        # compute_atr is already imported at module top (Step 4 imports block)
        df_slice = df.loc[:bar_ts]
        if len(df_slice) < 15:
            continue
        atr = float(compute_atr(df_slice["high"], df_slice["low"], df_slice["close"], 14).iloc[-1] or 0)
        if atr <= 0:
            continue
        pnl_pct = (close - pos["entry"]) / pos["entry"]
        atr_mult = ATR_MULT_TRAIL if pnl_pct >= PROFIT_LOOSEN_PCT else ATR_MULT_STOP_INIT
        new_stop = close - atr_mult * atr
        if new_stop > pos["stop"]:
            pos["stop"] = new_stop

    if skip_new_entries:
        # Equity snapshot then skip to next bar
        eq = capital + sum(float(cache_4h[s].loc[bar_ts]["close"]) * p["size"]
                           for s, p in positions.items() if bar_ts in cache_4h[s].index)
        equity_curve.append(eq)
        equity_ts.append(bar_ts)
        rg.update_equity(eq, now=bar_ts)
        continue

    # 4. Scan signals on symbols without position
    candidates = []
    for sym in ALL_SYMBOLS:
        if sym in positions or sym not in cache_4h:
            continue
        df_4h = cache_4h[sym]
        df_1d = cache_1d.get(sym)
        if bar_ts not in df_4h.index:
            continue
        df_4h_hist = df_4h.loc[:bar_ts]
        if len(df_4h_hist) < 60:
            continue
        df_1d_hist = df_1d.loc[:bar_ts.normalize()] if (df_1d is not None and hasattr(bar_ts, "normalize")) else df_1d
        if df_1d_hist is None or len(df_1d_hist) < 220:
            continue
        for detector in ALL_DETECTORS:
            try:
                sig = detector(sym, df_4h_hist, df_1d_hist)
                if sig is None:
                    continue
                sig.score = compute_score(sig, ctx)
                if sig.score >= SCORE_FLOOR:    # pre-filter at G1 already
                    candidates.append(sig)
            except Exception:
                pass

    # 5. Dédup intra-cycle by symbol, sort by score desc
    best_by_symbol: dict[str, Signal] = {}
    for sig in candidates:
        if sig.symbol not in best_by_symbol or sig.score > best_by_symbol[sig.symbol].score:
            best_by_symbol[sig.symbol] = sig
    sorted_cands = sorted(best_by_symbol.values(), key=lambda s: s.score, reverse=True)

    # 6. Filter through quality gate, take top-3, size by rank
    accepted = [s for s in sorted_cands if gate_passes(s, rg, now=bar_ts)][:TOP_N_SIGNALS]
    for rank, sig in enumerate(accepted):
        if len(positions) >= MAX_OPEN_POSITIONS:
            break
        size_res = compute_size(rank=rank, cash=capital, entry_price=sig.entry_price)
        if size_res.qty <= 0:
            continue
        entry_eff = sig.entry_price * (1 + SLIPPAGE)
        cost = entry_eff * size_res.qty
        fee = cost * FEE
        total = cost + fee
        if total > capital:
            continue
        capital -= total
        # Initial stop = entry - ATR_MULT_STOP_INIT × ATR (overrides the detector's stop_price for v2 consistency)
        stop_initial = entry_eff - ATR_MULT_STOP_INIT * sig.atr
        positions[sig.symbol] = {
            "strategy": sig.strategy, "score": sig.score,
            "entry": entry_eff, "size": size_res.qty,
            "stop": stop_initial, "atr": sig.atr,
            "entry_ts": bar_ts, "fee_entry": fee,
        }

    # 7. Equity snapshot
    eq = capital + sum(float(cache_4h[s].loc[bar_ts]["close"]) * p["size"]
                       for s, p in positions.items() if bar_ts in cache_4h[s].index)
    equity_curve.append(eq)
    equity_ts.append(bar_ts)
    rg.update_equity(eq, now=bar_ts)
```

- [ ] **Step 5: Update the "force close all à la fin" block + keep metrics output**

Replace the force-close + metrics output at the end of `main()` with:
```python
# Force close all open positions at the last bar. If a position's symbol has no
# bar at last_bar (data hole), use its last known close from earlier in cache
# instead of silently dropping it (which would corrupt G4 accounting).
last_bar = common_bars[-1]
for sym in list(positions.keys()):
    df = cache_4h[sym]
    pos = positions[sym]
    if last_bar in df.index:
        exit_price_raw = float(df.loc[last_bar]["close"])
    else:
        # Data hole at last_bar: fall back to symbol's last available close.
        if len(df) == 0:
            print(f"  [warn] {sym} has no data at end-of-backtest, recovering at entry price (zero P&L)")
            exit_price_raw = pos["entry"]
        else:
            exit_price_raw = float(df["close"].iloc[-1])
            print(f"  [warn] {sym} missing last_bar, using last available close {exit_price_raw}")
    exit_price = exit_price_raw * (1 - SLIPPAGE)
    proceeds = exit_price * pos["size"]
    fee_exit = proceeds * FEE
    capital += proceeds - fee_exit
    pnl = (exit_price - pos["entry"]) * pos["size"] - pos["fee_entry"] - fee_exit
    trades.append({
        "symbol": sym, "strategy": pos["strategy"],
        "entry": pos["entry"], "exit": exit_price,
        "entry_ts": pos["entry_ts"], "exit_ts": last_bar,
        "pnl": round(pnl, 2), "reason": "end_of_backtest", "score": pos["score"],
    })
    del positions[sym]

# G4 invariant: sum(trade_pnl) ≈ final - initial
sum_pnl = sum(t["pnl"] for t in trades)
delta_capital = capital - INITIAL_CAPITAL
accounting_gap = abs(sum_pnl - delta_capital)
gap_pct = (accounting_gap / INITIAL_CAPITAL) * 100
assert gap_pct < 1.0, (
    f"COMPTABILITÉ INCOHÉRENTE: sum(pnl)={sum_pnl:.2f} vs delta_capital={delta_capital:.2f} "
    f"écart={accounting_gap:.2f} ({gap_pct:.2f}%) — anti-régression bug 7803182"
)

from backtest.multi_backtest import compute_metrics
metrics = compute_metrics(trades, equity_curve, initial=INITIAL_CAPITAL)

# … rest of the output / JSON save unchanged (keep the existing print + json.dump block)
```

- [ ] **Step 6: Run a quick syntax check**

```bash
venv/bin/python3 -c "import ast; ast.parse(open('backtest/run_shadow.py').read()); print('OK')"
```
Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add backtest/run_shadow.py
git commit -m "feat(shadow): rewrite backtest for v2 — 4h timeframe + new modules + accounting assertion"
```

---

### Task 8: Run backtest, validate gate G1-G4

- [ ] **Step 1: Execute the backtest**

```bash
cd "/home/damoria/Developpement REACT/bot trading"
venv/bin/python3 backtest/run_shadow.py 2>&1 | tee /tmp/shadow_v2_backtest.log
```
Expected runtime: 5-12 minutes (depends on data API latency).
Expected end output: summary block with CAGR, Sharpe, MaxDD, PF, Win rate, Capital final.

- [ ] **Step 2: Verify gate G1 (CAGR ≥ 30%)**

```bash
grep "CAGR" /tmp/shadow_v2_backtest.log
```
Expected: `CAGR : XX.X %` where XX ≥ 30.

If CAGR < 30%: **STOP**. Do not proceed to deploy. Surface to user — possible adjustments:
- Loosen SCORE_FLOOR from 65 to 60 in `shadow/constants_v2.py`
- Raise WEIGHT_BY_RANK to [0.40, 0.25, 0.15]
- Try ATR_MULT_STOP_INIT 1.5 → 2.0 (give signals more breathing room)
- If all fail, fall back to alt B (leverage 1.5× on top-1) — out of v2 scope

- [ ] **Step 3: Verify gate G2 (Sharpe ≥ 1.0)**

```bash
grep "Sharpe" /tmp/shadow_v2_backtest.log
```
Expected: `Sharpe : ≥1.00`.

- [ ] **Step 4: Verify gate G3 (MaxDD better than -25%) — mechanical assertion**

```bash
venv/bin/python3 -c "
import re
log = open('/tmp/shadow_v2_backtest.log').read()
m = re.search(r'Max Drawdown\s*:\s*(-?\d+\.?\d*)', log)
assert m, 'MaxDD line not found in log'
dd = float(m.group(1))
assert dd > -25.0, f'GATE G3 FAILED: MaxDD={dd}% is not better than -25%'
print(f'G3 OK: MaxDD={dd}%')
"
```
Expected: `G3 OK: MaxDD=-X.X%`. If assertion fires: stop and revise sizing (e.g. reduce WEIGHT_BY_RANK to [0.25, 0.18, 0.12]) or trailing (e.g. tighten ATR_MULT_STOP_INIT 1.5 → 2.0).

- [ ] **Step 5: Verify gate G4 (accounting coherent)**

The assertion `gap_pct < 1.0` runs automatically. If it failed, the script would have raised `AssertionError`. Confirm no error:
```bash
grep "COMPTABILITÉ" /tmp/shadow_v2_backtest.log
```
Expected: empty output (no assertion fire).

- [ ] **Step 6: If all 4 gates pass, save the result JSON to git**

```bash
git add backtest/results/shadow_3y.json
git commit -m "test(shadow): backtest v2 results — CAGR XX% / Sharpe X.X / MaxDD -X%"
```
(Substitute the real numbers.)

---

### Task 9: Refactor shadow/runner.py for v2

The prod runner must mirror the backtest logic. The non-trivial addition vs current runner: detecting stop fills between cycles (so risk_guard.register_stop can be called).

**Files:**
- Modify: `shadow/runner.py`

- [ ] **Step 1: Add the new imports and remove the legacy local constants**

In `shadow/runner.py`, ADD these imports near the top (with the other module imports):
```python
from shadow.constants_v2 import (
    SCORE_FLOOR, TOP_N_SIGNALS, MAX_OPEN_POSITIONS,
    ATR_MULT_STOP_INIT, ATR_MULT_TRAIL, PROFIT_LOOSEN_PCT,
)
from shadow.regime import shield_active
from shadow.quality_gate import passes as gate_passes
from shadow.risk_guard import RiskGuard
from shadow.sizing import compute_size

MIN_SCORE = SCORE_FLOOR        # alias for log messages
```

Then, in the existing constants block (currently around lines 41-52), **REMOVE these specific constants** (now sourced from `constants_v2`):
- `MIN_SCORE = 55.0`
- `TOP_N_SIGNALS = 5`
- `RISK_PER_TRADE_PCT = 0.01` (replaced by score-weighted sizing)
- `MAX_POSITION_PCT = 0.10` (replaced by score-weighted sizing)
- `MAX_OPEN_POSITIONS = 10`
- `ATR_MULT_TRAIL = 4.0` (overridden to 3.0 in constants_v2)

**KEEP these constants** (still used, NOT in constants_v2):
- `LOG_DIR = "logs/shadow"`
- `DECISIONS_LOG = ...`
- `EQUITY_LOG = ...`
- `LOCAL_META = ...`
- **`CYCLE_HOURS_UTC = [3, 7, 11, 15, 19, 23]`** — consumed by `_next_cycle_dt()`, do NOT remove

Verify post-edit:
```bash
grep -n "CYCLE_HOURS_UTC\|LOG_DIR" shadow/runner.py
```
Expected: both still present.

- [ ] **Step 2: Add risk_guard load at top of `run_cycle()`**

After fetching the account (`equity` variable available) but before any decision, insert:
```python
now = datetime.now(timezone.utc)
rg = RiskGuard.load(state_path=f"{LOG_DIR}/risk_state.json",
                    initial_equity=equity, now=now)
halted = rg.is_halted(now=now)
print(f"[SHADOW] risk_guard: peak=${rg.peak_equity:.0f} halt={'YES until '+rg.halt_until.isoformat()[:19] if halted else 'no'}", flush=True)
```

- [ ] **Step 3: Add SHIELD check after macro fetch**

After the existing `print(f"[SHADOW] VIX=...")` line, add:
```python
shielded = shield_active({
    "vix": ctx["vix"], "btc_trend": ctx["btc_trend"], "qqq_regime_ok": ctx["qqq_ok"]
})
skip_new_entries = halted or shielded
print(f"[SHADOW] SHIELD={'YES' if shielded else 'no'} → skip_new_entries={skip_new_entries}", flush=True)
```

- [ ] **Step 4: Detect stop fills since last cycle**

After loading `pos_by_sym` and `meta` (around line 127-130), before trailing updates, add:
```python
# Detect stops triggered between cycles : symbols in local meta but absent from
# current Alpaca positions → stop was hit + filled by broker. Compute realized
# pnl using last close × qty - cost, register in risk_guard.
# .copy() is shallow on purpose: we iterate by key, then mutate the original
# pos_meta via .pop() below (intended).
prev_meta = pos_meta.copy()
for sym, m in prev_meta.items():
    if sym in pos_by_sym:
        continue  # still open
    # Stopped out (or manually closed). Compute approximate pnl from local meta.
    try:
        entry = float(m.get("entry_price") or 0)
        qty = float(m.get("qty") or 0)
        stop = float(m.get("stop") or 0)
        if entry > 0 and qty > 0:
            # Approximate exit price = stop (the most likely fill if stop_limit triggered)
            approx_pnl = (stop - entry) * qty
            rg.register_stop(sym, pnl=approx_pnl, now=now)
            log_event("stop_fill", {"symbol": sym, "approx_pnl": round(approx_pnl, 2),
                                    "entry": entry, "stop": stop, "qty": qty})
            print(f"[SHADOW] STOP FILL detected: {sym} pnl≈{approx_pnl:.2f}", flush=True)
    except Exception as e:
        print(f"[SHADOW] stop detection error for {sym}: {e}", flush=True)
    # Clean up orphan meta entry
    pos_meta.pop(sym, None)
```

**Bootstrap edge case**: existing pos_meta entries from BEFORE this deploy will NOT contain `qty`. The `if entry > 0 and qty > 0` guard skips them → no cooldown is registered for stops that occur during the first cycle window after deploy. This is acceptable (transient, self-healing once new entries record `qty` from Step 5 below). Add this comment to the code:
```python
# Note: positions opened before deploy lack 'qty' in meta. If they stop out
# during the first post-deploy cycle window, no cooldown is registered. This
# is intentional — accepted as a transient bootstrap effect (self-healing).
```

- [ ] **Step 5: Add `"qty"` to pos_meta entries at order placement**

Find the block:
```python
pos_meta[alp_sym] = {
    "strategy": sig.strategy,
    "score": sig.score,
    "entry_price": fill_price if not queued else None,
    "stop": sig.stop_price,
    ...
}
```
Add inside the dict:
```python
"qty": fill_qty,
```

- [ ] **Step 6: Adapt trailing stop to use adaptive 1.5 or 3× ATR**

In the existing trailing block (around line 153-190), replace `new_stop = round(close - ATR_MULT_TRAIL * atr, 2)` (which uses old constant 4.0) with:
```python
# Adaptive trailing: tight (1.5× ATR) until +5% gain, then loose (3× ATR)
entry = float(m.get("entry_price") or 0)
if entry > 0:
    pnl_pct = (close - entry) / entry
    atr_mult = ATR_MULT_TRAIL if pnl_pct >= PROFIT_LOOSEN_PCT else ATR_MULT_STOP_INIT
else:
    atr_mult = ATR_MULT_STOP_INIT
new_stop = round(close - atr_mult * atr, 2)
```

- [ ] **Step 7: Skip the scan block if halted or shielded**

There are TWO `# 7.` comments in `shadow/runner.py`:
- Around **line 235**: `# 7. Ouvrir positions top N` ← this is INSIDE the wrap (the scan + entry block)
- Around **line 300**: `# 7. Sauve meta + log equity` ← this is OUTSIDE the wrap (must always run)

**Wrap range** : from "5. Récupère les ordres BUY en cours" (around line 192) **through the end of the entry for-loop** (around line 298) — i.e. up to but NOT including `save_meta(meta)` at line 301. The save_meta + log_equity step at line 300+ must always execute, even on halted/shielded cycles, so risk_guard state and meta are persisted.

```python
if not skip_new_entries:
    # … existing scan + entry logic, from "5. Récupère les ordres BUY"
    # through end of the "for sig in candidates ..." entry loop …
else:
    print("[SHADOW] skip_new_entries=True → gestion positions only", flush=True)

# THIS save_meta + log_equity block stays OUTSIDE the if/else — always runs
save_meta(meta)
# ... (Step 9 below adds rg.save() here as well)
```

- [ ] **Step 8: Replace the candidate iteration + sizing block with score-weighted version**

In the existing entry block, find the line:
```python
for sig in candidates[:TOP_N_SIGNALS]:
```
This is the start of the entry loop, around line 237 of the current file.

**Replace that one `for` line with a 2-line block** that pre-filters via the quality gate and switches to `enumerate`:
```python
# Quality gate filter + dédup-by-symbol already happened above (candidates is sorted by score desc).
accepted = [s for s in candidates if gate_passes(s, rg, now=now)][:TOP_N_SIGNALS]
for rank, sig in enumerate(accepted):
```
(The body of the loop continues at the original indentation.)

Then, **inside the loop body**, find the existing sizing block:
```python
stop_dist = abs(sig.entry_price - sig.stop_price)
if stop_dist <= 0:
    continue
risk_eur = equity * RISK_PER_TRADE_PCT
size = risk_eur / stop_dist
max_size = (equity * MAX_POSITION_PCT) / sig.entry_price
size = min(size, max_size)
```
and **replace it entirely** with score-weighted sizing:
```python
size_res = compute_size(rank=rank, cash=cash, entry_price=sig.entry_price)
size = size_res.qty
if size <= 0:
    continue
```

**About `cash`**: introduce `cash = float(account.get("cash", 0))` just after the existing `equity = float(account.get("equity", 0))` line (around line 117). The sizing uses cash (not equity) because the bot can only buy what it can currently spend.

Verify the rank variable is in scope (it comes from `enumerate(accepted)`).

- [ ] **Step 9: Save risk_guard at end of cycle**

After the existing `save_meta(meta)` call (line ~301), add:
```python
# Re-fetch latest equity so DD tracking sees the actual post-cycle state
try:
    account = broker.get_account()
    latest_equity = float(account.get("equity", equity))
except Exception:
    latest_equity = equity
end_ts = datetime.now(timezone.utc)
rg.update_equity(latest_equity, now=end_ts)
rg.prune_expired(now=end_ts)
rg.save()
```

- [ ] **Step 10: Verify imports + syntax**

```bash
venv/bin/python3 -c "import ast; ast.parse(open('shadow/runner.py').read()); print('OK')"
```
Expected: `OK`.

- [ ] **Step 11: Quick smoke import (won't connect to Alpaca)**

```bash
venv/bin/python3 -c "from shadow import runner; print('runner imports OK')"
```
Expected: `runner imports OK`.

- [ ] **Step 12: Commit**

```bash
git add shadow/runner.py
git commit -m "feat(shadow): runner v2 — quality gate + score-weighted sizing + adaptive trailing + stop fill detection"
```

---

### Task 10: Deploy + smoke prod

- [ ] **Step 1: Push to GitHub**

```bash
git push
```
Expected: CI/CD picks up, deploys to VPS in ~30s.

- [ ] **Step 2: Wait for VPS pull confirmation**

```bash
ssh ubuntu@51.210.13.248 "sudo -u botuser bash -c 'cd /home/botuser/bot-trading && until git log -1 --format=%h | grep -q $(git -C \"/home/damoria/Developpement REACT/bot trading\" log -1 --format=%h); do sleep 3; git fetch -q; done; git log -1 --oneline'"
```
Expected: VPS HEAD matches local HEAD.

- [ ] **Step 3: Restart shadow service to load new code**

```bash
ssh ubuntu@51.210.13.248 "sudo systemctl restart shadow"
```

- [ ] **Step 4: Wait for the first v2 cycle log line**

```bash
ssh ubuntu@51.210.13.248 "until sudo grep -q 'SHIELD=' /home/botuser/bot-trading/logs/shadow/shadow.log; do sleep 3; done; echo 'first v2 cycle ran'; sudo tail -20 /home/botuser/bot-trading/logs/shadow/shadow.log"
```
Expected: log includes lines like `[SHADOW] risk_guard: peak=$XXXXX halt=no`, `[SHADOW] SHIELD=no → skip_new_entries=False`, and entries with `rank=0/1/2` weighting.

- [ ] **Step 5: Verify risk_state.json was created**

```bash
ssh ubuntu@51.210.13.248 "sudo ls -la /home/botuser/bot-trading/logs/shadow/risk_state.json && sudo cat /home/botuser/bot-trading/logs/shadow/risk_state.json"
```
Expected: file present, JSON has `peak_equity` matching current Alpaca shadow equity.

- [ ] **Step 6: Verify no oversize on Alpaca shadow account**

Write a small audit script locally then upload + run on the VPS (inlined here because earlier session artifacts may not be available to a fresh implementer):

Create `/tmp/shadow_audit.py` locally:
```python
from shadow import broker

orders = broker.get_open_orders()
buys = [o for o in orders if o.get("side") == "buy"]
print(f"Open orders: {len(orders)} | Buys: {len(buys)}")

by_sym = {}
for o in buys:
    by_sym.setdefault(o["symbol"], []).append(o)

for s, lst in sorted(by_sym.items()):
    flag = " DUP" if len(lst) > 1 else ""
    print(f"  {s:10}: {len(lst)} buy(s){flag}")

dup_count = sum(1 for lst in by_sym.values() if len(lst) > 1)
assert dup_count == 0, f"GATE FAIL: {dup_count} symbol(s) have duplicate buys"
print("audit OK: no symbol has > 1 pending buy")
```

Then run:
```bash
scp /tmp/shadow_audit.py ubuntu@51.210.13.248:/tmp/shadow_audit.py
ssh ubuntu@51.210.13.248 "sudo cp /tmp/shadow_audit.py /home/botuser/bot-trading/audit_shadow.py && sudo chown botuser:botuser /home/botuser/bot-trading/audit_shadow.py && cd /home/botuser/bot-trading && sudo -u botuser bash -c 'set -a; source .env; set +a; python3 audit_shadow.py' && sudo rm /home/botuser/bot-trading/audit_shadow.py"
```
Expected: `audit OK: no symbol has > 1 pending buy`. If the assertion fires, the dédup+pending_buys guard from commit `7803182` has regressed — revert and investigate before continuing.

---

## Test the whole suite

- [ ] **Step 1: Final regression check**

```bash
venv/bin/pytest tests/ -v
```
Expected: **33 tests pass** (7 regime + 11 risk_guard + 8 quality_gate + 7 sizing).

---

## Done criteria

This plan is complete when:
1. All chunks pass review.
2. Backtest gate G1-G4 all green (CAGR≥30%, Sharpe≥1.0, MaxDD better than -25%, accounting <1% gap).
3. VPS deploys cleanly, shadow service restarts without error, first cycle logs include `SHIELD=` and `risk_guard:` lines.
4. Alpaca shadow account has no oversize / duplicate orders.
5. `risk_state.json` exists on VPS with valid JSON.

After done: observe 30 days paper live before deciding whether to migrate to prod (out of v2 scope, separate decision).
