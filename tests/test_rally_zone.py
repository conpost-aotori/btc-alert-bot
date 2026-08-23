"""Rally-zone (上昇基調) counter-trend suppression for pullback alerts —
mirrors bear_zone for the upside. Regression tests.

Backtests the exact 2026-08-22 miss: a -1.40%/1m and a -2.13%/1h alert
fired mid-pullback during a clear rally (24h change +2.54%) because at
the moment of firing the 1h had already flipped negative (-1.37%) — the
legacy gate (t1h>=1.0 OR t24>=3.0) requires a fast-moving signal to still
agree, and missed a regime that was obviously still a rally one candle
earlier.

Runs standalone:  python tests/test_rally_zone.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc_alert_bot.detector import (  # noqa: E402
    COUNTER_TREND_OVERRIDE_PCT,
    HARD_FALLBACK_RETURN_12H_PCT,
    RALLY_ZONE_24H_PCT,
    RALLY_ZONE_MAX_PULLBACK_PCT,
    RALLY_ZONE_OVERRIDE_X,
    in_rally_zone,
    is_counter_trend_bounce,
)

# Real 2026-08-22 context: 24h high $78,511 (21:00 8/21), price $77,390 at
# the #172/#173 fires → 1.43% below the high, 24h change +2.54%.
HIGH_0822 = 78511.5
PX_0822 = 77390.4


def _pd(t24, price=PX_0822, high=HIGH_0822):
    return {"change_24h": t24, "price_usd": price, "high_24h": high}


def test_rally_zone_membership():
    assert in_rally_zone(_pd(2.54)) is True    # the 8/22 case (1.43% off high)
    assert in_rally_zone(_pd(2.0)) is True     # exactly at the 24h bar
    assert in_rally_zone(_pd(1.99)) is False
    assert in_rally_zone(_pd(-1.72)) is False  # #174, real rollover
    assert in_rally_zone({}) is False


def test_rally_zone_disarms_when_pullback_deepens():
    """The corroborating condition: a decline past RALLY_ZONE_MAX_PULLBACK_PCT
    off the 24h high is a reversal, not a short dip — zone OFF even though
    the 24h change is still positive, so full sensitivity returns."""
    bound = HIGH_0822 * (1 - RALLY_ZONE_MAX_PULLBACK_PCT / 100)
    assert in_rally_zone(_pd(2.54, price=bound)) is True         # exactly at bound
    assert in_rally_zone(_pd(2.54, price=bound - 1)) is False    # just past it
    # Rally +6% then -3.5% off the high: t24 still +2.3 but pullback deep.
    assert in_rally_zone(_pd(2.3, price=HIGH_0822 * 0.965)) is False


def test_rally_zone_degrades_safely():
    assert in_rally_zone({"change_24h": None}) is False
    assert in_rally_zone({"change_24h": "garbage"}) is False
    # Missing/zero high_24h → can't judge the pullback → NO suppression.
    assert in_rally_zone({"change_24h": 2.54, "price_usd": PX_0822}) is False
    assert in_rally_zone({"change_24h": 2.54, "price_usd": PX_0822, "high_24h": 0}) is False
    assert in_rally_zone({"change_24h": 2.54, "high_24h": HIGH_0822}) is False


def test_backtest_august_pullback_suppressed():
    """#172 (-1.40%/1m) and #173 (-2.13%/1h), both t1h=-1.37, t24=+2.54:
    legacy gate missed both (uptrend required t1h>=1.0 OR t24>=3.0, neither
    held). rally_zone catches them via the live 24h snapshot alone."""
    assert is_counter_trend_bounce("down", 1.40, -1.37, 2.54, rally_zone=True) is True
    assert is_counter_trend_bounce("down", 2.13, -1.37, 2.54, rally_zone=True) is True


def test_backtest_legacy_missed_this_without_the_flag():
    """Documents the actual bug: without rally_zone, neither alert was
    suppressed (t1h already flipped, t24 below the 3.0 general gate)."""
    assert is_counter_trend_bounce("down", 1.40, -1.37, 2.54) is False
    assert is_counter_trend_bounce("down", 2.13, -1.37, 2.54) is False


def test_backtest_rollover_still_fires_outside_zone():
    """#174 (-0.62%/5m): by then t24 had gone negative (-1.72%) — the rally
    had genuinely cooled into a rollover. in_rally_zone is False so the
    caller passes rally_zone=False and the alert fires normally."""
    assert in_rally_zone(_pd(-1.72, price=76060.4)) is False
    assert is_counter_trend_bounce("down", 0.62, -0.71, -1.72, rally_zone=False) is False


def test_genuine_reversal_still_fires_in_zone():
    # A real >=3% decline (== the scaled override) still fires.
    assert is_counter_trend_bounce("down", 3.0, -1.37, 2.54, rally_zone=True) is False


def test_up_moves_never_suppressed_in_rally_zone():
    # Continued upside during a rally must never be suppressed.
    assert is_counter_trend_bounce("up", 0.8, 0.3, 2.54) is False           # legacy: no downtrend anyway
    assert is_counter_trend_bounce("up", 0.8, 0.3, 2.54, rally_zone=True) is False
    # The real proof: inputs the LEGACY gate reads as a genuine DOWNTREND
    # (t1h -2.0, t24 -3.5) would be suppressed without the flag — the
    # rally_zone uptrend override must force them through.
    assert is_counter_trend_bounce("up", 0.8, -2.0, -3.5) is True              # legacy suppresses
    assert is_counter_trend_bounce("up", 0.8, -2.0, -3.5, rally_zone=True) is False


def test_event_mode_composes_with_rally_zone():
    # Event window (override_mult 0.6) inside the zone: bar = 1.5*2*0.6 = 1.8.
    assert is_counter_trend_bounce(
        "down", 2.0, -0.5, 2.5, override_mult=0.6, rally_zone=True
    ) is False  # 2.0 >= 1.8 → fires
    assert is_counter_trend_bounce(
        "down", 1.5, -0.5, 2.5, override_mult=0.6, rally_zone=True
    ) is True   # 1.5 < 1.8 → suppressed


def test_recovery_channel_invariant_pinned():
    """Mirrors bear_zone's invariant: scaled override <= 12h hard floor,
    so a >=3%/12h decline is always reported within one 12h window."""
    assert COUNTER_TREND_OVERRIDE_PCT * RALLY_ZONE_OVERRIDE_X <= HARD_FALLBACK_RETURN_12H_PCT
    assert is_counter_trend_bounce("down", 3.0, 0.0, 0.0, rally_zone=True) is False


def test_bear_zone_takes_precedence_over_rally_zone():
    """A fresh bounce off the YTD low that's already +2%/24h could satisfy
    BOTH zones. bear_zone's 'always alert on further decline near the lows'
    guarantee must win: down-moves must NOT be suppressed."""
    # bear_zone forces downtrend=True/never-suppress-down; rally_zone would
    # otherwise suppress this same move (uptrend=True with an unmet 3% bar).
    assert is_counter_trend_bounce(
        "down", 1.2, 0.5, 2.1, bear_zone=True, rally_zone=True
    ) is False  # NOT suppressed — bear_zone wins, decline is reported


def test_both_zones_up_move_uses_bear_zone_scaling():
    """When both are true, up-moves are governed by bear_zone's (larger)
    scaling, not silently let through by rally_zone's uptrend=True."""
    st_bear_only = is_counter_trend_bounce("up", 1.6, 0.5, 2.1, bear_zone=True)
    st_both = is_counter_trend_bounce("up", 1.6, 0.5, 2.1, bear_zone=True, rally_zone=True)
    assert st_bear_only == st_both  # rally_zone must not change bear_zone's up-side behavior


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
