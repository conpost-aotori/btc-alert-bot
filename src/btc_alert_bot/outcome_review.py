"""Outcome review (判定検証): grade every delivered alert's direction call
at +12h and +24h, post a short review note to DISCORD ONLY, and persist the
label as self-learning data.

Why: an alert is a directional "call" (📉 down / 📈 up). Whether price kept
going that way afterwards is the ground truth the detector is ultimately
tuned against. Storing it per alert, joined with the features recorded at
fire time (``alerts.features_json``), gives a labeled dataset for tuning
thresholds and the bear/rally zones on evidence instead of anecdotes.

Pipeline (runs in a background task every REVIEW_POLL_S, see realtime.py):
  1. history.pending_outcome_checks — delivered alerts whose +h is due.
  2. Re-price BOTH ends on OKX (fetch_close_at at alert ts and at +h) so
     the change is strictly same-instrument. If either price is missing
     the pair is left pending and retried next tick (no CoinGecko mixing
     — a cross-feed basis of up to ~0.3% would bias labels toward
     "continued" inside the 0.5% band). After MAX_PRICE_FAILURES tries
     the pair is skipped for the process lifetime so it can never starve
     newer alerts.
  3. classify_outcome — continued / reversed / flat (neutral band).
  4. history.record_outcome FIRST (INSERT OR IGNORE; True only if a row
     was actually inserted) — this is the gate for step 5, so a pair can
     never be announced twice even if two ticks overlap.
  5. Post a Discord note ONLY if (a) the row was just inserted and (b) the
     check ran within POST_GRACE_H of the due time; then mark posted=1.
     A late backlog (container was down, or the very first run after
     this feature shipped) is graded silently so the channel isn't
     flooded with dozens of stale reviews. In-grace pairs are always
     processed BEFORE backlog so backfill can't delay a timely note.
     DRY_RUN=true records but never posts. X is never involved.
  A non-blocking lock makes run_once single-flight: asyncio.wait_for
  cannot stop a to_thread worker, so a slow OKX tick must not overlap
  the next one.

Env:
  ENABLE_OUTCOME_REVIEW       default "true"; "false" disables the task.
  DISCORD_REVIEW_WEBHOOK_URL  optional dedicated channel; falls back to
                              DISCORD_WEBHOOK_URL.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import history
from .market import fetch_close_at
from .publishers import _format_jst, post_discord_note

log = logging.getLogger(__name__)

# Horizons graded, in hours (single source of truth in history.py so the
# CSV export schema stays fixed).
HORIZONS_H: tuple[int, ...] = history.DEFAULT_HORIZONS_H

# |change| within this band (inclusive) is "flat" — neither a win nor a
# loss for the call. 0.5% is roughly BTC's typical 1h noise; a 12-24h
# drift inside it says nothing about the alert's direction either way.
NEUTRAL_BAND_PCT = 0.5

# Consecutive pricing failures after which a (alert, horizon) pair is
# skipped for the rest of the process lifetime. Without this, 8 pairs that
# OKX can never price would sit at the head of the oldest-first queue and
# silently starve every newer alert forever.
MAX_PRICE_FAILURES = 3

# Post the Discord note only if graded within this many hours of the due
# time; later = silent backfill (still recorded).
POST_GRACE_H = 3.0

# Backlog throttle: max (alert, horizon) pairs graded per poll tick. Each
# needs up to 2 OKX calls (4 if both fall through to history-candles);
# 8/min drains a 170-alert backlog in ~45 min without hammering the API
# or starving the detection thread.
MAX_PER_TICK = 8

# Single-flight guard + per-pair failure budget (process lifetime).
_tick_lock = threading.Lock()
_fail_counts: dict[tuple[int, int], int] = {}
_skipped: set[tuple[int, int]] = set()

# Poll cadence for the background task.
REVIEW_POLL_S = 60.0

# Stats window shown in each note.
STATS_DAYS = 30

LABEL_JA = {
    "continued": "✅ 正解（継続）",
    "reversed": "❌ 不正解（反転）",
    "flat": "➖ 中立（横ばい）",
}
COLOR = {"continued": 0x00C853, "reversed": 0xD32F2F, "flat": 0x9E9E9E}


def enabled() -> bool:
    return os.getenv("ENABLE_OUTCOME_REVIEW", "true").strip().lower() != "false"


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------

def classify_outcome(
    direction: str, change_pct: float, *, band_pct: float = NEUTRAL_BAND_PCT
) -> str:
    """Grade a direction call against the later price change.

    ``change_pct`` is signed (price_at_eval / price_at_alert - 1) * 100.
    For a 'down' call a further fall is 'continued'; for 'up' a further
    rise. Within ±band it's 'flat'.
    """
    try:
        c = float(change_pct)
    except (TypeError, ValueError):
        return "flat"
    if abs(c) <= band_pct:
        return "flat"
    signed = c if direction == "up" else -c
    return "continued" if signed > 0 else "reversed"


def build_note(item: dict, outcome: dict, stats: dict) -> tuple[str, str, int]:
    """``(title, description, color)`` for the Discord review note."""
    h = item["horizon_h"]
    d = item["direction"]
    arrow = "📈 上昇" if d == "up" else "📉 下落"
    title = f"🔍 判定検証 #{item['alert_id']} — {h}h後"
    fired = _format_jst(item["ts"].isoformat()) or item["ts"].isoformat()
    label = outcome["label"]
    lines = [
        f"{arrow} {item['change_pct']:+.2f}%/{item['window']} @ ${outcome['price_at_alert']:,.0f}"
        f"（{fired}）",
        f"→ {h}h後 ${outcome['price_at_eval']:,.0f}（{outcome['change_pct']:+.2f}%）"
        f"  {LABEL_JA.get(label, label)}",
    ]
    # Running accuracy so the note doubles as a scoreboard.
    parts = []
    for hh in sorted(stats):
        st = stats[hh]
        if st.get("n"):
            parts.append(f"{hh}h {st['continued'] / st['n'] * 100:.0f}% (n={st['n']})")
    win = stats.get(h, {}).get("by_window", {}).get(item["window"])
    if win and win.get("n"):
        parts.append(f"{item['window']}窓の{h}h {win['continued'] / win['n'] * 100:.0f}% (n={win['n']})")
    if parts:
        lines.append(f"直近{STATS_DAYS}日 正解率: " + " ／ ".join(parts))
    return title, "\n".join(lines), COLOR.get(label, 0x9E9E9E)


# ---------------------------------------------------------------------------
# One scheduler tick
# ---------------------------------------------------------------------------

def run_once(
    db_path: Path,
    *,
    now: datetime | None = None,
    price_at: Callable[[datetime], float | None] = fetch_close_at,
    post: Callable[..., bool] = post_discord_note,
    max_items: int = MAX_PER_TICK,
    dry_run: bool | None = None,
) -> int:
    """Grade due (alert, horizon) pairs. Returns how many were recorded.

    ``price_at`` / ``post`` are injectable for tests. Never raises — every
    failure is logged and the item is left pending for the next tick.
    Single-flight: if a previous tick is still running, returns 0.
    """
    if not _tick_lock.acquire(blocking=False):
        log.warning("outcome: previous tick still running — skipping this one")
        return 0
    try:
        return _run_once_locked(
            db_path, now=now, price_at=price_at, post=post,
            max_items=max_items, dry_run=dry_run,
        )
    finally:
        _tick_lock.release()


def _run_once_locked(
    db_path: Path,
    *,
    now: datetime | None,
    price_at: Callable[[datetime], float | None],
    post: Callable[..., bool],
    max_items: int,
    dry_run: bool | None,
) -> int:
    now = now or datetime.now(timezone.utc)
    if dry_run is None:
        dry_run = os.getenv("DRY_RUN", "false").strip().lower() == "true"
    # Whole due set (cheap: one indexed read), so prioritisation below
    # sees every in-grace pair no matter how deep the backlog is.
    items = history.pending_outcome_checks(db_path, now=now, horizons_h=HORIZONS_H)
    if not items:
        return 0
    # Drop pairs that exhausted their failure budget, then put timely
    # (in-grace) pairs first so a backlog can never delay a live note.
    items = [it for it in items if (it["alert_id"], it["horizon_h"]) not in _skipped]

    def _late_h(it: dict) -> float:
        return (now - it["due"]).total_seconds() / 3600.0

    items.sort(key=lambda it: (0 if _late_h(it) <= POST_GRACE_H else 1, it["due"]))
    items = items[:max_items]

    done = 0
    for it in items:
        key = (it["alert_id"], it["horizon_h"])
        try:
            p_eval = price_at(it["due"])
            p_alert = price_at(it["ts"]) if (p_eval and p_eval > 0) else None
            if not p_eval or p_eval <= 0 or not p_alert or p_alert <= 0:
                n_fail = _fail_counts.get(key, 0) + 1
                _fail_counts[key] = n_fail
                if n_fail >= MAX_PRICE_FAILURES:
                    _skipped.add(key)
                    log.error(
                        "outcome: alert #%s +%dh unpriceable after %d tries — "
                        "skipping for this process lifetime",
                        it["alert_id"], it["horizon_h"], n_fail,
                    )
                else:
                    log.warning(
                        "outcome: no OKX price for alert #%s +%dh (try %d/%d) — retry next tick",
                        it["alert_id"], it["horizon_h"], n_fail, MAX_PRICE_FAILURES,
                    )
                continue
            _fail_counts.pop(key, None)

            change = (p_eval / p_alert - 1.0) * 100.0
            label = classify_outcome(it["direction"], change)
            in_grace = _late_h(it) <= POST_GRACE_H

            # Persist FIRST. True only if this call inserted the row — the
            # gate that makes a duplicate note impossible.
            inserted = history.record_outcome(
                db_path,
                alert_id=it["alert_id"], horizon_h=it["horizon_h"],
                due_ts=it["due"], eval_ts=now,
                price_at_alert=p_alert, price_at_eval=p_eval,
                change_pct=change, label=label, posted=False,
            )
            if not inserted:
                continue
            done += 1

            posted = False
            if in_grace and not dry_run:
                stats = history.outcome_stats(db_path, days=STATS_DAYS, now=now)
                title, desc, color = build_note(
                    it,
                    {"price_at_alert": p_alert, "price_at_eval": p_eval,
                     "change_pct": change, "label": label},
                    stats,
                )
                posted = bool(post(
                    title, desc, color=color, webhook_env="DISCORD_REVIEW_WEBHOOK_URL"
                ))
                if posted:
                    history.mark_outcome_posted(
                        db_path, alert_id=it["alert_id"], horizon_h=it["horizon_h"]
                    )
            log.info(
                "outcome: #%s +%dh %s %+.2f%% → %s%s",
                it["alert_id"], it["horizon_h"], it["direction"], change, label,
                "" if posted else (
                    " (dry-run, not posted)" if (in_grace and dry_run)
                    else (" (backfill, not posted)" if not in_grace else " (post failed)")
                ),
            )
        except Exception:
            log.exception(
                "outcome: failed on alert #%s +%dh", it.get("alert_id"), it.get("horizon_h")
            )
    return done
