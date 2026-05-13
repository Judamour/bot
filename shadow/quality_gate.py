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
    return reject_reason(sig, risk_guard, now) is None


def reject_reason(sig: Signal, risk_guard: RiskGuard, now: datetime) -> str | None:
    """Return the first failing gate code, or None if all 4 pass.

    Used for audit logging — same logic as passes() but exposes WHY a signal
    was rejected. Codes: G1_score, G2_mtf, G3_volume, G4_cooldown.
    """
    if sig.score < SCORE_FLOOR:
        return "G1_score"
    if not sig.rationale.get("mtf_aligned"):
        return "G2_mtf"
    vol = sig.rationale.get("volume_ratio")
    if vol is None or vol < 1.0:
        return "G3_volume"
    if risk_guard.is_in_cooldown(sig.symbol, now=now):
        return "G4_cooldown"
    return None
