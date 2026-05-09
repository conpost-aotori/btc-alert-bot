"""Bybit public market data fetchers.

All endpoints are unauthenticated and free. We deliberately consolidate the
calls here so feature engineering (features.py) and detection (detector.py)
have a single, mockable interface.

Endpoints used:
- /v5/market/kline           OHLCV candles (5min, 15min)
- /v5/market/tickers         Latest price, 24h volume, OI, funding rate
- /v5/market/open-interest   OI history (5min interval)
- /v5/market/funding/history Recent funding rate history
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

BYBIT_BASE = "https://api.bybit.com"
SYMBOL = "BTCUSDT"
CATEGORY = "linear"  # USDT-margined perpetual

# A single requests.Session pools TLS — small wins matter on cold CI runners.
_session = requests.Session()
_session.headers.update({"User-Agent": "btc-alert-bot/0.1"})


def _get(path: str, params: dict, timeout: int = 15) -> dict:
    resp = _session.get(f"{BYBIT_BASE}{path}", params=params, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("retCode") not in (0, None):
        raise RuntimeError(f"Bybit error: {payload.get('retMsg')} ({payload})")
    return payload.get("result", {})


# ---------------------------------------------------------------------------
# Klines
# ---------------------------------------------------------------------------

def fetch_klines(interval: str = "5", limit: int = 200) -> list[dict]:
    """Fetch BTCUSDT OHLCV candles, oldest-first.

    interval: "1","3","5","15","30","60","120","240","360","720","D","W","M"
    limit:    max 1000 (Bybit cap)

    The most recent candle is *unconfirmed* (in progress). Callers that need
    closed candles should drop the last element.
    """
    result = _get(
        "/v5/market/kline",
        {"category": CATEGORY, "symbol": SYMBOL, "interval": interval, "limit": limit},
    )
    raw = result.get("list", [])
    raw.reverse()  # newest-first → chronological
    return [
        {
            "ts": datetime.fromtimestamp(int(c[0]) / 1000, tz=timezone.utc),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
            "turnover": float(c[6]),
        }
        for c in raw
    ]


# ---------------------------------------------------------------------------
# Ticker (live snapshot)
# ---------------------------------------------------------------------------

def fetch_ticker() -> dict:
    """Latest BTCUSDT perp snapshot: price, 24h volume, OI, funding rate."""
    result = _get(
        "/v5/market/tickers", {"category": CATEGORY, "symbol": SYMBOL}
    )
    rows = result.get("list", [])
    if not rows:
        raise RuntimeError("Bybit ticker returned empty list")
    t = rows[0]
    # Bybit returns numeric values as strings — cast carefully.
    return {
        "ts": datetime.now(timezone.utc),
        "last_price": float(t["lastPrice"]),
        "mark_price": float(t.get("markPrice", t["lastPrice"])),
        "index_price": float(t.get("indexPrice", t["lastPrice"])),
        "price_change_pct_24h": float(t.get("price24hPcnt", 0.0)) * 100,
        "high_24h": float(t["highPrice24h"]),
        "low_24h": float(t["lowPrice24h"]),
        "volume_24h": float(t["volume24h"]),       # in BTC
        "turnover_24h": float(t["turnover24h"]),    # in USDT
        "open_interest_btc": float(t.get("openInterest", 0.0)),
        "open_interest_usd": float(t.get("openInterestValue", 0.0)),
        "funding_rate": float(t.get("fundingRate", 0.0)),       # decimal, e.g. 0.0001
        "funding_next_ts": int(t.get("nextFundingTime", 0)),    # ms
    }


# ---------------------------------------------------------------------------
# Open Interest history
# ---------------------------------------------------------------------------

def fetch_open_interest(interval_time: str = "5min", limit: int = 200) -> list[dict]:
    """OI history, oldest-first.

    interval_time: "5min","15min","30min","1h","4h","1d"
    Useful for detecting OI drops that often coincide with cascade liquidations.
    """
    result = _get(
        "/v5/market/open-interest",
        {
            "category": CATEGORY,
            "symbol": SYMBOL,
            "intervalTime": interval_time,
            "limit": limit,
        },
    )
    raw = result.get("list", [])
    raw.reverse()
    return [
        {
            "ts": datetime.fromtimestamp(int(r["timestamp"]) / 1000, tz=timezone.utc),
            "oi": float(r["openInterest"]),
        }
        for r in raw
    ]


# ---------------------------------------------------------------------------
# Funding rate history
# ---------------------------------------------------------------------------

def fetch_funding_history(limit: int = 60) -> list[dict]:
    """Funding rate history, oldest-first. Bybit pays every 8h, so 60 ≈ 20 days."""
    result = _get(
        "/v5/market/funding/history",
        {"category": CATEGORY, "symbol": SYMBOL, "limit": limit},
    )
    raw = result.get("list", [])
    raw.reverse()
    return [
        {
            "ts": datetime.fromtimestamp(int(r["fundingRateTimestamp"]) / 1000, tz=timezone.utc),
            "rate": float(r["fundingRate"]),
        }
        for r in raw
    ]


# ---------------------------------------------------------------------------
# Bundled snapshot for the detection pipeline
# ---------------------------------------------------------------------------

def fetch_market_snapshot() -> dict:
    """Single call surface used by main.py — fetches everything detection needs.

    Network failures bubble up; main.py decides whether to fall back to the
    simpler CoinGecko-only path.
    """
    # 5min klines: 200 bars = ~16h of context (enough for 14-period ATR with room).
    klines_5m = fetch_klines(interval="5", limit=200)
    ticker = fetch_ticker()
    oi_history = fetch_open_interest(interval_time="5min", limit=72)  # 6h
    funding_history = fetch_funding_history(limit=30)                  # ~10 days
    return {
        "klines_5m": klines_5m,
        "ticker": ticker,
        "oi_history": oi_history,
        "funding_history": funding_history,
    }
