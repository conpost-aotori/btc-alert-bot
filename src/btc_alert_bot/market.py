"""OKX public market data fetchers.

We deliberately use OKX rather than Bybit because Bybit returns HTTP 403
to data-center IP ranges (where GitHub Actions runs), while OKX is
CI-friendly. The data we need (BTC-USDT-SWAP perpetual: OHLC, OI,
funding) is equivalent on both exchanges.

Endpoints used:
- /api/v5/market/candles         OHLCV candles
- /api/v5/market/ticker          Latest price + 24h vol
- /api/v5/public/open-interest   Current OI snapshot
- /api/v5/public/funding-rate-history  Recent funding history
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"
INST_ID = "BTC-USDT-SWAP"          # USDT-margined linear perpetual
INST_TYPE = "SWAP"

_session = requests.Session()
_session.headers.update({"User-Agent": "btc-alert-bot/0.1"})


def _get(path: str, params: dict, timeout: int = 15) -> list:
    resp = _session.get(f"{OKX_BASE}{path}", params=params, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    code = payload.get("code")
    # OKX returns "0" on success.
    if code not in ("0", 0, None):
        raise RuntimeError(f"OKX error: code={code} msg={payload.get('msg')}")
    return payload.get("data", [])


# ---------------------------------------------------------------------------
# Klines
# ---------------------------------------------------------------------------

# OKX bar codes: "1m","3m","5m","15m","30m","1H","2H","4H","6H","12H","1D"...
def fetch_klines(bar: str = "5m", limit: int = 200) -> list[dict]:
    """Fetch BTC-USDT-SWAP OHLCV candles, oldest-first.

    OKX returns newest-first; we reverse for chronological order. The latest
    bar may be unconfirmed — the 9th element ("confirm") is "1" when closed.
    """
    rows = _get(
        "/api/v5/market/candles",
        {"instId": INST_ID, "bar": bar, "limit": str(limit)},
    )
    rows.reverse()
    out: list[dict] = []
    for r in rows:
        # Schema: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        out.append({
            "ts": datetime.fromtimestamp(int(r[0]) / 1000, tz=timezone.utc),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),       # in BTC contracts
            "turnover": float(r[6]),     # in BTC
            "confirmed": r[8] == "1" if len(r) > 8 else True,
        })
    return out


# ---------------------------------------------------------------------------
# Ticker
# ---------------------------------------------------------------------------

def fetch_ticker() -> dict:
    """Latest BTC-USDT-SWAP snapshot: price + 24h volume range."""
    rows = _get("/api/v5/market/ticker", {"instId": INST_ID})
    if not rows:
        raise RuntimeError("OKX ticker returned empty list")
    t = rows[0]
    last = float(t["last"])
    open24 = float(t.get("open24h", last))
    pct_24h = ((last / open24 - 1) * 100) if open24 > 0 else 0.0
    return {
        "ts": datetime.now(timezone.utc),
        "last_price": last,
        "open_24h": open24,
        "high_24h": float(t["high24h"]),
        "low_24h": float(t["low24h"]),
        "volume_24h": float(t.get("vol24h", 0.0)),       # in contracts
        "turnover_24h": float(t.get("volCcy24h", 0.0)),  # in BTC
        "price_change_pct_24h": pct_24h,
    }


# ---------------------------------------------------------------------------
# Open Interest (current snapshot — history is derived from feature_history)
# ---------------------------------------------------------------------------

def fetch_open_interest() -> dict | None:
    """Current OI snapshot.

    OKX's free public OI history endpoint requires an ``instFamily`` filter
    that doesn't return a clean per-contract series, so we fetch only the
    current value here. The detector compares it against historical OI
    snapshots stored in state.feature_history.
    """
    rows = _get(
        "/api/v5/public/open-interest",
        {"instType": INST_TYPE, "instId": INST_ID},
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "ts": datetime.fromtimestamp(int(r["ts"]) / 1000, tz=timezone.utc),
        "oi_contracts": float(r.get("oi", 0.0)),
        "oi_btc": float(r.get("oiCcy", 0.0)),
        "oi_usd": float(r.get("oiUsd", 0.0)) if r.get("oiUsd") else 0.0,
    }


# ---------------------------------------------------------------------------
# Funding rate history
# ---------------------------------------------------------------------------

def fetch_funding_history(limit: int = 30) -> list[dict]:
    """Funding rate history, oldest-first. OKX pays every 8h."""
    rows = _get(
        "/api/v5/public/funding-rate-history",
        {"instId": INST_ID, "limit": str(limit)},
    )
    rows.reverse()
    return [
        {
            "ts": datetime.fromtimestamp(int(r["fundingTime"]) / 1000, tz=timezone.utc),
            "rate": float(r["fundingRate"]),
        }
        for r in rows
    ]


def fetch_current_funding() -> float:
    """Latest funding rate (also exposed in funding history's last item)."""
    rows = _get(
        "/api/v5/public/funding-rate", {"instId": INST_ID}
    )
    if not rows:
        return 0.0
    return float(rows[0].get("fundingRate", 0.0))


# ---------------------------------------------------------------------------
# Bundled snapshot for the detection pipeline
# ---------------------------------------------------------------------------

def fetch_market_snapshot() -> dict:
    """Single call surface used by main.py — fetches everything detection needs.

    Network failures bubble up; main.py decides whether to fall back to the
    simpler CoinGecko-only path.
    """
    klines_5m = fetch_klines(bar="5m", limit=200)
    ticker = fetch_ticker()
    oi = fetch_open_interest() or {}
    funding_history = fetch_funding_history(limit=30)
    current_funding = fetch_current_funding()

    # Surface a flat dict shape compatible with the existing features.py.
    return {
        "klines_5m": klines_5m,
        "ticker": {
            **ticker,
            "open_interest_btc": oi.get("oi_btc", 0.0),
            "open_interest_usd": oi.get("oi_usd", 0.0),
            "funding_rate": current_funding,
        },
        # OI history is now derived from state.feature_history; we keep this
        # field empty for backwards compat with features.py which falls back
        # to history lookup when this is short.
        "oi_history": [],
        "funding_history": funding_history,
    }
