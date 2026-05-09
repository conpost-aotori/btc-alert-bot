"""Discord-only end-to-end test.

Forces a fake spike (so detection always fires) and runs the full pipeline:
real price fetch → real factor analysis → real Gemini summary → Discord post.
X posting is skipped to preserve the 500/month Free tier quota.

Usage:
    pip install -e .
    python scripts/test_discord.py
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Allow running from project root without install: prepend src/ to path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

from btc_alert_bot.analyzers import gather_factors
from btc_alert_bot.chart import render_chart
from btc_alert_bot.features import compute_market_features
from btc_alert_bot.market import fetch_market_snapshot
from btc_alert_bot.price import fetch_btc_price
from btc_alert_bot.publishers import post_discord
from btc_alert_bot.summarizer import summarize

load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("test_discord")


def main() -> int:
    log.info("=== Discord Test Run ===")

    # 1. Real price.
    price_data = fetch_btc_price()
    log.info(
        "BTC ${:,.2f} | 1h {:+.2f}% | 24h {:+.2f}%".format(
            price_data["price_usd"],
            price_data["change_1h"],
            price_data["change_24h"],
        )
    )

    # 2. Real Bybit features (so the summary uses live observations, not mocks).
    log.info("Fetching Bybit market snapshot...")
    try:
        snapshot = fetch_market_snapshot()
        features = compute_market_features(snapshot)
    except Exception as e:
        log.warning("Bybit fetch failed: %s — features will be empty", e)
        features = {}

    # 3. Force a spike. Use the real 15m return if visible, otherwise fabricate.
    real_change = features.get("return_15m", 0.0) if features else price_data["change_1h"]
    if abs(real_change) < 0.5:
        log.info(
            "Real movement tiny (%.2f%%) — fabricating +2.5%% for test", real_change
        )
        forced_change = 2.5
    else:
        forced_change = real_change
    spike = {
        "window": "15m",
        "change": forced_change,
        "direction": "up" if forced_change > 0 else "down",
        "score": 3.1,
        "reasons": [
            "[forced for test]",
            f"15m return {forced_change:+.2f}%",
            f"ATR%={features.get('atr_pct', 0):.3f}" if features else "no ATR data",
            f"OI Δ1h={features.get('oi_change_1h_pct', 0):+.2f}%" if features else "no OI data",
        ],
        "features": features,
    }
    log.info("Forced spike: %+.2f%% / %s (score=%s)", spike["change"], spike["window"], spike["score"])

    # 3. Real factor analysis.
    log.info("Gathering factors (parallel)...")
    factors = gather_factors(spike)
    log.info("Got %d factors:", len(factors))
    for f in factors[:5]:
        log.info("  - [%s/%s] %s", f["type"], f["source"], f["title"][:80])

    # 4. Real Gemini summary.
    log.info("Calling Gemini...")
    summary = summarize(price_data, spike, factors)
    log.info("Summary:\n%s", summary)

    # 5. Render chart PNG.
    log.info("Rendering chart...")
    try:
        chart_png = render_chart(spike, price_data)
        log.info("Chart: %d KB", len(chart_png) // 1024)
    except Exception as e:
        log.warning("Chart render failed: %s — text only", e)
        chart_png = None

    # 6. Discord post (X skipped).
    log.info("Posting to Discord (X skipped to save quota)...")
    post_discord(summary, price_data, spike, chart_png=chart_png)

    log.info("=== Test Complete ===")
    log.info("Check Discord channel for the alert message.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
