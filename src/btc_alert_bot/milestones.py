"""Year-to-date-low milestone badge for alerts.

When BTC prints a new year-to-date low, an alert gets a prominent badge
line ("🔴 年初来最安値を更新（$XX,XXX）").

Two guards keep it meaningful and non-spammy:
  - Only from March onward (``YTD_BADGE_MIN_MONTH``). In Jan/Feb the
    "year-to-date low" is trivially broken because barely any of the year
    has elapsed, so badging then is noise.
  - At most once per ~month (``YTD_BADGE_COOLDOWN_DAYS``). During a single
    downtrend only the first break badges; a genuinely new low a month+
    later can badge again. The running low is still *tracked* in Jan/Feb
    and during the cooldown — only the badge text is withheld.

State persisted in ``state.json``:
  ytd_low_year       : int   — calendar year the running low belongs to
  ytd_low            : float — lowest USD price seen so far this year
  ytd_low_last_badge : str   — ISO ts of the last badge (cooldown anchor)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

log = logging.getLogger(__name__)

# Don't badge before this month — "年初来最安値" is meaningless in Jan/Feb
# when the year has barely started (any dip is a new "year-to-date low").
YTD_BADGE_MIN_MONTH = 3

# Minimum gap between YTD-low badges. Suppresses repeats during one
# downtrend while still allowing a fresh badge for a new low a month later.
YTD_BADGE_COOLDOWN_DAYS = 30


def ytd_low_badge(
    state: dict,
    price_usd: float,
    *,
    now: datetime | None = None,
    seed_year_low: Callable[[], float | None] | None = None,
) -> str:
    """Return the YTD-low badge when a new year-to-date low qualifies, else "".

    Mutates ``state`` to track the running low + the last-badge timestamp
    (caller persists ``state`` afterwards).

    Behavior:
    - Year boundary / first run: re-seed ``ytd_low`` from ``seed_year_low()``
      (historical low) capped at the current price. Never badges on a seed.
      If the seed can't be fetched, leave unseeded and retry next call
      (never seed from the current price alone — that would false-fire on
      any tiny dip).
    - New running low: always tracked. The badge text is returned only when
      ``now.month >= YTD_BADGE_MIN_MONTH`` AND at least
      ``YTD_BADGE_COOLDOWN_DAYS`` have passed since the last badge.
    """
    try:
        price = float(price_usd)
    except (TypeError, ValueError):
        return ""
    if price <= 0:
        return ""

    now = now or datetime.now(timezone.utc)
    year = now.year

    # Re-seed the running low at the calendar-year boundary so it tracks
    # only the current year. The last-badge timestamp is deliberately NOT
    # reset — the cooldown runs continuously across the boundary (and the
    # month gate already blocks Jan/Feb).
    if state.get("ytd_low_year") != year or state.get("ytd_low") is None:
        seed: float | None = None
        if seed_year_low is not None:
            try:
                seed = seed_year_low()
            except Exception as e:  # pragma: no cover - network/parse guard
                log.warning("YTD-low seed fetch failed: %s", e)
        if not seed:
            return ""  # no reliable baseline yet — retry next call
        state["ytd_low_year"] = year
        state["ytd_low"] = min(float(seed), price)
        log.info("YTD-low baseline seeded: $%,.0f (year %d)", state["ytd_low"], year)
        return ""

    try:
        ytd_low = float(state["ytd_low"])
    except (TypeError, ValueError):
        state["ytd_low"] = price
        return ""

    if price >= ytd_low:
        return ""  # not a new low

    # New running low — always track it, even in Jan/Feb or during cooldown.
    state["ytd_low"] = price

    # Gate the *badge*: only from March, and at most once per ~month.
    if now.month < YTD_BADGE_MIN_MONTH:
        return ""
    last = state.get("ytd_low_last_badge")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if now - last_dt < timedelta(days=YTD_BADGE_COOLDOWN_DAYS):
                return ""  # within the 1-month cooldown
        except Exception:
            pass
    state["ytd_low_last_badge"] = now.isoformat()
    log.info("YTD-low break — badging ($%,.0f, month=%d)", price, now.month)
    return f"🔴 年初来最安値を更新（${price:,.0f}）"


def forced_ytd_spike(features: dict) -> dict:
    """Synthesize a DOWN spike for a YTD-low override fire.

    Used when a first-time year-to-date-low break must fire even though the
    normal detector returned None (cooldown-suppressed, or no window
    threshold crossed). Picks the widest horizon with the largest move so
    the alert still conveys magnitude. Direction is always ``down`` — a new
    low is inherently a down event.
    """
    horizons = [
        ("12h", "return_12h"), ("2h", "return_2h"), ("1h", "return_1h"),
        ("15m", "return_15m"), ("5m", "return_5m"),
    ]
    window, change = "1h", 0.0
    for w, key in horizons:
        v = (features or {}).get(key)
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if abs(fv) > abs(change):
            window, change = w, fv
    return {
        "window": window,
        "change": change,
        "direction": "down",
        "score": None,
        "reasons": [
            "年初来最安値更新により強制発火（通常クールダウンを無視）",
            f"{window} {change:+.2f}%",
        ],
        "features": features or {},
    }
