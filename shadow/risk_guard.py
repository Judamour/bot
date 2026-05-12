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
