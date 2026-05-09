"""Entry point — invoked by GitHub Actions every 5 minutes.

Pipeline:
1. Fetch CoinGecko price (lightweight, used for the embed body).
2. Fetch Bybit market snapshot + compute features.
3. Append features to the rolling history (regardless of spike).
4. Run composite-score detector (or legacy fallback if Bybit failed).
5. If spike → gather factors, summarize, render chart, publish.
6. Save state ONLY if at least one publisher succeeded.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .analyzers import gather_factors
from .chart import render_chart
from .detector import (
    SpikeDetector,
    append_feature_history,
    load_state,
    save_state,
)
from .features import compute_market_features
from .history import find_similar_alerts, record_alert
from .market import fetch_market_snapshot
from .price import fetch_btc_price
from .publishers import post_discord, post_x
from .summarizer import summarize

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("btc_alert_bot")

STATE_PATH = Path("data/state.json")
HISTORY_DB_PATH = Path("data/history.sqlite")


def main() -> int:
    log.info("=== BTC Alert Bot run started ===")

    # 1. Snapshot price (CoinGecko — used in the Discord embed).
    try:
        price_data = fetch_btc_price()
    except Exception as e:
        log.error("Price fetch failed — aborting: %s", e)
        return 1

    log.info(
        "BTC ${:,.2f} | 1h {:+.2f}% | 24h {:+.2f}%".format(
            price_data["price_usd"], price_data["change_1h"], price_data["change_24h"]
        )
    )

    # 2. Load state first — features.py needs it for OI history lookup.
    state = load_state(STATE_PATH)

    # 3. OKX market snapshot + feature engineering.
    features: dict = {}
    try:
        snapshot = fetch_market_snapshot()
        features = compute_market_features(snapshot, state=state)
        if features:
            log.info(
                "Features: ATR%%=%.3f, ret15m=%+.2f%%, move/ATR=%.2f, "
                "OI Δ1h=%+.2f%%, fund=%.5f",
                features["atr_pct"], features["return_15m"],
                features["move_per_atr"], features["oi_change_1h_pct"],
                features["funding_rate"],
            )
    except Exception as e:
        log.warning(
            "OKX market fetch failed: %s — falling back to CoinGecko-only detection",
            e,
        )

    # 4. Append features to ring buffer (always — for z-score history).
    append_feature_history(state, features)

    # 5. Spike detection (composite if features available, else legacy).
    detector = SpikeDetector(state)
    if features:
        spike = detector.check_composite(price_data, features)
    else:
        spike = detector.check_legacy(price_data)

    if spike is None:
        # Save the updated feature history even if no spike fired.
        save_state(STATE_PATH, state)
        log.info("No spike. State persisted (history only). Done.")
        return 0

    log.info(
        "SPIKE: %s window, %+.2f%% (%s) — score=%s",
        spike["window"], spike["change"], spike["direction"],
        spike.get("score"),
    )
    for r in spike.get("reasons") or []:
        log.info("  reason: %s", r)

    # 6. Parallel factor analysis.
    factors = gather_factors(spike)
    log.info("Gathered %d candidate factors", len(factors))

    # 6b. Look up similar past alerts (Phase 2.5).
    similar = find_similar_alerts(HISTORY_DB_PATH, spike, limit=3)
    if similar:
        log.info(
            "Similar past alerts: %s",
            ", ".join(f"#{s['id']}({s['change_pct']:+.2f}%)" for s in similar),
        )

    # 7. Generate Japanese summary via Gemini.
    summary = summarize(price_data, spike, factors, similar_alerts=similar)
    log.info("Summary:\n%s", summary)

    # 8. Render chart PNG (optional — alert still goes out if rendering fails).
    chart_png: bytes | None = None
    try:
        chart_png = render_chart(spike, price_data)
        log.info("Chart rendered (%d KB)", len(chart_png) // 1024)
    except Exception as e:
        log.warning("Chart render failed: %s — posting text only", e)

    # 9. Publish (or dry-run preview). Track per-channel for the history DB.
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    enable_x = os.getenv("ENABLE_X_POST", "false").lower() == "true"
    delivered_discord = False
    delivered_x = False
    if dry_run:
        log.info("[DRY_RUN] Skipping actual posts")
        delivered_discord = True  # treat dry-run as success for cooldown
    else:
        delivered_discord = post_discord(
            summary, price_data, spike, chart_png=chart_png
        )
        if enable_x:
            delivered_x = post_x(
                summary, price_data, spike, chart_png=chart_png
            )
        else:
            log.info("X posting disabled (set ENABLE_X_POST=true to enable)")
    delivered = delivered_discord or delivered_x

    # 10. Append to SQLite history DB regardless of delivery — even a failed
    #     delivery is still a real spike worth auditing later.
    record_alert(
        HISTORY_DB_PATH,
        price_data=price_data,
        spike=spike,
        factors=factors,
        summary=summary,
        delivered_discord=delivered_discord,
        delivered_x=delivered_x,
    )

    # 11. Persist state.json — cooldown fields only updated if delivery
    #     succeeded, but feature_history (already appended) is always saved.
    if delivered:
        state.update({
            "last_alert_time": price_data["timestamp"],
            "last_alert_price": price_data["price_usd"],
            "last_alert_direction": spike["direction"],
            "last_spike_window": spike["window"],
            "last_spike_change": spike["change"],
            "last_spike_score": spike.get("score"),
        })
        save_state(STATE_PATH, state)
        log.info("Cooldown state + history persisted.")
    else:
        # Still save feature history so the next run has data — but don't
        # mark cooldown so the next tick can retry the publish.
        save_state(STATE_PATH, state)
        log.warning(
            "All publishers failed — history saved, cooldown NOT updated."
        )
    log.info("=== Done ===")
    return 0 if delivered else 1


if __name__ == "__main__":
    sys.exit(main())
