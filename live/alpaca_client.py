"""
Client Alpaca pour les xStocks — paper trading.
Données OHLCV + ordres BUY/SELL via Alpaca Paper Trading API.

Variables .env requises :
  ALPACA_API_KEY    — clé API (paper account)
  ALPACA_SECRET_KEY — clé secrète (paper account)
  ALPACA_PAPER      — "true" (défaut) ou "false" pour passer en réel
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_trading_client = None
_data_client = None


def _get_trading_client():
    global _trading_client
    if _trading_client is None:
        from alpaca.trading.client import TradingClient
        paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"
        _trading_client = TradingClient(
            api_key=os.getenv("ALPACA_API_KEY", ""),
            secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
            paper=paper,
        )
    return _trading_client


def _get_data_client():
    global _data_client
    if _data_client is None:
        from alpaca.data.historical import StockHistoricalDataClient
        _data_client = StockHistoricalDataClient(
            api_key=os.getenv("ALPACA_API_KEY", ""),
            secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
        )
    return _data_client


def is_configured() -> bool:
    """True si les clés Alpaca sont présentes dans l'environnement."""
    return bool(os.getenv("ALPACA_API_KEY")) and bool(os.getenv("ALPACA_SECRET_KEY"))


def fetch_ohlcv(symbol: str, timeframe: str = "4h", days: int = 45) -> pd.DataFrame:
    """
    Données OHLCV depuis Alpaca pour une action US.
    symbol : ex "NVDA" (sans /EUR)
    Retourne un DataFrame avec index UTC, prix en USD.
    """
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    # Mapping timeframe → Alpaca TimeFrame
    tf_map = {
        "1h":  TimeFrame(1,  TimeFrameUnit.Hour),
        "4h":  TimeFrame(4,  TimeFrameUnit.Hour),
        "1d":  TimeFrame(1,  TimeFrameUnit.Day),
    }
    alpaca_tf = tf_map.get(timeframe, TimeFrame(4, TimeFrameUnit.Hour))

    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    client = _get_data_client()
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=alpaca_tf,
        start=start,
        end=end,
    )
    bars = client.get_stock_bars(request)
    df = bars.df

    if df.empty:
        raise ValueError(f"Aucune donnée Alpaca pour {symbol}")

    # bars.df a un MultiIndex (symbol, timestamp) — on garde juste le symbole demandé
    if isinstance(df.index, pd.MultiIndex):
        df = df.loc[symbol]

    df.index = pd.to_datetime(df.index, utc=True)
    df = df[["open", "high", "low", "close", "volume"]]
    df = df[~df.index.duplicated(keep="last")].sort_index()

    print(f"  ✓ {len(df)} bougies {symbol} [{timeframe}] via Alpaca ({df.index[0].date()} → {df.index[-1].date()})")
    return df


def place_order(symbol: str, qty: float, side: str, stop_loss: float, take_profit: float) -> dict:
    """
    Passe un ordre market sur Alpaca paper trading.
    side : "buy" ou "sell"
    Retourne le dict order avec l'id Alpaca.
    """
    from alpaca.trading.requests import MarketOrderRequest, OrderClass, TakeProfitRequest, StopLossRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    client = _get_trading_client()

    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

    # Ordre bracket (market + SL + TP en une seule requête)
    req = MarketOrderRequest(
        symbol=symbol,
        qty=round(qty, 6),
        side=order_side,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        stop_loss=StopLossRequest(stop_price=round(stop_loss, 4)),
        take_profit=TakeProfitRequest(limit_price=round(take_profit, 4)),
    )

    order = client.submit_order(req)
    return {
        "alpaca_id": str(order.id),
        "symbol": symbol,
        "side": side,
        "qty": float(order.qty),
        "status": str(order.status),
    }


def close_position(symbol: str) -> dict:
    """Clôture la position ouverte sur Alpaca pour ce symbole."""
    client = _get_trading_client()
    result = client.close_position(symbol)
    return {"alpaca_id": str(result.id), "status": str(result.status)}


def get_account() -> dict:
    """Retourne le solde et la valeur du compte paper Alpaca."""
    client = _get_trading_client()
    account = client.get_account()
    return {
        "cash": float(account.cash),
        "portfolio_value": float(account.portfolio_value),
        "buying_power": float(account.buying_power),
        "paper": os.getenv("ALPACA_PAPER", "true").lower() == "true",
    }


if __name__ == "__main__":
    if not is_configured():
        print("⚠ ALPACA_API_KEY et ALPACA_SECRET_KEY non définis dans .env")
        print("  Ajoute-les puis relance ce script.")
    else:
        print("=== Test Alpaca ===")
        print("Compte:", get_account())
        df = fetch_ohlcv("NVDA", "4h", 10)
        print(df.tail(3))
