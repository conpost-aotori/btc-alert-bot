"""Unit tests for the one-shot YTD-low emergency latch (YTD_ONESHOT).

Runs standalone:  python tests/test_ytd_oneshot.py
Also pytest-discoverable.

Guarantee under test:
  - YTD_ONESHOT unset/false  -> existing recurring behavior; the latch flag
    is never set and is ignored even if present (off-by-default, no change).
  - YTD_ONESHOT=true          -> the badge fires once, mark_ytd_badged latches
    `ytd_emergency_fired`, and every later call returns "" (never re-arms).
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc_alert_bot.milestones import (  # noqa: E402
    YTD_X_RETRY_MAX,
    mark_ytd_badged,
    ytd_commit_decision,
    ytd_commit_ok,
    ytd_low_badge,
)

_VARS = ("YTD_ONESHOT",)
NOW = datetime(2026, 6, 30, 13, 0, tzinfo=timezone.utc)  # June → past the March gate


@contextmanager
def env(**overrides):
    saved = {k: os.environ.get(k) for k in _VARS}
    try:
        for k in _VARS:
            os.environ.pop(k, None)
        for k, v in overrides.items():
            if v is not None:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _seeded_state(low=60000.0):
    # Pre-seeded so ytd_low_badge skips the seeding branch and evaluates the low.
    return {"ytd_low_year": 2026, "ytd_low": low}


def test_oneshot_fires_once_then_latches():
    with env(YTD_ONESHOT="true"):
        state = _seeded_state(60000.0)
        b1 = ytd_low_badge(state, 59000.0, now=NOW)
        assert "年初来最安値" in b1, f"expected a badge, got {b1!r}"

        mark_ytd_badged(state, 59000.0, now=NOW)
        assert state.get("ytd_emergency_fired") is True

        # A further new low must NOT badge again — latched for good.
        b2 = ytd_low_badge(state, 58000.0, now=NOW)
        assert b2 == "", f"expected latched empty, got {b2!r}"


def test_latch_ignored_when_oneshot_off():
    # Even with the flag already present, default mode ignores it and fires.
    with env():  # YTD_ONESHOT unset
        state = _seeded_state(60000.0)
        state["ytd_emergency_fired"] = True
        b = ytd_low_badge(state, 59000.0, now=NOW)
        assert "年初来最安値" in b, f"latch should be ignored when off, got {b!r}"


def test_mark_does_not_latch_when_off():
    with env():  # YTD_ONESHOT unset
        state = _seeded_state(60000.0)
        mark_ytd_badged(state, 59000.0, now=NOW)
        assert state.get("ytd_emergency_fired") is None  # no latch in default mode


def test_no_badge_when_not_a_new_low():
    # Sanity: price above the running low never badges (one-shot irrelevant).
    with env(YTD_ONESHOT="true"):
        state = _seeded_state(60000.0)
        assert ytd_low_badge(state, 61000.0, now=NOW) == ""


def test_ytd_commit_ok_truth_table():
    """X armed + configured → X delivery REQUIRED to commit (Discord-only
    success must NOT burn the one-shot). Otherwise legacy: any channel."""
    # (force_x, x_ready, d_disc, d_x) -> expected
    cases = [
        # X armed & keys present: only d_x commits
        ((True, True, True, False), False),   # Discord-only → keep pending
        ((True, True, False, True), True),
        ((True, True, True, True), True),
        ((True, True, False, False), False),
        # X armed but keys missing (misconfig): legacy — don't loop forever
        ((True, False, True, False), True),
        ((True, False, False, False), False),
        # X not armed: legacy
        ((False, True, True, False), True),
        ((False, False, False, True), True),
        ((False, False, False, False), False),
    ]
    for args, want in cases:
        got = ytd_commit_ok(*args)
        assert got is want, f"ytd_commit_ok{args} = {got}, want {want}"


def test_ytd_commit_decision_retry_cap():
    """X required + failing while Discord succeeds → bounded retries, then
    fall back to committing on the Discord success (dead keys / rate cap)."""
    st = {}
    for i in range(1, YTD_X_RETRY_MAX):
        assert ytd_commit_decision(st, True, True, True, False) is False
        assert st["ytd_x_retry_count"] == i
    assert ytd_commit_decision(st, True, True, True, False) is True  # cap hit
    assert "ytd_x_retry_count" not in st  # counter cleared


def test_ytd_commit_decision_resets_on_success():
    st = {"ytd_x_retry_count": 3}
    assert ytd_commit_decision(st, True, True, False, True) is True
    assert "ytd_x_retry_count" not in st


def test_ytd_commit_decision_total_failure_not_counted():
    # Neither channel delivered: existing 絶対投稿 retry, no counter (no spam).
    st = {}
    assert ytd_commit_decision(st, True, True, False, False) is False
    assert "ytd_x_retry_count" not in st


def test_x_configured_empty_string_not_configured():
    try:
        from btc_alert_bot.publishers import x_configured
    except ImportError:  # publisher deps (tweepy) absent locally — container has them
        print("        (skipped: publishers deps not installed)")
        return
    keys = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ[k] = "v"
        os.environ["X_API_KEY"] = ""   # present but empty = NOT configured
        assert x_configured() is False
        os.environ["X_API_KEY"] = "v"
        assert x_configured() is True
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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
