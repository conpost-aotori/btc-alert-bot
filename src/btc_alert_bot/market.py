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
        # OKX schema: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        # - vol         : number of contracts (1 contract = 0.01 BTC for SWAP)
        # - volCcy      : volume in base currency (BTC)
        # - volCcyQuote : volume in quote currency (USDT)
        # We expose all three with explicit names so callers can't confuse
        # "contracts" with "BTC" (which an earlier version did).
        out.append({
            "ts": datetime.fromtimestamp(int(r[0]) / 1000, tz=timezone.utc),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume_contracts": float(r[5]),
            "volume_btc": float(r[6]),
            "volume_usdt": float(r[7]) if len(r) > 7 else 0.0,
            # Back-compat aliases (some chart code still reads ``volume``).
            "volume": float(r[5]),
            "turnover": float(r[6]),
            "confirmed": r[8] == "1" if len(r) > 8 else True,
        })
    return out


def fetch_year_low() -> float | None:
    """Lowest daily LOW since Jan 1 of the current (UTC) year, or None.

    Used to seed the year-to-date-low milestone baseline. Fetches up to
    300 daily candles (covers >9 months) and takes the min low within the
    calendar year. Returns None on any failure so callers degrade safely.
    """
    try:
        now = datetime.now(timezone.utc)
        jan1 = datetime(now.year, 1, 1, tzinfo=timezone.utc)
        candles = fetch_klines(bar="1D", limit=300)
        lows = []
        for c in candles:
            ts = c.get("ts")
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= jan1:
                lows.append(float(c["low"]))
        return min(lows) if lows else None
    except Exception as e:
        log.warning("fetch_year_low failed: %s", e)
        return None


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

# ---------------------------------------------------------------------------
# Per-spike-window OHLCV helper — used by publishers to enrich the alert
# embed with the actual high/low/volume of the bar that triggered.
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

# Map an alert window to the OKX bar code we need to look up.
_WINDOW_TO_OKX_BAR: dict[str, str] = {
    "1m":  "1m",
    "3m":  "3m",
    "5m":  "5m",
    "15m": "15m",
    "1h":  "1H",
    "2h":  "2H",   # slow-grind detector
    "12h": "12H",  # extended-tier status report
    # 24h+ no longer fires alerts (removed at the user's request) but we
    # leave the mapping here so a stale spike object doesn't crash.
    "24h": "1D",
}

_WINDOW_DURATION_S: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900,
    "1h": 3600, "2h": 7200, "12h": 43200, "24h": 86400,
}


def _format_ohlcv(c: dict) -> dict:
    return {
        "ts": c["ts"].isoformat(),
        "open": c["open"],
        "high": c["high"],
        "low": c["low"],
        "close": c["close"],
        # Use the actual base/quote-currency volumes — earlier code
        # mistakenly labelled the contract count as BTC and over-stated
        # bar volume by 100×.
        "volume_btc": c.get("volume_btc", 0.0),
        "volume_usd": c.get(
            "volume_usdt", c.get("volume_btc", 0.0) * c["close"]
        ),
    }


def fetch_window_ohlcv(
    window: str,
    anchor_ts: str | None = None,
) -> dict | None:
    """Return the OHLCV bar that the spike actually triggered from.

    ``anchor_ts`` is the timestamp the detector observed (typically
    ``features["ts"]``, which equals the LIVE bar's ts in cron mode and
    the just-confirmed bar's ts in WS mode). If supplied, the matching
    bar is returned regardless of confirm flag — that's the bar the
    user actually wants to see in the embed.

    Falls back to "most recent confirmed bar" when no anchor is given.
    Returns None on unknown window / fetch failure / no data. Never raises.
    """
    bar = _WINDOW_TO_OKX_BAR.get(window)
    if not bar:
        return None
    try:
        # limit=3 leaves room for clock-skew / late updates while staying tiny.
        candles = fetch_klines(bar=bar, limit=3)
    except Exception as e:
        log.warning("fetch_window_ohlcv(%s) failed: %s", window, e)
        return None
    if not candles:
        return None

    # Try anchor-ts match first — this picks the in-progress bar in cron
    # mode (where detection used its live close) and the just-closed bar
    # in WS mode (where detection ran on confirm=1).
    if anchor_ts:
        try:
            anchor_dt = datetime.fromisoformat(anchor_ts)
        except Exception:
            anchor_dt = None
        if anchor_dt is not None:
            bar_secs = _WINDOW_DURATION_S.get(window, 300)
            for c in reversed(candles):
                start = c["ts"]
                if start <= anchor_dt < start + timedelta(seconds=bar_secs):
                    return _format_ohlcv(c)

    # Fallback: most recent confirmed bar (matches fast-track semantics
    # where the trigger is always a just-closed candle).
    for c in reversed(candles):
        if c.get("confirmed"):
            return _format_ohlcv(c)
    return _format_ohlcv(candles[-1])


def fetch_market_snapshot() -> dict:
    """Single call surface used by main.py — fetches everything detection needs.

    Network failures bubble up; main.py decides whether to fall back to the
    simpler CoinGecko-only path.
    """
    klines_5m = fetch_klines(bar="5m", limit=200)
    # 1m klines let features.py compute return_5m / return_15m as TRUE
    # rolling windows. The 5m-kline computation anchors to bar boundaries
    # (the "5m return" resets toward zero right after each boundary), which
    # blinded the detector to the steady -0.2%/min grind on 2026-07-06.
    # Isolated failure: this fetch degrading must not take down the whole
    # snapshot — features.py falls back to the 5m-anchored computation.
    try:
        klines_1m = fetch_klines(bar="1m", limit=20)
    except Exception as e:
        log.warning(
            "1m kline fetch failed (%s) — short returns fall back to "
            "5m-bar anchoring", e,
        )
        klines_1m = []
    ticker = fetch_ticker()
    oi = fetch_open_interest() or {}
    funding_history = fetch_funding_history(limit=30)
    current_funding = fetch_current_funding()

    # Surface a flat dict shape compatible with the existing features.py.
    return {
        "klines_5m": klines_5m,
        "klines_1m": klines_1m,
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
