"""Bear-zone (安値圏) counter-trend suppression — regression tests.

Backtests the exact 2026-07-01/02 noise: five +2%-class dead-cat bounces
fired as 暴騰 alerts while price sat 3-6% above the fresh YTD low, because
the 24h trend had already flipped positive (+1.8..+3.8%) so the legacy
counter-trend gate never engaged. In the bear zone the regime is treated
as DOWN: up-moves need 2x the reversal override (3.0%), the momentum
exception is off, and down-moves are never suppressed.

Runs standalone:  python tests/test_bear_zone.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc_alert_bot.detector import (  # noqa: E402
    BEAR_ZONE_PROX_PCT,
    BEAR_ZONE_OVERRIDE_X,
    COUNTER_TREND_OVERRIDE_PCT,
    HARD_FALLBACK_RETURN_12H_PCT,
    in_bear_zone,
    is_counter_trend_bounce,
)

LOW = 57892.0  # the VM's tracked YTD low as of 2026-07-06
CEIL = LOW * (1 + BEAR_ZONE_PROX_PCT / 100)  # 62,523.36
NOW = datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)  # July → month gate open


def _st():
    return {"ytd_low": LOW, "ytd_low_year": 2026}


def test_zone_membership():
    st = _st()
    assert in_bear_zone(st, 59541.0, now=NOW) is True   # #100 (2.8% above)
    assert in_bear_zone(st, 61175.0, now=NOW) is True   # #106 (5.7% above)
    assert in_bear_zone(st, CEIL, now=NOW) is True      # exactly at ceiling
    assert in_bear_zone(st, CEIL - 1, now=NOW) is True  # just inside
    assert in_bear_zone(st, CEIL + 1, now=NOW) is False  # just outside
    assert in_bear_zone(st, 66000.0, now=NOW) is False  # normal regime
    assert in_bear_zone(st, LOW - 500, now=NOW) is True  # below the low still bear


def test_zone_degrades_safely_on_missing_state():
    assert in_bear_zone({}, 60000.0, now=NOW) is False
    assert in_bear_zone({"ytd_low": None, "ytd_low_year": 2026}, 60000.0, now=NOW) is False
    assert in_bear_zone({"ytd_low": "garbage", "ytd_low_year": 2026}, 60000.0, now=NOW) is False
    assert in_bear_zone(_st(), 0.0, now=NOW) is False


def test_zone_never_arms_in_january_fresh_seed():
    """January's 'year low' is days-old data: an ATH bull at $118k would sit
    within 8% of it and wrongly arm the zone. The month gate blocks that."""
    jan = datetime(2027, 1, 10, tzinfo=timezone.utc)
    st = {"ytd_low": 117000.0, "ytd_low_year": 2027}
    assert in_bear_zone(st, 118000.0, now=jan) is False


def test_zone_never_arms_off_stale_prior_year_low():
    """A 2026 low surviving a failed January reseed must not arm the zone
    in 2027 (year mismatch), whatever the month."""
    mar = datetime(2027, 3, 15, tzinfo=timezone.utc)
    st = {"ytd_low": LOW, "ytd_low_year": 2026}
    assert in_bear_zone(st, 60000.0, now=mar) is False


def test_recovery_channel_invariant_pinned():
    """The 12h status report (3.0% floor) must remain reachable in-zone:
    scaled override <= 12h hard floor, and a move exactly at the scaled
    override FIRES. Breaking either silently unbounds recovery latency."""
    assert COUNTER_TREND_OVERRIDE_PCT * BEAR_ZONE_OVERRIDE_X <= HARD_FALLBACK_RETURN_12H_PCT
    assert is_counter_trend_bounce("up", 3.0, 0.0, 0.0, bear_zone=True) is False


def test_backtest_july_bounces_suppressed():
    """The four noise bounces from 7/1-7/2 are suppressed in the zone."""
    # (move, t1h, t24) reconstructed from OKX 1H closes at fire time
    noise = [
        (2.10, 0.08, 1.81),   # #100 — also proves t24>0 no longer bypasses
        (1.02, 1.02, 3.02),   # #101 — momentum exception must NOT save it
        (2.01, -0.54, 3.21),  # #104
        (2.00, 1.09, 3.79),   # #106
    ]
    for move, t1h, t24 in noise:
        assert is_counter_trend_bounce(
            "up", move, t1h, t24, bear_zone=True
        ) is True, f"({move}, {t1h}, {t24}) should be suppressed"


def test_backtest_big_recovery_still_fires():
    # #103: +3.04%/12h ≥ 3.0% scaled override → genuine recovery, fires.
    assert is_counter_trend_bounce("up", 3.04, 1.13, 3.70, bear_zone=True) is False


def test_down_moves_never_suppressed_in_zone():
    # Without the zone, t24=+3.79 reads as an uptrend and a -1.2% down move
    # would be suppressed as counter-trend. Near the lows that's wrong —
    # downs are regime-aligned and must always evaluate.
    assert is_counter_trend_bounce("down", -1.2, 1.09, 3.79) is True   # legacy
    assert is_counter_trend_bounce("down", -1.2, 1.09, 3.79, bear_zone=True) is False


def test_out_of_zone_behavior_unchanged():
    # Same July inputs without the zone flag: legacy gates see no downtrend
    # (t24 positive) → not suppressed. Documents the old blind spot.
    assert is_counter_trend_bounce("up", 2.10, 0.08, 1.81) is False
    # Classic legacy suppression still works: small bounce mid-crash.
    assert is_counter_trend_bounce("up", 0.7, -2.0, -6.0) is True


def test_event_mode_composes_with_zone():
    # Event window (override_mult 0.6) inside the zone: bar = 1.5*2*0.6 = 1.8.
    assert is_counter_trend_bounce(
        "up", 2.0, 0.5, 2.0, override_mult=0.6, bear_zone=True
    ) is False  # 2.0 >= 1.8 → fires
    assert is_counter_trend_bounce(
        "up", 1.5, 0.5, 2.0, override_mult=0.6, bear_zone=True
    ) is True   # 1.5 < 1.8 → suppressed


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
