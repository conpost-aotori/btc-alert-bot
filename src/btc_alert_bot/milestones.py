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
import os
from datetime import datetime, timedelta, timezone
from typing import Callable

log = logging.getLogger(__name__)

# Don't badge before this month — "年初来最安値" is meaningless in Jan/Feb
# when the year has barely started (any dip is a new "year-to-date low").
YTD_BADGE_MIN_MONTH = 3

# Minimum gap between YTD-low badges. Suppresses repeats during one
# downtrend while still allowing a fresh badge for a new low a month later.
YTD_BADGE_COOLDOWN_DAYS = 30

# Psychological round-number levels (USD). A downward break of one of these
# force-fires a 緊急暴落 alert (special exception — cooldown ignored, always
# posted), like the YTD-low. Configurable via env PSYCH_LEVELS (comma list).
# After a break fires, the level disarms and only re-arms once price recovers
# PSYCH_REARM_BUFFER_PCT above it — so oscillation right at the level doesn't
# spam.
PSYCH_REARM_BUFFER_PCT = 0.5


def _psych_levels() -> list[float]:
    raw = os.getenv("PSYCH_LEVELS", "60000")
    out: list[float] = []
    for x in raw.split(","):
        x = x.strip()
        if not x:
            continue
        try:
            out.append(float(x))
        except ValueError:
            continue
    return out


def psych_level_badge(
    state: dict, price_usd: float, *, levels: list[float] | None = None
) -> str:
    """Return a 🚨 badge when price has crossed DOWN through a psychological
    level (e.g. $60,000), else "".

    Mirrors the YTD-low retry pattern: does NOT disarm here — the caller
    commits via ``mark_psych_badged()`` only after a successful post, so a
    delivery failure retries on the next candle (絶対投稿). Re-arms a level
    only after price recovers PSYCH_REARM_BUFFER_PCT above it.
    """
    try:
        price = float(price_usd)
    except (TypeError, ValueError):
        return ""
    if price <= 0:
        return ""
    levels = levels if levels is not None else _psych_levels()
    armed = state.setdefault("psych_armed", {})
    broken: float | None = None
    for lv in levels:
        key = str(int(lv))
        if key not in armed:
            # First sighting: arm if currently at/above the level (so we only
            # fire on a fresh downward cross, not a level already below us).
            armed[key] = price >= lv
        elif price >= lv * (1 + PSYCH_REARM_BUFFER_PCT / 100):
            armed[key] = True  # clear recovery → re-arm
        if price < lv and armed.get(key):
            if broken is None or lv > broken:
                broken = lv  # report the highest level breached
    if broken is None:
        return ""
    return f"🚨 BTC ${broken:,.0f}割れ — 心理的節目を下抜け"


def mark_psych_badged(
    state: dict, price_usd: float, *, levels: list[float] | None = None
) -> None:
    """Disarm every level the price is now below, after a successful post."""
    try:
        price = float(price_usd)
    except (TypeError, ValueError):
        return
    levels = levels if levels is not None else _psych_levels()
    armed = state.setdefault("psych_armed", {})
    for lv in levels:
        if price < lv:
            armed[str(int(lv))] = False


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
    - One-shot mode (``YTD_ONESHOT=true``): once the YTD-low emergency has
      fired and been delivered once, never badge again (the running low is
      still tracked). Re-arm by clearing ``ytd_emergency_fired`` in
      state.json or unsetting the env. Default off → recurring behavior.
    """
    # One-shot latch (user opt-in "今回限り"): suppress the badge for good
    # once it has fired. Checked first so it short-circuits all other work.
    if os.getenv("YTD_ONESHOT", "false").lower() == "true" and state.get(
        "ytd_emergency_fired"
    ):
        return ""
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

    # New running low. Decide whether to BADGE or just silently track.
    in_cooldown = False
    last = state.get("ytd_low_last_badge")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            in_cooldown = now - last_dt < timedelta(days=YTD_BADGE_COOLDOWN_DAYS)
        except Exception:
            in_cooldown = False

    if now.month < YTD_BADGE_MIN_MONTH or in_cooldown:
        # Track the running low silently (keeps the year-low reference
        # accurate) but withhold the badge — Jan/Feb, or within the
        # 1-month cooldown after a previous badge.
        state["ytd_low"] = price
        return ""

    # Qualifies to badge. Deliberately do NOT mutate ytd_low / last_badge
    # here: the caller commits via mark_ytd_badged() ONLY after a successful
    # post. So if delivery fails, the next candle re-evaluates (price is
    # still < ytd_low) and retries — the YTD-low badge is GUARANTEED to
    # post (絶対投稿), with the normal cooldown ignored.
    log.info("YTD-low break — badge pending ($%,.0f, month=%d)", price, now.month)
    return f"🔴 年初来最安値を更新（${price:,.0f}）"


# Max consecutive candles the X-required commit may retry while X delivery
# keeps failing but Discord succeeds. Keys can be present yet dead (revoked
# token, X free-tier 500 posts/month cap) — unbounded ~1/min Discord+LLM+X
# retries during a crash would be far worse than a missed tweet.
YTD_X_RETRY_MAX = 5


def ytd_commit_decision(
    state: dict, force_x: bool, x_ready: bool, d_disc: bool, d_x: bool
) -> bool:
    """``ytd_commit_ok`` with a persistent, bounded retry counter.

    When X delivery is required (armed + keys present) but X keeps failing
    while Discord succeeds, allow up to ``YTD_X_RETRY_MAX`` retry candles,
    then log an error and fall back to committing on the Discord success so
    the bulletin doesn't loop forever. The counter lives in ``state``
    (persisted by the caller) and resets on any commit.
    """
    if ytd_commit_ok(force_x, x_ready, d_disc, d_x):
        state.pop("ytd_x_retry_count", None)
        return True
    if force_x and x_ready and d_disc and not d_x:
        try:
            n = int(state.get("ytd_x_retry_count", 0) or 0) + 1
        except (TypeError, ValueError):
            n = 1
        state["ytd_x_retry_count"] = n
        if n >= YTD_X_RETRY_MAX:
            log.error(
                "YTD-low X delivery failed %d times — committing on the "
                "Discord success alone (check X keys / monthly post cap)", n,
            )
            state.pop("ytd_x_retry_count", None)
            return True
        log.warning(
            "YTD-low X delivery failed (attempt %d/%d) — badge stays "
            "pending, will retry next candle", n, YTD_X_RETRY_MAX,
        )
    return False


def ytd_commit_ok(
    force_x: bool, x_ready: bool, d_disc: bool, d_x: bool
) -> bool:
    """Whether the YTD-low badge may be committed (one-shot latch + cooldown
    stamp) after a post attempt.

    When the X emergency path is armed (``ENABLE_X_YTD_LOW``) AND X is
    actually configured, **X delivery is required**: a Discord-only success
    must NOT burn the ``YTD_ONESHOT`` latch, or the single X shot would be
    consumed without a tweet. The badge then stays pending and the forced
    fire retries on the next candle (Discord may repeat until X succeeds —
    X is the deliverable). When X isn't armed, or is armed but has no keys
    (misconfiguration), any successful channel commits (legacy behavior),
    so the bulletin doesn't loop forever on a channel that can never work.
    """
    if force_x and x_ready:
        return d_x
    return d_disc or d_x


def mark_ytd_badged(state: dict, price_usd: float, *, now: datetime | None = None) -> None:
    """Commit a successfully-posted YTD-low badge: advance the low + stamp the
    cooldown anchor. Called by the alert path ONLY after a successful post, so
    a delivery failure leaves the badge pending and it retries next candle.
    """
    now = now or datetime.now(timezone.utc)
    try:
        state["ytd_low"] = float(price_usd)
    except (TypeError, ValueError):
        pass
    state["ytd_low_last_badge"] = now.isoformat()
    # Latch the one-shot emergency (only consulted when YTD_ONESHOT=true, so
    # this is a harmless no-op in the default recurring mode).
    if os.getenv("YTD_ONESHOT", "false").lower() == "true":
        state["ytd_emergency_fired"] = True


def forced_ytd_spike(features: dict, price_data: dict | None = None) -> dict:
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
    # Features empty (REST snapshot failed → legacy path): fall back to the
    # 1h change from the price feed so the alert still shows a real number.
    if abs(change) < 0.01 and price_data is not None:
        ch = price_data.get("change_1h")
        try:
            change = float(ch)
            window = "1h"
        except (TypeError, ValueError):
            pass
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
