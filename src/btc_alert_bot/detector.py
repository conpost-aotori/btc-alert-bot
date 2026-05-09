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
    clipped_z,
    history_field,
)

log = logging.getLogger(__name__)

# --- Legacy fallback thresholds (used if Bybit features are unavailable) ---
THRESHOLD_1H_PCT = 2.0
THRESHOLD_24H_PCT = 5.0

# --- Composite-score weights & gates (Phase 1 initial values from Codex) ---
SCORE_WEIGHTS = {
    "move_per_atr": 0.30,
    "atr_pct":      0.25,
    "volume_5bar":  0.20,
    "oi_drop":      0.15,   # only counts negative OI change
    "funding_abs":  0.10,
}

# Hard fire conditions (all must hold).
FIRE_SCORE_MIN = 2.6
FIRE_RETURN_15M_MIN_PCT = 0.8
FIRE_ATR_Z_MIN = 1.2
FIRE_VOL_Z_MIN = 1.5

# "Always alert" overrides — catch obvious moves even with thin history.
HARD_FALLBACK_RETURN_15M_PCT = 1.5
HARD_FALLBACK_RETURN_1H_PCT = 2.0
HARD_FALLBACK_RETURN_24H_PCT = 5.0

# Cooldown: same direction is suppressed longer than a reversal.
COOLDOWN_SAME_DIR_MIN = 90
COOLDOWN_OPP_DIR_MIN = 30

# Ring buffer: how many feature snapshots to retain in state.json.
FEATURE_HISTORY_MAX = HIST_LOOKBACK_BARS * 2  # ~48h of 5-min bars


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


def append_feature_history(state: dict, features: dict) -> None:
    """Append the latest features to the ring buffer in state."""
    if not features:
        return
    hist = state.setdefault("feature_history", [])
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
        """Original simple-threshold detection, used when Bybit features absent."""
        direction = self._cooldown_direction(price_data["timestamp"])
        ch_1h = price_data["change_1h"]
        ch_24h = price_data["change_24h"]

        spike: dict | None = None
        if abs(ch_1h) >= THRESHOLD_1H_PCT:
            spike = {
                "window": "1h",
                "change": ch_1h,
                "direction": "up" if ch_1h > 0 else "down",
                "score": None,
                "reasons": [f"1h move {ch_1h:+.2f}% over ±{THRESHOLD_1H_PCT}% (legacy)"],
                "features": None,
            }
        elif abs(ch_24h) >= THRESHOLD_24H_PCT:
            spike = {
                "window": "24h",
                "change": ch_24h,
                "direction": "up" if ch_24h > 0 else "down",
                "score": None,
                "reasons": [f"24h move {ch_24h:+.2f}% over ±{THRESHOLD_24H_PCT}% (legacy)"],
                "features": None,
            }
        if spike and self._suppressed_by_cooldown(direction, spike["direction"]):
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

        return_15m = features["return_15m"]
        direction = "up" if return_15m >= 0 else "down"

        reasons: list[str] = []

        # --- 3. Hard fallback first (catches obvious moves regardless of score) ---
        hard_fired = False
        if abs(return_15m) >= HARD_FALLBACK_RETURN_15M_PCT:
            hard_fired = True
            reasons.append(f"15m move {return_15m:+.2f}% over hard floor")
        elif abs(features["return_1h"]) >= HARD_FALLBACK_RETURN_1H_PCT:
            hard_fired = True
            reasons.append(f"1h move {features['return_1h']:+.2f}% over hard floor")
            direction = "up" if features["return_1h"] >= 0 else "down"
        elif abs(features["return_24h"]) >= HARD_FALLBACK_RETURN_24H_PCT:
            hard_fired = True
            reasons.append(f"24h move {features['return_24h']:+.2f}% over hard floor")
            direction = "up" if features["return_24h"] >= 0 else "down"

        # --- 4. Composite gate (score + return floor + vol/atr confirm) ---
        composite_fired = (
            score >= FIRE_SCORE_MIN
            and abs(return_15m) >= FIRE_RETURN_15M_MIN_PCT
            and (atr_z >= FIRE_ATR_Z_MIN or vol_z >= FIRE_VOL_Z_MIN)
        )
        if composite_fired:
            reasons.extend([
                f"score={score:.2f} ≥ {FIRE_SCORE_MIN}",
                f"15m return {return_15m:+.2f}% ≥ {FIRE_RETURN_15M_MIN_PCT}%",
                f"ATR z={atr_z:.2f}, volume z={vol_z:.2f}",
            ])
            if oi_drop_z > 1.0:
                reasons.append(f"OI drop z={oi_drop_z:.2f} (cascade-liq proxy)")
            if funding_abs_z > 1.5:
                reasons.append(f"|funding| z={funding_abs_z:.2f} (crowded)")

        if not (hard_fired or composite_fired):
            log.info(
                "No spike — score=%.2f, 15m=%+.2f%%, atr_z=%.2f, vol_z=%.2f",
                score, return_15m, atr_z, vol_z,
            )
            return None

        # --- 5. Cooldown (directional) ---
        if self._suppressed_by_cooldown(direction_cd, direction):
            return None

        return {
            "window": "15m",
            "change": return_15m,
            "direction": direction,
            "score": round(score, 2),
            "reasons": reasons,
            "features": features,
        }

    # ---- Internal helpers --------------------------------------------------
    def _hard_fallback_only(self, features: dict, price_data: dict) -> dict | None:
        """Pre-history-warmup gate: only fire on obvious moves."""
        direction_cd = self._cooldown_direction(price_data["timestamp"])
        for window, value, hard in (
            ("15m", features.get("return_15m", 0.0), HARD_FALLBACK_RETURN_15M_PCT),
            ("1h",  features.get("return_1h",  0.0), HARD_FALLBACK_RETURN_1H_PCT),
            ("24h", features.get("return_24h", 0.0), HARD_FALLBACK_RETURN_24H_PCT),
        ):
            if abs(value) >= hard:
                direction = "up" if value >= 0 else "down"
                if self._suppressed_by_cooldown(direction_cd, direction):
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

    def _cooldown_direction(self, now_iso: str) -> str | None:
        """Return the last alert's direction if cooldown is still active, else None."""
        last = self.state.get("last_alert_time")
        last_dir = self.state.get("last_alert_direction")
        if not last or not last_dir:
            return None
        try:
            elapsed_min = (
                datetime.fromisoformat(now_iso) - datetime.fromisoformat(last)
            ).total_seconds() / 60
        except Exception:
            return None
        # Both windows shrunk to 0 means "no cooldown".
        if elapsed_min >= COOLDOWN_SAME_DIR_MIN:
            return None
        return last_dir

    def _suppressed_by_cooldown(
        self, last_dir: str | None, current_dir: str
    ) -> bool:
        if last_dir is None:
            return False
        # Reversal: shorter cooldown.
        if last_dir != current_dir:
            last_iso = self.state.get("last_alert_time")
            try:
                elapsed = (
                    datetime.now().astimezone()
                    - datetime.fromisoformat(last_iso)
                ).total_seconds() / 60
                if elapsed >= COOLDOWN_OPP_DIR_MIN:
                    return False
                log.info(
                    "Cooldown active (reversal): %.1f / %d min",
                    elapsed, COOLDOWN_OPP_DIR_MIN,
                )
                return True
            except Exception:
                return True
        # Same direction: full cooldown enforced inside _cooldown_direction()
        log.info("Cooldown active (same direction)")
        return True
