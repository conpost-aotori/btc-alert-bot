"""Render a BTC candlestick chart PNG for embedding in Discord/X alerts.

Pulls OHLC from OKX (free, no auth, CI-friendly) via market.fetch_klines and
draws a candlestick chart via ``mplfinance``. Returns PNG bytes in-memory
so the GitHub Actions job never has to write to disk.

Timeframe is adaptive to the spike window so the trigger move is always
visually centered:

- 15m / 1h spike → 6h × 5min  (72 candles, fine detail around the move)
- 24h spike      → 24h × 15min (96 candles, full day context)
"""
from __future__ import annotations

import io
import logging

import matplotlib

matplotlib.use("Agg")  # Headless backend — required in CI.

import matplotlib.pyplot as plt  # noqa: E402
import mplfinance as mpf  # noqa: E402
import pandas as pd  # noqa: E402

from .market import fetch_klines  # noqa: E402

log = logging.getLogger(__name__)

# (OKX bar code, num candles) per spike window.
# OKX bar codes: "1m","3m","5m","15m","30m","1H","2H","4H","6H","12H","1D"...
TIMEFRAME_BY_WINDOW: dict[str, tuple[str, int]] = {
    "15m": ("5m",  72),   # 6h of 5min candles
    "1h":  ("5m",  72),   # 6h of 5min candles
    "24h": ("15m", 96),   # 24h of 15min candles
}
DEFAULT_TIMEFRAME = ("5m", 144)  # fallback: 12h × 5min


def _candles_to_dataframe(candles: list[dict]) -> pd.DataFrame:
    """Convert market.fetch_klines candles into the OHLCV DataFrame mplfinance expects."""
    df = pd.DataFrame(
        [
            {
                "Date": c["ts"],
                "Open": c["open"],
                "High": c["high"],
                "Low": c["low"],
                "Close": c["close"],
                "Volume": c["volume"],
            }
            for c in candles
        ]
    )
    df.set_index("Date", inplace=True)
    return df


def render_chart(spike: dict, price_data: dict) -> bytes:
    """Generate the candlestick chart PNG and return raw bytes."""
    bar, limit = TIMEFRAME_BY_WINDOW.get(spike["window"], DEFAULT_TIMEFRAME)
    candles = fetch_klines(bar=bar, limit=limit)
    if not candles:
        raise RuntimeError("Bybit returned no kline data")

    df = _candles_to_dataframe(candles)

    # Discord-dark themed style.
    market_colors = mpf.make_marketcolors(
        up="#00C853",
        down="#FF5252",
        edge="inherit",
        wick={"up": "#00C853", "down": "#FF5252"},
        volume={"up": "#00C85355", "down": "#FF525255"},
    )
    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        marketcolors=market_colors,
        facecolor="#1e1f22",
        figcolor="#1e1f22",
        gridcolor="#444",
        gridstyle="--",
        rc={
            "axes.labelcolor": "lightgray",
            "xtick.color": "lightgray",
            "ytick.color": "lightgray",
            "axes.edgecolor": "#444",
            "axes.titlecolor": "white",
        },
    )

    arrow = "↑" if spike["direction"] == "up" else "↓"
    title = (
        f"BTC/USDT  ${price_data['price_usd']:,.0f}  "
        f"{arrow} {spike['change']:+.2f}% / {spike['window']}"
    )

    fig, _axes = mpf.plot(
        df,
        type="candle",
        style=style,
        title=title,
        ylabel="USD",
        figsize=(10, 5),
        tight_layout=True,
        datetime_format="%H:%M",
        xrotation=0,
        returnfig=True,
    )

    # Footer with source attribution.
    fig.text(
        0.99, 0.005,
        f"Source: OKX (UTC) — {limit} × {bar} candles",
        ha="right", fontsize=8, color="gray", alpha=0.7,
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
