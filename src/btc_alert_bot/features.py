"""Compute market features for the composite spike detector.

The detector cares about how *unusual* a given moment is relative to recent
history, not absolute values. We therefore standardize each raw feature with
a robust z-score (median + MAD) which tolerates the heavy-tailed BTC return
distribution far better than the classic mean+stdev z-score.

Inputs come from market.fetch_market_snapshot(); outputs feed into
detector.SpikeDetector.check_composite().
"""
from __future__ import annotations

import logging
import math
import statistics
from typing import Iterable

log = logging.getLogger(__name__)

# Wilder smoothing constant for ATR. Classic period is 14.
ATR_PERIOD = 14

# How many recent samples we use for robust statistics. Larger windows are
# stabler but lag regime changes; ~3 days of 5min bars is a good middle.
HIST_LOOKBACK_BARS = 288  # 24h * 12 bars/h


# ---------------------------------------------------------------------------
# Robust z-score (median / MAD)
# ---------------------------------------------------------------------------

def robust_z(value: float, history: Iterable[float]) -> float:
    """Return how many MADs the value is from the historical median.

    Returns 0.0 if history has too few points or zero spread (degenerate).
    The 1.4826 factor makes MAD a consistent estimator of stdev for normal data.
    """
    hist = [float(x) for x in history if x is not None and not math.isnan(x)]
    if len(hist) < 10:
        return 0.0
    med = statistics.median(hist)
    mad = statistics.median(abs(x - med) for x in hist) or 0.0
    if mad <= 0:
        return 0.0
    return (value - med) / (1.4826 * mad)


def clipped_z(value: float, history: Iterable[float], lo: float, hi: float) -> float:
    """Robust z, clipped to [lo, hi]. Used to bound any single feature's score weight."""
    z = robust_z(value, history)
    return max(lo, min(hi, z))


# ---------------------------------------------------------------------------
# ATR & price returns
# ---------------------------------------------------------------------------

def compute_true_range(candles: list[dict]) -> list[float]:
    """True Range series. candles must be chronological with open/high/low/close."""
    trs: list[float] = []
    prev_close: float | None = None
    for c in candles:
        h, l, close = c["high"], c["low"], c["close"]
        if prev_close is None:
            trs.append(h - l)
        else:
            trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        prev_close = close
    return trs


def compute_atr_series(candles: list[dict], period: int = ATR_PERIOD) -> list[float]:
    """Wilder's RMA of the True Range — the canonical ATR.

    Returns one ATR value per candle (NaN-equivalent 0.0 until period bars exist).
    """
    trs = compute_true_range(candles)
    atrs: list[float] = []
    rma: float | None = None
    for i, tr in enumerate(trs):
        if i < period:
            atrs.append(0.0)
            if i == period - 1:
                rma = sum(trs[:period]) / period
                # overwrite the last 0.0 with the seed
                atrs[-1] = rma
        else:
            assert rma is not None
            rma = (rma * (period - 1) + tr) / period
            atrs.append(rma)
    return atrs


def compute_returns_pct(closes: list[float], lag_bars: int) -> float:
    """Percentage return between closes[-1] and closes[-1 - lag_bars]."""
    if len(closes) <= lag_bars:
        return 0.0
    base = closes[-1 - lag_bars]
    if base <= 0:
        return 0.0
    return (closes[-1] / base - 1.0) * 100


# ---------------------------------------------------------------------------
# Headline features
# ---------------------------------------------------------------------------

def compute_market_features(snapshot: dict, state: dict | None = None) -> dict:
    """Extract the headline features detector.py compares against history.

    Returned dict is JSON-serializable so it can be appended to the state
    ring buffer for future z-score calculations.

    OI change-rate is derived from the rolling feature history (passed via
    ``state``) rather than a snapshot field, because OKX's public API does
    not expose a clean per-contract OI history endpoint.
    """
    klines = snapshot["klines_5m"]
    if len(klines) < ATR_PERIOD + 5:
        log.warning("Not enough klines (%d) for features", len(klines))
        return {}

    # Drop the unconfirmed last candle for stable features. Keep the live
    # close separately so callers can still reason about the very latest move.
    confirmed = klines[:-1]
    live_close = klines[-1]["close"]

    closes = [c["close"] for c in confirmed]
    atrs = compute_atr_series(confirmed)
    atr_now = atrs[-1] if atrs else 0.0
    close_now = closes[-1] if closes else 0.0

    # ATR as a percentage of price — comparable across regimes.
    atr_pct = (atr_now / close_now * 100) if close_now > 0 else 0.0

    # Returns on multiple horizons (in 5min-bar units: 3=15m, 12=1h, 288=24h).
    return_5m = compute_returns_pct([*closes, live_close], 1)
    return_15m = compute_returns_pct([*closes, live_close], 3)
    return_1h = compute_returns_pct([*closes, live_close], 12)
    return_24h = compute_returns_pct([*closes, live_close], 288) if len(closes) >= 288 else 0.0

    # |return_15m| normalized by ATR — "how big is this move vs typical 15m move?".
    # ATR is per-bar, so for 3-bar returns we scale by sqrt(3).
    move_per_atr = (
        abs(return_15m) / (atr_pct * math.sqrt(3))
        if atr_pct > 0 else 0.0
    )

    # Volume features (last bar + 5-bar window).
    vol_now = klines[-1]["volume"]
    vol_5bar = sum(c["volume"] for c in klines[-5:])

    # --- OI now from snapshot ticker, OI 1h ago from feature_history -----
    ticker = snapshot.get("ticker", {})
    oi_now = float(ticker.get("open_interest_btc", 0.0))

    # 1h-ago OI: walk back through state.feature_history to find a snapshot
    # whose OI was captured ~12 bars earlier (with tolerance).
    oi_change_pct = 0.0
    history = (state or {}).get("feature_history", []) or []
    if oi_now > 0 and len(history) >= 12:
        oi_1h_ago = _find_oi_one_hour_ago(history)
        if oi_1h_ago and oi_1h_ago > 0:
            oi_change_pct = (oi_now - oi_1h_ago) / oi_1h_ago * 100

    funding_rate = float(ticker.get("funding_rate", 0.0))

    return {
        "ts": klines[-1]["ts"].isoformat(),
        "close": live_close,
        "atr_pct": atr_pct,
        "return_5m": return_5m,
        "return_15m": return_15m,
        "return_1h": return_1h,
        "return_24h": return_24h,
        "move_per_atr": move_per_atr,
        "volume_now": vol_now,
        "volume_5bar": vol_5bar,
        "oi_now": oi_now,
        "oi_change_1h_pct": oi_change_pct,
        "funding_rate": funding_rate,
    }


def _find_oi_one_hour_ago(history: list[dict]) -> float | None:
    """Walk back through feature_history to find OI roughly 60 minutes ago.

    With a 5min cron, 12 entries back is the natural choice; we accept any
    entry whose timestamp is between 50 and 75 minutes old to tolerate
    GitHub Actions' variable cron delay.
    """
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    target_min = (now - timedelta(minutes=60))
    earliest_ok = now - timedelta(minutes=75)
    latest_ok = now - timedelta(minutes=50)
    best: tuple[float, float] | None = None  # (abs_offset, oi)
    for h in history:
        ts_s = h.get("ts")
        if not ts_s:
            continue
        try:
            ts = datetime.fromisoformat(ts_s)
        except Exception:
            continue
        if not (earliest_ok <= ts <= latest_ok):
            continue
        offset = abs((ts - target_min).total_seconds())
        oi_val = float(h.get("oi_now", 0.0) or 0.0)
        if oi_val <= 0:
            continue
        if best is None or offset < best[0]:
            best = (offset, oi_val)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# z-score helpers used by the detector
# ---------------------------------------------------------------------------

def history_field(history: list[dict], field: str) -> list[float]:
    """Project a single feature out of the rolling state history."""
    out: list[float] = []
    for h in history:
        v = h.get(field)
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out
