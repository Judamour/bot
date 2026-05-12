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
