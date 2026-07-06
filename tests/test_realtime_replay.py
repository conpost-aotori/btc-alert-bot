"""Queue-and-replay for candles that arrive mid-detection — regression tests.

`RealtimeBot` serializes the composite (1m) and fast-track (1m/3m) pipelines
on purpose: both do a non-atomic load/mutate/save of state.json, so running
them concurrently would risk a lost update on cooldown/YTD/psych-level state.
Before this fix, a candle arriving in that ~0.5-1s overlap window was simply
dropped with a warning log. Now it's queued and replayed the instant the
in-flight pipeline finishes, so a genuine fast-track move can't be silently
lost. See realtime.py's `_pending_fast_track` / `_pending_composite` /
`_on_detection_done`.

Runs standalone:  python tests/test_realtime_replay.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc_alert_bot.realtime import RealtimeBot  # noqa: E402


def _confirmed_candle(channel: str, ts: int, open_p: float, close_p: float) -> str:
    """Build a WS message matching OKX's confirmed-candle shape."""
    row = [str(ts), str(open_p), str(open_p), str(open_p), str(close_p), "1", "1", "1", "1"]
    return json.dumps({"arg": {"channel": channel}, "data": [row]})


def test_busy_fast_track_candle_is_queued_and_replayed():
    """A 3m fast-track candle arriving mid-composite-detection must not be
    dropped — it should run right after the composite pass completes."""
    calls: list[str] = []

    async def scenario():
        bot = RealtimeBot()

        async def fake_composite():
            calls.append("composite-start")
            await asyncio.sleep(0.05)
            calls.append("composite-end")

        async def fake_fast_track(intra_pct, close_p, window):
            calls.append(f"fast-track-{window}-{intra_pct:+.2f}")

        bot._run_detection_async = fake_composite
        bot._run_fast_track_async = fake_fast_track

        # 1m candle, tiny move -> composite path, takes 50ms in this test.
        await bot._handle_message(_confirmed_candle("candle1m", 1, 100.0, 100.01))
        assert bot._detection_task is not None and not bot._detection_task.done()
        await asyncio.sleep(0)  # let the scheduled task actually start running

        # While composite is in flight, a 3m candle with a big move (+3%,
        # well over the 2.0% fast-track floor) arrives. Must be queued, not
        # dropped or run concurrently.
        await bot._handle_message(_confirmed_candle("candle3m", 2, 100.0, 103.0))
        assert bot._pending_fast_track == ("3m", 3.0, 103.0)
        assert calls == ["composite-start"]  # not launched yet — still queued

        await asyncio.sleep(0.2)  # let composite finish + replay run

        assert calls == ["composite-start", "composite-end", "fast-track-3m-+3.00"]
        assert bot._pending_fast_track is None

    asyncio.run(scenario())


def test_busy_composite_candle_is_queued_and_replayed():
    """A 1m composite candle arriving mid-fast-track must also be replayed,
    not dropped."""
    calls: list[str] = []

    async def scenario():
        bot = RealtimeBot()

        async def fake_fast_track(intra_pct, close_p, window):
            calls.append(f"fast-track-{window}-start")
            await asyncio.sleep(0.05)
            calls.append(f"fast-track-{window}-end")

        async def fake_composite():
            calls.append("composite")

        bot._run_fast_track_async = fake_fast_track
        bot._run_detection_async = fake_composite

        # 1m candle with a big move -> fast-track, takes 50ms in this test.
        await bot._handle_message(_confirmed_candle("candle1m", 1, 100.0, 101.0))
        assert bot._detection_task is not None and not bot._detection_task.done()

        # A subsequent 1m composite-only candle arrives before fast-track
        # finishes. Must be queued, not dropped.
        await bot._handle_message(_confirmed_candle("candle1m", 2, 100.0, 100.0))
        assert bot._pending_composite is True

        await asyncio.sleep(0.2)

        assert calls == ["fast-track-1m-start", "fast-track-1m-end", "composite"]
        assert bot._pending_composite is False

    asyncio.run(scenario())


def test_fast_track_pending_takes_priority_over_composite_pending():
    """If both a composite AND a fast-track candle get queued while busy
    (a slow detection spanning >1 candle close), the fast-track — a real
    move — must replay first; the composite replay follows after."""
    calls: list[str] = []

    async def scenario():
        bot = RealtimeBot()

        async def fake_composite():
            calls.append("composite-start")
            await asyncio.sleep(0.05)
            calls.append("composite-end")

        async def fake_fast_track(intra_pct, close_p, window):
            calls.append(f"fast-track-{window}")

        bot._run_detection_async = fake_composite
        bot._run_fast_track_async = fake_fast_track

        await bot._handle_message(_confirmed_candle("candle1m", 1, 100.0, 100.01))
        # Two candles arrive back-to-back while the first composite runs:
        # a small 1m composite candle, then a big 3m fast-track candle.
        await bot._handle_message(_confirmed_candle("candle1m", 2, 100.0, 100.02))
        await bot._handle_message(_confirmed_candle("candle3m", 3, 100.0, 103.0))
        assert bot._pending_composite is True
        assert bot._pending_fast_track == ("3m", 3.0, 103.0)

        await asyncio.sleep(0.3)  # composite -> fast-track replay -> composite replay

        assert calls == ["composite-start", "composite-end", "fast-track-3m", "composite-start", "composite-end"]

    asyncio.run(scenario())


def test_non_fast_track_candle_dropped_when_busy_and_not_composite_trigger():
    """A 3m candle that is NOT a fast-track move contributes nothing even
    when idle (see original behavior) — busy or not, it's a no-op, so it
    must not get queued (there's nothing meaningful to replay)."""
    calls: list[str] = []

    async def scenario():
        bot = RealtimeBot()

        async def fake_composite():
            calls.append("composite-start")
            await asyncio.sleep(0.05)
            calls.append("composite-end")

        bot._run_detection_async = fake_composite
        bot._run_fast_track_async = lambda *a: calls.append("fast-track")  # should never run

        await bot._handle_message(_confirmed_candle("candle1m", 1, 100.0, 100.01))
        # 3m candle, tiny move -> not fast-track, and 3m never drives composite.
        await bot._handle_message(_confirmed_candle("candle3m", 2, 100.0, 100.02))
        assert bot._pending_fast_track is None
        assert bot._pending_composite is False

        await asyncio.sleep(0.1)
        assert calls == ["composite-start", "composite-end"]

    asyncio.run(scenario())


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
