"""Render a BTC candlestick chart PNG for embedding in Discord/X alerts.

Pulls OHLC from Bybit (free, no auth) and draws a candlestick chart with
volume sub-pane via ``mplfinance``. Returns PNG bytes in-memory so the
GitHub Actions job never has to write to disk.

Timeframe is adaptive to the spike window so the trigger move is always
visually centered:

- 1h spike  → 6h × 5min  (72 candles, fine detail around the move)
- 24h spike → 24h × 15min (96 candles, full day context)
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")  # Headless backend — required in CI.

import matplotlib.pyplot as plt  # noqa: E402
import mplfinance as mpf  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402

log = logging.getLogger(__name__)

BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"

# (interval_minutes_str, num_candles) per spike window.
# Bybit interval strings: "1","3","5","15","30","60","120","240","360","720","D".
TIMEFRAME_BY_WINDOW: dict[str, tuple[str, int]] = {
    "1h": ("5", 72),    # 6h of 5-min candles
    "24h": ("15", 96),  # 24h of 15-min candles
}
DEFAULT_TIMEFRAME = ("5", 144)  # fallback: 12h × 5min


def fetch_ohlcv(interval: str, limit: int) -> list[dict]:
    """Fetch BTC/USDT perpetual klines from Bybit, oldest-first."""
    resp = requests.get(
        BYBIT_KLINE_URL,
        params={
            "category": "linear",
            "symbol": "BTCUSDT",
            "interval": interval,
            "limit": limit,
        },
        timeout=20,
    )
    resp.raise_for_status()
    raw = resp.json().get("result", {}).get("list", [])
    raw.reverse()  # Bybit returns newest-first.
    return [
        {
            "ts": datetime.fromtimestamp(int(c[0]) / 1000, tz=timezone.utc),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
        }
        for c in raw
    ]


def _candles_to_dataframe(candles: list[dict]) -> pd.DataFrame:
    """Convert Bybit candles into the OHLCV DataFrame mplfinance expects."""
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
    interval, limit = TIMEFRAME_BY_WINDOW.get(spike["window"], DEFAULT_TIMEFRAME)
    candles = fetch_ohlcv(interval, limit)
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
        f"Source: Bybit (UTC) — {limit} × {interval}min candles",
        ha="right", fontsize=8, color="gray", alpha=0.7,
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
