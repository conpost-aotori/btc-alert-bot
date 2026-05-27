"""Composite-score spike detection backed by a rolling feature history.

Layered design (matches the Codex consultation):

1. Compute current market features via features.compute_market_features().
2. Robust-z each feature against the on-disk feature history ring buffer.
3. Combine the z-scores into a single composite score.
4. Fire if score >= threshold AND a hard floor on |return_15m| is crossed
   AND volatility/volume confirm the move (anti-fakeout filters).
5. Apply directional cooldown so the same move doesn't fire twice.

The legacy CoinGecko-only path (1h/24h % thresholds) is kept as a fallback
for cases where Bybit data is missing — better to alert than to go dark.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .features import (
    HIST_LOOKBACK_BARS,
    adaptive_return_floor,
    clipped_z,
    history_field,
)

log = logging.getLogger(__name__)

# --- Legacy fallback thresholds (used if Bybit features are unavailable) ---
# 24h was removed at the user's request — only fire on horizons that
# correspond to actionable spikes (≤1h).
THRESHOLD_1H_PCT = 2.5

# --- Composite-score weights & gates (Phase 1 initial values from Codex) ---
SCORE_WEIGHTS = {
    "move_per_atr": 0.30,
    "atr_pct":      0.25,
    "volume_5bar":  0.20,
    "oi_drop":      0.15,   # only counts negative OI change
    "funding_abs":  0.10,
}

# Hard fire conditions (all must hold).
# Score lowered again per user "もうちょっと下げよう" (2.7 → 2.4).
FIRE_SCORE_MIN = 2.4
FIRE_ATR_Z_MIN = 1.2
FIRE_VOL_Z_MIN = 1.5

# Per-window return floors — second round of sensitivity bumps. Hierarchy
# (1m << 3m / 5m < 15m) still preserved.
FIRE_RETURN_5M_MIN_PCT = 0.5
FIRE_RETURN_15M_MIN_PCT = 1.5

# Adaptive reference. ``features["atr_pct"]`` is already per-5-min-bar ATR
# regardless of which return horizon we're testing, so both 5m and 15m
# adaptive floors scale against the same baseline. The timeframe-specific
# part is already encoded in the *base* floor (FIRE_RETURN_5M_MIN_PCT vs
# FIRE_RETURN_15M_MIN_PCT). A previous version divided this by sqrt(3)
# for 5m, which roughly doubled the 5m floor and made the new path
# subsumed by the hard fallback.
ADAPTIVE_REF_ATR_PCT_5M = 0.10
ADAPTIVE_REF_ATR_PCT_15M = 0.10

# "Always alert" overrides — catch obvious moves even with thin history.
# Each hard floor sits ABOVE the corresponding composite-gate base so a
# threshold-only path can't quietly bypass the composite tightening.
# 24h was removed at the user's request (no need to alert on multi-day moves).
HARD_FALLBACK_RETURN_5M_PCT = 1.0
HARD_FALLBACK_RETURN_15M_PCT = 2.0
HARD_FALLBACK_RETURN_1H_PCT = 2.5

# Cooldown: same direction is suppressed longer than a reversal.
# Per-tier durations let the medium tier (15m) stay quieter than the
# responsive short tier (1m/3m/5m) — the user reported 15m firing back-to-
# back on the same sustained trend, so medium gets a 3-hour cooldown.
COOLDOWN_SAME_DIR_MIN = 90  # default fallback
# Reversal cooldown raised 30 → 60 min to suppress whiplash alerts when
# the price oscillates around a level (was firing up + down + up...).
COOLDOWN_OPP_DIR_MIN = 60
COOLDOWN_BY_TIER_MIN: dict[str, int] = {
    "short": 90,
    "medium": 180,  # 3 hours — discourage 15m back-to-back on same trend
    "long": 90,
}

# ---------------------------------------------------------------------------
# Timeframe tiers — controls how alerts of different windows suppress each
# other. Per the user's design:
#   short  (1m/3m/5m) : the primary detection band
#   medium (15m)       : suppressed when a recent short-tier alert exists
#   long   (1h)        : independent — fires only on large moves
# Anything longer than 1h does not generate alerts at all.
# ---------------------------------------------------------------------------
WINDOW_TIER: dict[str, str] = {
    "1m": "short",
    "3m": "short",
    "5m": "short",
    "15m": "medium",
    "1h": "long",
}
TIER_RANK = {"short": 0, "medium": 1, "long": 2}

# Intra-tier window rank — used by _is_suppressed() to enforce the
# user's rule that *faster windows preempt slower windows* but not the
# other way around. A 1m alert silences subsequent same-direction 3m/5m
# alerts (no point repeating the news), but a fresh 1m fast-track AFTER
# a 5m alert is still allowed through as the "急変動" override.
WINDOW_RANK: dict[str, int] = {
    "1m": 0,
    "3m": 1,
    "5m": 2,
    "15m": 0,   # only one window in the medium tier
    "1h":  0,   # only one window in the long tier
}

# Ring buffer: how many feature snapshots to retain in state.json.
# ~48h of 1-minute features (was *2 of HIST_LOOKBACK_BARS when sampling
# was 5-min; same multiplier still keeps 48h after 1m switch).
FEATURE_HISTORY_MAX = HIST_LOOKBACK_BARS * 2


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Failed to parse %s: %s — starting fresh", path, e)
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def record_alert_in_state(state: dict, spike: dict, price_data: dict) -> None:
    """Update ``state`` with both a tier-keyed alert record and the legacy flat fields.

    The tier-keyed entries (``last_alert_short_*``, ``last_alert_medium_*``,
    ``last_alert_long_*``) drive the new cooldown logic. The flat
    ``last_alert_*`` fields stay for backwards compatibility (legacy
    consumers + Codex audit trail).
    """
    window = spike.get("window", "")
    direction = spike.get("direction", "up")
    tier = WINDOW_TIER.get(window)
    ts = price_data.get("timestamp")
    if tier and ts:
        state[f"last_alert_{tier}_time"] = ts
        state[f"last_alert_{tier}_direction"] = direction
        state[f"last_alert_{tier}_window"] = window
    state.update({
        "last_alert_time": ts,
        "last_alert_price": price_data.get("price_usd"),
        "last_alert_direction": direction,
        "last_spike_window": window,
        "last_spike_change": spike.get("change"),
        "last_spike_score": spike.get("score"),
    })


def append_feature_history(state: dict, features: dict) -> None:
    """Append the latest features to the ring buffer in state.

    Deduplicates by ``ts`` against the most recent entry: in WS realtime
    mode a reconnect causes OKX to re-send the most recent closed candle,
    which would otherwise inflate the ring buffer with duplicates and
    skew z-score statistics.
    """
    if not features:
        return
    hist = state.setdefault("feature_history", [])
    new_ts = features.get("ts")
    if new_ts and hist and hist[-1].get("ts") == new_ts:
        # Same candle as last time — silently skip.
        return
    hist.append(features)
    # Trim to max size (FIFO).
    if len(hist) > FEATURE_HISTORY_MAX:
        del hist[: len(hist) - FEATURE_HISTORY_MAX]


# ---------------------------------------------------------------------------
# Composite spike detection
# ---------------------------------------------------------------------------

class SpikeDetector:
    def __init__(self, state: dict):
        self.state = state

    # ---- Legacy CoinGecko-only path (fallback) -----------------------------
    def check_legacy(self, price_data: dict) -> dict | None:
        """Original simple-threshold detection, used when Bybit features absent.

        Now respects the tier-aware suppression rules and only fires on
        the 1h horizon (24h alerts were removed at the user's request).
        """
        ch_1h = price_data["change_1h"]
        if abs(ch_1h) < THRESHOLD_1H_PCT:
            return None
        spike = {
            "window": "1h",
            "change": ch_1h,
            "direction": "up" if ch_1h > 0 else "down",
            "score": None,
            "reasons": [
                f"1h move {ch_1h:+.2f}% over ±{THRESHOLD_1H_PCT}% (legacy)"
            ],
            "features": None,
        }
        if self._is_suppressed(
            spike["window"], spike["direction"], price_data["timestamp"]
        ):
            return None
        return spike

    # ---- Composite-score path ---------------------------------------------
    def check_composite(self, price_data: dict, features: dict) -> dict | None:
        """Multi-feature detection with robust z-scores. Returns a spike dict or None."""
        if not features:
            log.info("No features — falling back to legacy detector")
            return self.check_legacy(price_data)

        history = self.state.get("feature_history", [])
        # Need a minimum amount of history for z-scores to be meaningful.
        if len(history) < 30:
            log.info(
                "Feature history too short (%d/30) — using hard-fallback only",
                len(history),
            )
            return self._hard_fallback_only(features, price_data)

        direction_cd = self._cooldown_direction(price_data["timestamp"])

        # --- 1. Per-feature robust z-scores ---
        atr_z = clipped_z(features["atr_pct"], history_field(history, "atr_pct"), 0, 4)
        vol_z = clipped_z(
            features["volume_5bar"], history_field(history, "volume_5bar"), 0, 4
        )
        # OI drop only matters when negative — we want clipped(negative_z) range.
        oi_changes = history_field(history, "oi_change_1h_pct")
        oi_z_signed = clipped_z(features["oi_change_1h_pct"], oi_changes, -4, 4)
        oi_drop_z = max(0.0, -oi_z_signed)  # 0 unless OI is dropping unusually
        # Funding magnitude (we ignore sign here — extreme funding matters either way).
        funding_abs_hist = [abs(x) for x in history_field(history, "funding_rate")]
        funding_abs_z = clipped_z(
            abs(features["funding_rate"]), funding_abs_hist, 0, 4
        )
        # Move-per-ATR is already a magnitude; z-score on its own scale.
        move_z = clipped_z(
            features["move_per_atr"], history_field(history, "move_per_atr"), 0, 4
        )

        # --- 2. Composite score ---
        score = (
            SCORE_WEIGHTS["move_per_atr"] * move_z
            + SCORE_WEIGHTS["atr_pct"]    * atr_z
            + SCORE_WEIGHTS["volume_5bar"] * vol_z
            + SCORE_WEIGHTS["oi_drop"]    * oi_drop_z
            + SCORE_WEIGHTS["funding_abs"] * funding_abs_z
        )

        return_5m = features["return_5m"]
        return_15m = features["return_15m"]

        reasons: list[str] = []
        fired_window: str | None = None
        fired_change: float = 0.0
        fired_direction: str = "up"

        # --- 3. Hard fallback first (catches obvious moves regardless of score).
        #        We probe in tier-priority order: short windows first, then
        #        medium, then long. 24h+ are intentionally not considered.
        if abs(return_5m) >= HARD_FALLBACK_RETURN_5M_PCT:
            fired_window = "5m"
            fired_change = return_5m
            fired_direction = "up" if return_5m >= 0 else "down"
            reasons.append(f"5m move {return_5m:+.2f}% over hard floor")
        elif abs(return_15m) >= HARD_FALLBACK_RETURN_15M_PCT:
            fired_window = "15m"
            fired_change = return_15m
            fired_direction = "up" if return_15m >= 0 else "down"
            reasons.append(f"15m move {return_15m:+.2f}% over hard floor")
        elif abs(features["return_1h"]) >= HARD_FALLBACK_RETURN_1H_PCT:
            fired_window = "1h"
            fired_change = features["return_1h"]
            fired_direction = "up" if features["return_1h"] >= 0 else "down"
            reasons.append(f"1h move {fired_change:+.2f}% over hard floor")

        # --- 4. Composite gate: independent 5m and 15m windows ---
        # 5m is the responsive timeframe — fires often. 15m is the trend-
        # confirmation timeframe — fires only on sustained moves so it
        # doesn't replay a 5m alert as a separate "15m" alert 10min later.
        adaptive_floor_5m = adaptive_return_floor(
            history, FIRE_RETURN_5M_MIN_PCT,
            reference_pct=ADAPTIVE_REF_ATR_PCT_5M,
        )
        adaptive_floor_15m = adaptive_return_floor(
            history, FIRE_RETURN_15M_MIN_PCT,
            reference_pct=ADAPTIVE_REF_ATR_PCT_15M,
        )

        passes_score_and_confirm = (
            score >= FIRE_SCORE_MIN
            and (atr_z >= FIRE_ATR_Z_MIN or vol_z >= FIRE_VOL_Z_MIN)
        )

        # Strength = how many "floor units" the move covers. Higher = clearer.
        strength_5m = (
            abs(return_5m) / adaptive_floor_5m if adaptive_floor_5m > 0 else 0.0
        )
        strength_15m = (
            abs(return_15m) / adaptive_floor_15m if adaptive_floor_15m > 0 else 0.0
        )
        passes_5m = passes_score_and_confirm and strength_5m >= 1.0
        passes_15m = passes_score_and_confirm and strength_15m >= 1.0

        # If hard fallback already chose a window, don't downgrade it.
        # Otherwise pick the timeframe with the higher relative strength.
        if fired_window is None and (passes_5m or passes_15m):
            if passes_5m and (not passes_15m or strength_5m >= strength_15m):
                fired_window = "5m"
                fired_change = return_5m
                fired_direction = "up" if return_5m >= 0 else "down"
                floor_used, base_pct = adaptive_floor_5m, FIRE_RETURN_5M_MIN_PCT
            else:
                fired_window = "15m"
                fired_change = return_15m
                fired_direction = "up" if return_15m >= 0 else "down"
                floor_used, base_pct = adaptive_floor_15m, FIRE_RETURN_15M_MIN_PCT

            floor_note = (
                f"{floor_used:.2f}% (vol-adaptive)"
                if abs(floor_used - base_pct) > 1e-6
                else f"{base_pct}%"
            )
            reasons.extend([
                f"score={score:.2f} ≥ {FIRE_SCORE_MIN}",
                f"{fired_window} return {fired_change:+.2f}% ≥ {floor_note}",
                f"ATR z={atr_z:.2f}, volume z={vol_z:.2f}",
            ])
            if oi_drop_z > 1.0:
                reasons.append(f"OI drop z={oi_drop_z:.2f} (cascade-liq proxy)")
            if funding_abs_z > 1.5:
                reasons.append(f"|funding| z={funding_abs_z:.2f} (crowded)")

        if fired_window is None:
            log.info(
                "No spike — score=%.2f, 5m=%+.2f%% (floor %.2f), "
                "15m=%+.2f%% (floor %.2f), atr_z=%.2f, vol_z=%.2f",
                score, return_5m, adaptive_floor_5m,
                return_15m, adaptive_floor_15m, atr_z, vol_z,
            )
            return None

        # --- 5. Cooldown (tier-aware): a recent shorter-tier alert
        #        suppresses medium-tier candidates; same-tier same-direction
        #        cooldown still applies.
        if self._is_suppressed(
            fired_window, fired_direction, price_data["timestamp"]
        ):
            return None

        return {
            "window": fired_window,
            "change": fired_change,
            "direction": fired_direction,
            "score": round(score, 2),
            "reasons": reasons,
            "features": features,
        }

    # ---- Internal helpers --------------------------------------------------
    def _hard_fallback_only(self, features: dict, price_data: dict) -> dict | None:
        """Pre-history-warmup gate: only fire on obvious moves.

        Probes the same window list as the steady-state path: short tier
        first (5m), then medium (15m), then long (1h). Anything longer
        than 1h was removed at the user's request.
        """
        for window, value, hard in (
            ("5m",  features.get("return_5m",  0.0), HARD_FALLBACK_RETURN_5M_PCT),
            ("15m", features.get("return_15m", 0.0), HARD_FALLBACK_RETURN_15M_PCT),
            ("1h",  features.get("return_1h",  0.0), HARD_FALLBACK_RETURN_1H_PCT),
        ):
            if abs(value) >= hard:
                direction = "up" if value >= 0 else "down"
                if self._is_suppressed(window, direction, price_data["timestamp"]):
                    return None
                return {
                    "window": window,
                    "change": value,
                    "direction": direction,
                    "score": None,
                    "reasons": [f"{window} move {value:+.2f}% over hard floor (warmup)"],
                    "features": features,
                }
        return None

    def _last_tier_alert(
        self, tier: str
    ) -> tuple[datetime, str, str | None] | None:
        """Return ``(ts, direction, window)`` of the most recent alert in ``tier``.

        Backward-compatible: if only the legacy flat keys exist
        (``last_alert_time``/``last_alert_direction``), they are mapped to
        the tier of ``last_spike_window`` (defaulting to ``short``).
        """
        ts_iso = self.state.get(f"last_alert_{tier}_time")
        direction = self.state.get(f"last_alert_{tier}_direction")
        window = self.state.get(f"last_alert_{tier}_window")
        if ts_iso and direction:
            try:
                return datetime.fromisoformat(ts_iso), direction, window
            except Exception:
                return None
        # Legacy migration path.
        legacy_ts = self.state.get("last_alert_time")
        legacy_dir = self.state.get("last_alert_direction")
        legacy_window = self.state.get("last_spike_window")
        if legacy_ts and legacy_dir and legacy_window:
            legacy_tier = WINDOW_TIER.get(legacy_window, "short")
            if legacy_tier == tier:
                try:
                    return (
                        datetime.fromisoformat(legacy_ts),
                        legacy_dir,
                        legacy_window,
                    )
                except Exception:
                    return None
        return None

    def _cooldown_direction(self, now_iso: str) -> str | None:
        """Legacy helper — returns the most recent SHORT-tier alert's direction.

        Kept for compatibility with the realtime fast-track call site and
        with tests that pre-date the tier system. New call sites should
        use _is_suppressed() directly.
        """
        last = self._last_tier_alert("short")
        if not last:
            return None
        ts, direction, _ = last
        try:
            elapsed = (datetime.fromisoformat(now_iso) - ts).total_seconds() / 60
        except Exception:
            return None
        return direction if elapsed < COOLDOWN_SAME_DIR_MIN else None

    def _suppressed_by_cooldown(
        self, last_dir: str | None, current_dir: str
    ) -> bool:
        """Legacy helper — applies SHORT-tier cooldown for fast-track use."""
        if last_dir is None:
            return False
        last = self._last_tier_alert("short")
        if not last:
            return False
        ts, _, _ = last
        elapsed = (
            datetime.now(ts.tzinfo) - ts
        ).total_seconds() / 60
        if last_dir != current_dir:
            if elapsed >= COOLDOWN_OPP_DIR_MIN:
                return False
            log.info(
                "Cooldown active (reversal, short tier): %.1f / %d min",
                elapsed, COOLDOWN_OPP_DIR_MIN,
            )
            return True
        log.info("Cooldown active (same direction, short tier)")
        return True

    def _is_suppressed(self, window: str, direction: str, now_iso: str) -> bool:
        """Tier-aware cooldown.

        Suppression rules:
        - Same tier, same direction → 90min cooldown.
        - Same tier, opposite direction → 30min cooldown.
        - Cross-tier: a recent SHORT-tier alert (same direction) suppresses
          MEDIUM-tier candidates. Long tier is independent.
        """
        if window not in WINDOW_TIER:
            log.warning("Unknown window %r — not suppressing", window)
            return False
        spike_tier = WINDOW_TIER[window]
        try:
            now = datetime.fromisoformat(now_iso)
        except Exception:
            return False

        same_dir_cooldown = COOLDOWN_BY_TIER_MIN.get(
            spike_tier, COOLDOWN_SAME_DIR_MIN
        )

        # 1) Same-tier cooldown — with INTRA-TIER hierarchy. Within the
        #    short tier (1m/3m/5m), a faster window's fire silences only
        #    SLOWER-or-equal candidates; the reverse is allowed through
        #    so a fresh 1m fast-track after a 5m alert can still fire as
        #    the "急変動" override.
        same_tier_last = self._last_tier_alert(spike_tier)
        if same_tier_last:
            ts, last_dir, last_window = same_tier_last
            elapsed = (now - ts).total_seconds() / 60
            if last_dir == direction and elapsed < same_dir_cooldown:
                # Unknown last_window (e.g. pre-upgrade state files that
                # wrote time/direction but not window) is treated as
                # conservatively suppressive so we never silently lose
                # cooldown across deployment upgrades.
                last_rank = WINDOW_RANK.get(last_window or "")
                cur_rank = WINDOW_RANK.get(window, 99)
                if last_rank is None or cur_rank >= last_rank:
                    log.info(
                        "Suppressed: %s tier same-direction cooldown "
                        "(%.1f / %d min, last=%s, cur=%s rank=%d)",
                        spike_tier, elapsed, same_dir_cooldown,
                        last_window, window, cur_rank,
                    )
                    return True
                # else: candidate is FASTER than last fire — let through.
                log.info(
                    "Allowing %s through: faster than recent %s alert "
                    "(rank %d < %d, %.1f min ago)",
                    window, last_window, cur_rank, last_rank, elapsed,
                )
            elif last_dir != direction and elapsed < COOLDOWN_OPP_DIR_MIN:
                log.info(
                    "Suppressed: %s tier reversal cooldown (%.1f / %d min)",
                    spike_tier, elapsed, COOLDOWN_OPP_DIR_MIN,
                )
                return True

        # 2) Cross-tier: medium (15m) is suppressed by ANY recent
        #    same-direction short-tier alert (1m/3m/5m). The medium
        #    tier's longer cooldown applies — short-tier news takes
        #    precedence over the slower 15m view of the same trend.
        if spike_tier == "medium":
            short_last = self._last_tier_alert("short")
            if short_last:
                ts, last_dir, _ = short_last
                elapsed = (now - ts).total_seconds() / 60
                if last_dir == direction and elapsed < same_dir_cooldown:
                    log.info(
                        "Suppressed: medium-tier alert preempted by "
                        "short-tier alert %.1f min ago", elapsed,
                    )
                    return True

        # Long tier (1h) and short tier are independent of each other.
        return False
