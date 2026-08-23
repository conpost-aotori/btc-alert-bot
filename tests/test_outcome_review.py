"""Outcome review (判定検証) — grading delivered alerts at +12h/+24h,
Discord-only notes, and the self-learning label store.

Uses a temp SQLite DB and injected price/post callables. No network.

Runs standalone:  python tests/test_outcome_review.py
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import os  # noqa: E402
import threading  # noqa: E402

from btc_alert_bot import history  # noqa: E402
from btc_alert_bot import outcome_review as orv  # noqa: E402
from btc_alert_bot.outcome_review import (  # noqa: E402
    HORIZONS_H,
    MAX_PRICE_FAILURES,
    NEUTRAL_BAND_PCT,
    POST_GRACE_H,
    build_note,
    classify_outcome,
    run_once,
)


def _reset_process_state():
    # Module-level failure budget / skip set persist across tests.
    orv._fail_counts.clear()
    orv._skipped.clear()

T0 = datetime(2026, 8, 22, 5, 31, tzinfo=timezone.utc)  # alert #173 fire time


# ---------------------------------------------------------------------------
# classify_outcome — pure
# ---------------------------------------------------------------------------

def test_classify_down_call():
    assert classify_outcome("down", -2.0) == "continued"
    assert classify_outcome("down", +2.0) == "reversed"
    assert classify_outcome("down", -0.2) == "flat"


def test_classify_up_call():
    assert classify_outcome("up", +1.0) == "continued"
    assert classify_outcome("up", -1.0) == "reversed"
    assert classify_outcome("up", 0.3) == "flat"


def test_classify_band_boundary():
    # Band is INCLUSIVE (matches the doc: ±0.5% 以内は中立).
    assert classify_outcome("down", -NEUTRAL_BAND_PCT) == "flat"
    assert classify_outcome("down", -(NEUTRAL_BAND_PCT + 0.01)) == "continued"


def test_classify_garbage_is_flat():
    assert classify_outcome("down", None) == "flat"
    assert classify_outcome("down", "x") == "flat"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _db_with_alert(ts=T0, direction="down", window="1h", change=-2.13,
                   price=77206.0, delivered=True):
    tmp = Path(tempfile.mkdtemp()) / "h.sqlite"
    history.init_db(tmp)
    aid = history.record_alert(
        tmp,
        price_data={"timestamp": ts.isoformat(), "price_usd": price},
        spike={"window": window, "change": change, "direction": direction,
               "score": 2.5, "reasons": ["r"], "features": {"atr_pct": 0.1}},
        factors=[],
        summary="s",
        delivered_discord=delivered,
        delivered_x=False,
    )
    return tmp, aid


def test_pending_respects_due_time_and_horizons():
    db, aid = _db_with_alert()
    # 11h later: nothing due.
    assert history.pending_outcome_checks(db, now=T0 + timedelta(hours=11), horizons_h=HORIZONS_H) == []
    # Exactly 12h later: NOT yet due — the 1m bar containing the due
    # instant must have closed first (+1 min).
    assert history.pending_outcome_checks(db, now=T0 + timedelta(hours=12), horizons_h=HORIZONS_H) == []
    p = history.pending_outcome_checks(db, now=T0 + timedelta(hours=12, minutes=1), horizons_h=HORIZONS_H)
    assert [(x["alert_id"], x["horizon_h"]) for x in p] == [(aid, 12)]
    # 25h later: both due, oldest-due first.
    p = history.pending_outcome_checks(db, now=T0 + timedelta(hours=25), horizons_h=HORIZONS_H)
    assert [x["horizon_h"] for x in p] == [12, 24]


def test_undelivered_alerts_are_never_graded():
    db, _ = _db_with_alert(delivered=False)
    assert history.pending_outcome_checks(db, now=T0 + timedelta(days=2), horizons_h=HORIZONS_H) == []


def test_record_is_idempotent():
    db, aid = _db_with_alert()
    kw = dict(alert_id=aid, horizon_h=12, due_ts=T0 + timedelta(hours=12),
              eval_ts=T0 + timedelta(hours=12, minutes=1), price_at_alert=77206.0,
              price_at_eval=77134.2, change_pct=-0.09, label="flat", posted=True)
    assert history.record_outcome(db, **kw) is True
    assert history.record_outcome(db, **kw) is False  # INSERT OR IGNORE → nothing inserted
    p = history.pending_outcome_checks(db, now=T0 + timedelta(hours=13), horizons_h=HORIZONS_H)
    assert p == []  # 12h now done, 24h not yet due
    stats = history.outcome_stats(db, days=30, now=T0 + timedelta(hours=13))
    assert stats[12]["n"] == 1 and stats[12]["flat"] == 1


# ---------------------------------------------------------------------------
# run_once — end to end with injected price + post
# ---------------------------------------------------------------------------

def _prices(at_alert, at_eval):
    def _p(ts):
        return at_alert if ts == T0 else at_eval
    return _p


def test_run_once_grades_posts_and_records():
    _reset_process_state()
    db, aid = _db_with_alert()  # down -2.13%/1h @ 77,206
    posted = []
    def _post(title, desc, **kw):
        posted.append((title, desc, kw))
        return True
    n = run_once(db, now=T0 + timedelta(hours=12, minutes=5),
                 price_at=_prices(77206.0, 75500.0), post=_post)
    assert n == 1
    assert len(posted) == 1
    title, desc, kw = posted[0]
    assert "#%d" % aid in title and "12h後" in title
    assert "正解" in desc and "-2.21%" in desc          # 75500/77206-1 = -2.21%
    assert kw.get("webhook_env") == "DISCORD_REVIEW_WEBHOOK_URL"  # Discord-only path
    stats = history.outcome_stats(db, days=30, now=T0 + timedelta(hours=13))
    assert stats[12]["continued"] == 1
    # posted flag flipped after the note went out
    import sqlite3
    with sqlite3.connect(str(db)) as c:
        assert c.execute("SELECT posted FROM alert_outcomes").fetchone()[0] == 1
    # Second tick: nothing pending, nothing posted again.
    assert run_once(db, now=T0 + timedelta(hours=12, minutes=6),
                    price_at=_prices(77206.0, 75500.0), post=_post) == 0
    assert len(posted) == 1


def test_run_once_late_backlog_is_silent():
    _reset_process_state()
    """Graded > POST_GRACE_H after due → recorded, NOT posted (no flood)."""
    db, aid = _db_with_alert()
    posted = []
    n = run_once(db, now=T0 + timedelta(hours=12 + POST_GRACE_H + 1),
                 price_at=_prices(77206.0, 78000.0), post=lambda *a, **k: posted.append(1) or True)
    assert n == 1
    assert posted == []
    st = history.outcome_stats(db, days=30, now=T0 + timedelta(days=1))
    assert st[12]["reversed"] == 1  # down call, price rose +1.03% → reversed


def test_run_once_missing_eval_price_leaves_pending():
    _reset_process_state()
    db, aid = _db_with_alert()
    n = run_once(db, now=T0 + timedelta(hours=12, minutes=5),
                 price_at=lambda ts: None, post=lambda *a, **k: True)
    assert n == 0
    # still pending for the next tick
    p = history.pending_outcome_checks(db, now=T0 + timedelta(hours=12, minutes=6), horizons_h=HORIZONS_H)
    assert len(p) == 1


def test_run_once_alert_end_price_missing_retries_no_cross_feed():
    """Same-instrument only: if OKX can't price the ALERT end, do NOT fall
    back to the CoinGecko fire price (cross-feed basis ≈ band width would
    bias labels). Leave pending instead."""
    _reset_process_state()
    db, aid = _db_with_alert(price=77206.0)
    def _p(ts):
        return 76000.0 if ts != T0 else None
    n = run_once(db, now=T0 + timedelta(hours=12, minutes=1), price_at=_p, post=lambda *a, **k: True)
    assert n == 0
    assert history.outcome_stats(db, days=30, now=T0 + timedelta(days=1)) == {}


def test_unpriceable_pairs_do_not_starve_newer_alerts():
    """8 permanently unpriceable OLD pairs at the head of the queue must not
    block a newer priceable alert forever (failure budget + skip set)."""
    _reset_process_state()
    db, _ = _db_with_alert(ts=T0 - timedelta(days=30))
    old_ts = T0 - timedelta(days=30)
    for i in range(3):  # 4 old alerts → 8 old pairs
        history.record_alert(
            db, price_data={"timestamp": (old_ts + timedelta(hours=i + 1)).isoformat(), "price_usd": 70000.0},
            spike={"window": "5m", "change": -1.0, "direction": "down", "score": 2.5, "reasons": [], "features": {}},
            factors=[], summary="s", delivered_discord=True, delivered_x=False,
        )
    new_aid = history.record_alert(
        db, price_data={"timestamp": T0.isoformat(), "price_usd": 77206.0},
        spike={"window": "1h", "change": -2.13, "direction": "down", "score": 2.5, "reasons": [], "features": {}},
        factors=[], summary="s", delivered_discord=True, delivered_x=False,
    )
    def _p(ts):
        return None if ts < T0 - timedelta(days=1) else 76000.0  # old ones unpriceable
    now = T0 + timedelta(hours=12, minutes=5)
    recorded = 0
    for _ in range(MAX_PRICE_FAILURES + 2):
        recorded += run_once(db, now=now, price_at=_p, post=lambda *a, **k: True)
    assert recorded == 1  # the new alert's 12h pair got graded
    st = history.outcome_stats(db, days=2, now=now)
    assert st[12]["n"] == 1
    assert len(orv._skipped) == 8


def test_in_grace_items_processed_before_backlog():
    """A timely note must never wait behind the oldest-first backlog."""
    _reset_process_state()
    db, _ = _db_with_alert(ts=T0 - timedelta(days=20))
    for i in range(12):  # lots of old backlog (all late → silent)
        history.record_alert(
            db, price_data={"timestamp": (T0 - timedelta(days=19, hours=i)).isoformat(), "price_usd": 70000.0},
            spike={"window": "5m", "change": -1.0, "direction": "down", "score": 2.5, "reasons": [], "features": {}},
            factors=[], summary="s", delivered_discord=True, delivered_x=False,
        )
    fresh = history.record_alert(
        db, price_data={"timestamp": T0.isoformat(), "price_usd": 77206.0},
        spike={"window": "1h", "change": -2.13, "direction": "down", "score": 2.5, "reasons": [], "features": {}},
        factors=[], summary="s", delivered_discord=True, delivered_x=False,
    )
    posted = []
    # One tick, max_items=2: the fresh in-grace pair must be in it.
    run_once(db, now=T0 + timedelta(hours=12, minutes=2), price_at=lambda ts: 76000.0,
             post=lambda t, d, **k: posted.append(t) or True, max_items=2)
    assert any(f"#{fresh}" in t for t in posted), posted


def test_concurrent_ticks_post_exactly_once():
    """Single-flight lock + insert-before-post: two overlapping run_once
    calls on the same due pair → one row, ONE note."""
    _reset_process_state()
    db, aid = _db_with_alert()
    posted = []
    gate = threading.Event()
    def slow_price(ts):
        gate.wait(timeout=2)  # hold the first tick inside pricing
        return 77206.0 if ts == T0 else 75500.0
    def _post(t, d, **k):
        posted.append(t); return True
    now = T0 + timedelta(hours=12, minutes=5)
    results = []
    t1 = threading.Thread(target=lambda: results.append(run_once(db, now=now, price_at=slow_price, post=_post)))
    t1.start()
    import time; time.sleep(0.2)
    # Second tick while the first is inside price_at → lock refuses it.
    results.append(run_once(db, now=now, price_at=slow_price, post=_post))
    gate.set(); t1.join()
    assert sorted(results) == [0, 1]
    assert len(posted) == 1


def test_failed_record_never_posts():
    """If the DB write fails, no note goes out (no 1/min duplicate loop)."""
    _reset_process_state()
    db, aid = _db_with_alert()
    posted = []
    orig = history.record_outcome
    history.record_outcome = lambda *a, **k: False
    try:
        for _ in range(3):
            run_once(db, now=T0 + timedelta(hours=12, minutes=5),
                     price_at=_prices(77206.0, 75500.0), post=lambda *a, **k: posted.append(1) or True)
    finally:
        history.record_outcome = orig
    assert posted == []


def test_dry_run_records_but_never_posts():
    _reset_process_state()
    db, aid = _db_with_alert()
    posted = []
    n = run_once(db, now=T0 + timedelta(hours=12, minutes=5),
                 price_at=_prices(77206.0, 75500.0), post=lambda *a, **k: posted.append(1) or True,
                 dry_run=True)
    assert n == 1 and posted == []
    import sqlite3
    with sqlite3.connect(str(db)) as c:
        assert c.execute("SELECT posted FROM alert_outcomes").fetchone()[0] == 0


def test_real_poster_hits_discord_webhook_only():
    """Behavioural Discord-only check with the DEFAULT poster: the HTTP
    call goes to the review webhook (fallback: main webhook) and nothing
    X-related is touched."""
    _reset_process_state()
    try:
        from btc_alert_bot import publishers
    except ImportError:
        print("        (skipped: publishers deps not installed)")
        return
    db, aid = _db_with_alert()
    calls = []
    class _Resp:
        status_code = 204
        def raise_for_status(self): pass
    orig_post = publishers.requests.post
    publishers.requests.post = lambda url, **kw: calls.append((url, kw.get("json"))) or _Resp()
    saved = {k: os.environ.get(k) for k in ("DISCORD_REVIEW_WEBHOOK_URL", "DISCORD_WEBHOOK_URL")}
    try:
        os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.test/main"
        os.environ.pop("DISCORD_REVIEW_WEBHOOK_URL", None)
        run_once(db, now=T0 + timedelta(hours=12, minutes=5), price_at=_prices(77206.0, 75500.0))
        assert len(calls) == 1
        assert calls[0][0] == "https://discord.test/main"          # fallback used
        assert "embeds" in calls[0][1] and "content" not in calls[0][1]
        # dedicated review channel takes precedence when set
        _reset_process_state()
        db2, _ = _db_with_alert()
        os.environ["DISCORD_REVIEW_WEBHOOK_URL"] = "https://discord.test/review"
        run_once(db2, now=T0 + timedelta(hours=12, minutes=5), price_at=_prices(77206.0, 75500.0))
        assert calls[-1][0] == "https://discord.test/review"
    finally:
        publishers.requests.post = orig_post
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_run_once_never_calls_x():
    """Structural: the reviewer imports no X poster at all."""
    import btc_alert_bot.outcome_review as m
    src = Path(m.__file__).read_text(encoding="utf-8")
    assert "post_x" not in src
    assert "tweepy" not in src


def test_build_note_shape():
    item = {"alert_id": 173, "horizon_h": 12, "direction": "down", "window": "1h",
            "change_pct": -2.13, "ts": T0}
    outcome = {"price_at_alert": 77206.0, "price_at_eval": 77134.2,
               "change_pct": -0.09, "label": "flat"}
    stats = {12: {"n": 10, "continued": 6, "reversed": 3, "flat": 1,
                  "by_window": {"1h": {"n": 4, "continued": 3}}}}
    title, desc, color = build_note(item, outcome, stats)
    assert title.startswith("🔍 判定検証 #173")
    assert "📉 下落 -2.13%/1h" in desc
    assert "中立" in desc
    assert "12h 60% (n=10)" in desc and "1h窓の12h 75% (n=4)" in desc
    assert color == 0x9E9E9E


def test_export_csv_joins_features_and_labels():
    _reset_process_state()
    db, aid = _db_with_alert()
    run_once(db, now=T0 + timedelta(hours=12, minutes=1),
             price_at=_prices(77206.0, 75500.0), post=lambda *a, **k: True)
    out = db.parent / "export.csv"
    n = history.export_outcomes_csv(db, out)
    assert n == 1
    text = out.read_text(encoding="utf-8").splitlines()
    header = text[0].split(",")
    assert "f_atr_pct" in header and "h12_label" in header and "h24_label" in header
    row = text[1].split(",")
    assert row[header.index("h12_label")] == "continued"
    assert row[header.index("h24_label")] == ""  # not yet graded


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
