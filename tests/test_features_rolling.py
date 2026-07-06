"""Rolling short-horizon returns — regression test for the 2026-07-06 miss.

That day BTC ground down ~0.2-0.25%/min for ~15 minutes. The legacy
computation anchored return_5m to the last completed 5-MINUTE bar, so right
after each bar boundary the measured "5m return" reset toward zero — the bot
logged ``5m=+0.02%`` while the true trailing-5-minute move was ≈ -0.9%, and
the 1.0% hard floor never fired.

These tests replicate that shape: a steady 1m grind where
  - the ROLLING computation (klines_1m present) reports the true move and
    crosses the hard floor, while
  - the legacy ANCHORED computation (no klines_1m) reports ~0 at boundary
    phase — the blind spot.

Runs standalone:  python tests/test_features_rolling.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc_alert_bot.detector import HARD_FALLBACK_RETURN_5M_PCT  # noqa: E402
from btc_alert_bot.features import compute_market_features  # noqa: E402

_T0 = datetime(2026, 7, 6, 11, 50, tzinfo=timezone.utc)


def _bar(ts: datetime, close: float) -> dict:
    """Minimal kline dict with the fields features.py touches."""
    return {
        "ts": ts,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 100.0,
        "volume_btc": 1.0,
        "volume_usdt": close,
    }


def _grind_snapshot(rate_per_min: float = 0.0025, with_1m: bool = True) -> dict:
    """A steady downward grind ending exactly ON a 5m boundary.

    1m closes fall ``rate_per_min`` per bar for 20 bars. The 5m klines are
    built so the last confirmed 5m close EQUALS the live close — the
    boundary-phase worst case where the anchored 5m return reads 0.0.
    """
    closes_1m = [62000.0 * (1 - rate_per_min) ** i for i in range(20)]
    live = closes_1m[-1]
    klines_1m = [
        _bar(_T0 + timedelta(minutes=i), c) for i, c in enumerate(closes_1m)
    ]
    # 30 flat 5m bars at the live price: anchored return_5m/15m == 0 exactly.
    klines_5m = [
        _bar(_T0 - timedelta(minutes=5 * (30 - i)), live) for i in range(30)
    ]
    snap = {"klines_5m": klines_5m, "ticker": {}}
    if with_1m:
        snap["klines_1m"] = klines_1m
    return snap


def test_rolling_reports_true_grind_and_crosses_hard_floor():
    feats = compute_market_features(_grind_snapshot(with_1m=True))
    assert feats, "features should compute"
    r5, r15 = feats["return_5m"], feats["return_15m"]
    # 5 bars x -0.25% compounding ≈ -1.244%; 15 bars ≈ -3.68%.
    assert -1.30 < r5 < -1.15, f"rolling 5m should show the true move, got {r5}"
    assert -3.90 < r15 < -3.45, f"rolling 15m should show the true move, got {r15}"
    # The whole point: the rolling value crosses the 1.0% hard floor.
    assert abs(r5) >= HARD_FALLBACK_RETURN_5M_PCT


def test_anchored_blind_spot_without_1m_klines():
    # Same market shape, no 1m data -> legacy anchored computation -> 0.0
    # at boundary phase. This documents the blind spot the fix removes.
    feats = compute_market_features(_grind_snapshot(with_1m=False))
    assert feats
    assert feats["return_5m"] == 0.0
    assert feats["return_15m"] == 0.0


def test_short_1m_history_falls_back_to_anchored():
    snap = _grind_snapshot(with_1m=True)
    snap["klines_1m"] = snap["klines_1m"][-10:]  # < 16 bars -> fallback
    feats = compute_market_features(snap)
    assert feats
    assert feats["return_5m"] == 0.0  # anchored fallback, not a crash
    assert feats["return_15m"] == 0.0


def test_flat_market_stays_zero():
    snap = _grind_snapshot(rate_per_min=0.0, with_1m=True)
    feats = compute_market_features(snap)
    assert feats
    assert feats["return_5m"] == 0.0
    assert feats["return_15m"] == 0.0


def test_in_progress_last_bar_gets_lag_bump():
    """WS-mode reality: the last 1m kline is IN-PROGRESS with ~0s elapsed.
    Without the lag bump, 'return_5m' would span only ~4 minutes (-1.00%,
    under the 1.0% floor); with it, the true 5 minutes (-1.24%)."""
    snap = _grind_snapshot(with_1m=True)
    last = snap["klines_1m"][-1]
    live = _bar(last["ts"] + timedelta(minutes=1), last["close"])
    live["confirmed"] = False  # in-progress, zero elapsed movement
    snap["klines_1m"] = snap["klines_1m"] + [live]
    feats = compute_market_features(snap)
    r5 = feats["return_5m"]
    assert -1.30 < r5 < -1.15, f"lag bump missing? got {r5}"
    assert abs(r5) >= HARD_FALLBACK_RETURN_5M_PCT


def test_bump_needs_one_extra_bar_else_fallback():
    snap = _grind_snapshot(with_1m=True)
    last = snap["klines_1m"][-1]
    live = _bar(last["ts"] + timedelta(minutes=1), last["close"])
    live["confirmed"] = False
    # 15 confirmed + 1 in-progress = 16 < 16+bump(1) -> anchored fallback
    snap["klines_1m"] = snap["klines_1m"][-15:] + [live]
    feats = compute_market_features(snap)
    assert feats["return_5m"] == 0.0  # fell back, no crash


def test_longer_horizons_unchanged_by_1m_presence():
    a = compute_market_features(_grind_snapshot(with_1m=True))
    b = compute_market_features(_grind_snapshot(with_1m=False))
    for k in ("return_1h", "return_2h", "return_12h", "return_24h"):
        assert a[k] == b[k], f"{k} must not depend on klines_1m"


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e!r}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
