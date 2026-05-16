"""AUM estimator — stable cash + cost basis composition + cache TTL.

The estimator must NEVER react to MTM swings on open positions, because we
use AUM as the denominator for copy sizing. A position MTM-ing from $0.04
→ $0.99 would 25x the denominator if we used currentValue, inflating then
deflating the trade_pct we mirror.

Formula: cash_estim = max(0, /value − Σ currentValue), then
         AUM = cash_estim + Σ initialValue (cost basis).
"""
import time
from unittest.mock import patch

from live.copytrade import aum_estimator


def _pos(initial_value, current_value, size=100, avg_price=None, redeemable=False):
    """Build a position dict matching Polymarket /positions schema.
    Default redeemable=False = open active position (counts in AUM)."""
    return {
        "size": size,
        "avgPrice": avg_price if avg_price is not None else initial_value / size,
        "initialValue": initial_value,
        "currentValue": current_value,
        "curPrice": current_value / size if size else 0,
        "redeemable": redeemable,
    }


def test_aum_is_cash_plus_cost_basis():
    """value=$5000, positions cost=$1000, MTM=$3000 → cash=$2000, AUM=$3000."""
    positions = [_pos(initial_value=1000.0, current_value=3000.0)]
    with patch("live.copytrade.aum_estimator.data_api.value", return_value=5000.0), \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=positions):
        aum = aum_estimator.aum("0xW", _cache_ttl=0)
    # cash = 5000 - 3000 = 2000 ; AUM = 2000 + 1000 (cost) = 3000
    assert aum == 3000.0


def test_aum_stable_when_position_mtms_up():
    """KEY: une position $0.04 → $0.99 ne doit PAS changer l'AUM.
    Si cash_estim reste correct, la formule isole le cost basis."""
    # Snapshot 1 : entrée à $0.04, pas encore de MTM (cur ≈ entry)
    p1 = _pos(initial_value=100.0, current_value=100.0, size=2500)
    with patch("live.copytrade.aum_estimator.data_api.value", return_value=1100.0), \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=[p1]):
        aum_estimator.clear_cache()
        aum1 = aum_estimator.aum("0xW", _cache_ttl=0)
    # cash = 1100 - 100 = 1000 ; AUM = 1000 + 100 = 1100

    # Snapshot 2 : favori vire à $0.99 → currentValue $2475, mais cash réel inchangé
    # Polymarket /value reflète cette hausse : value = cash(1000) + MTM(2475) = 3475
    p2 = _pos(initial_value=100.0, current_value=2475.0, size=2500)
    with patch("live.copytrade.aum_estimator.data_api.value", return_value=3475.0), \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=[p2]):
        aum_estimator.clear_cache()
        aum2 = aum_estimator.aum("0xW", _cache_ttl=0)
    # cash = 3475 - 2475 = 1000 ; AUM = 1000 + 100 = 1100

    assert aum1 == aum2 == 1100.0


def test_aum_grows_only_after_realization():
    """Quand la position se résout (currentValue → 0, cash reçoit le payoff),
    l'AUM doit refléter le gain réalisé."""
    # Position résolue → cash a reçu $2500, plus aucune position ouverte
    with patch("live.copytrade.aum_estimator.data_api.value", return_value=3500.0), \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=[]):
        aum_estimator.clear_cache()
        aum = aum_estimator.aum("0xW", _cache_ttl=0)
    # cash = 3500 ; AUM = 3500 + 0 = 3500 (gain de +$2400 vs start $1100)
    assert aum == 3500.0


def test_aum_falls_back_to_cost_basis_when_value_endpoint_lags():
    """/value parfois retourne 0 (lag indexer). Fallback : sum(initialValue)."""
    positions = [
        _pos(initial_value=30.0, current_value=100.0),  # MTM gonflé, on ignore
        _pos(initial_value=40.0, current_value=200.0),
    ]
    with patch("live.copytrade.aum_estimator.data_api.value", return_value=0.0), \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=positions):
        aum_estimator.clear_cache()
        aum = aum_estimator.aum("0xW", _cache_ttl=0)
    # value=0 → cash=0 ; AUM = 0 + (30+40) = 70 (pas 300 comme l'ancien code)
    assert aum == 70.0


def test_aum_clamps_negative_cash():
    """Race condition : sum(currentValue) > /value (positions plus à jour
    que /value). cash_estim doit être clamp à 0, jamais négatif."""
    positions = [_pos(initial_value=100.0, current_value=500.0)]
    with patch("live.copytrade.aum_estimator.data_api.value", return_value=300.0), \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=positions):
        aum_estimator.clear_cache()
        aum = aum_estimator.aum("0xW", _cache_ttl=0)
    # cash_estim = max(0, 300 - 500) = 0 ; AUM = 0 + 100 = 100
    assert aum == 100.0


def test_aum_ignores_redeemable_resolved_positions():
    """REAL-WORLD BUG: bossoskil1 avait 98/100 positions résolues-perdues encore
    dans la liste (redeemable=True, currentValue=0). Σ initialValue brute donnait
    $10.5M de cost basis fantôme vs $210K de /value réel. On doit exclure les
    positions redeemable du cost_open."""
    poss = [
        # 2 open positions actives (la seule chose qui doit compter)
        _pos(initial_value=50_000.0, current_value=60_000.0, redeemable=False),
        _pos(initial_value=30_000.0, current_value=35_000.0, redeemable=False),
        # 5 stale loser stubs : initialValue énorme, currentValue=0, redeemable=true
        _pos(initial_value=100_000.0, current_value=0.0, redeemable=True),
        _pos(initial_value=200_000.0, current_value=0.0, redeemable=True),
        _pos(initial_value=150_000.0, current_value=0.0, redeemable=True),
        _pos(initial_value=80_000.0, current_value=0.0, redeemable=True),
        _pos(initial_value=120_000.0, current_value=0.0, redeemable=True),
    ]
    with patch("live.copytrade.aum_estimator.data_api.value", return_value=110_000.0), \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=poss):
        aum_estimator.clear_cache()
        aum = aum_estimator.aum("0xW", _cache_ttl=0)
    # cost_open = 50K + 30K = 80K (les stales sont exclues)
    # mtm_open = 60K + 35K = 95K
    # cash = 110K - 95K = 15K
    # AUM = 15K + 80K = 95K  (PAS 760K comme si on sommait tout)
    assert aum == 95_000.0


def test_aum_zero_when_no_data():
    with patch("live.copytrade.aum_estimator.data_api.value", return_value=0.0), \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=[]):
        aum_estimator.clear_cache()
        aum = aum_estimator.aum("0xW", _cache_ttl=0)
    assert aum == 0.0


def test_cache_hit_skips_api_calls():
    """Two calls within TTL — second uses cache."""
    aum_estimator.clear_cache()
    positions = [_pos(initial_value=500.0, current_value=500.0)]
    with patch("live.copytrade.aum_estimator.data_api.value", return_value=1000.0) as mv, \
         patch("live.copytrade.aum_estimator.data_api.positions",
               return_value=positions) as mp:
        a1 = aum_estimator.aum("0xW", _cache_ttl=60)
        a2 = aum_estimator.aum("0xW", _cache_ttl=60)
    # cash = 1000 - 500 = 500 ; AUM = 500 + 500 = 1000
    assert a1 == a2 == 1000.0
    assert mv.call_count == 1
    assert mp.call_count == 1


def test_cache_expires():
    aum_estimator.clear_cache()
    t = [1000.0]

    def fake_value(_):
        t[0] += 100
        return t[0]

    with patch("live.copytrade.aum_estimator.data_api.value", side_effect=fake_value), \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=[]), \
         patch("live.copytrade.aum_estimator.time.time", side_effect=[0, 70]):
        a1 = aum_estimator.aum("0xW", _cache_ttl=60)
        a2 = aum_estimator.aum("0xW", _cache_ttl=60)
    # no positions → cash = value, AUM = value
    assert a1 == 1100.0
    assert a2 == 1200.0  # cache expired between calls
