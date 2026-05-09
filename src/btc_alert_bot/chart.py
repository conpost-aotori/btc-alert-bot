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

import os.path

import matplotlib

matplotlib.use("Agg")  # Headless backend — required in CI.

import matplotlib.font_manager as _fm  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import mplfinance as mpf  # noqa: E402
import pandas as pd  # noqa: E402

from .market import fetch_klines  # noqa: E402

# Ensure CJK glyphs (e.g. "仮想NISHI" in the credit footer) render instead
# of falling back to tofu boxes. We explicitly addfont() the file rather
# than relying on matplotlib's auto-scan, because the font cache is built
# during Docker build BEFORE fonts-noto-cjk lands and never refreshes.
_CJK_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # Linux (Debian/Ubuntu)
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/Library/Fonts/Hiragino Sans W3.ttc",                      # macOS
    "C:/Windows/Fonts/YuGothR.ttc",                              # Windows
]
_CJK_FONT_PATH: str | None = None
for _candidate in _CJK_FONT_CANDIDATES:
    if os.path.exists(_candidate):
        try:
            _fm.fontManager.addfont(_candidate)
            _CJK_FONT_PATH = _candidate
            break
        except Exception:
            pass

# An explicit FontProperties bypasses mpf style overrides — we use this
# whenever we draw text that may contain CJK glyphs.
_CJK_FONT_PROPS = (
    _fm.FontProperties(fname=_CJK_FONT_PATH) if _CJK_FONT_PATH else None
)

plt.rcParams["axes.unicode_minus"] = False

log = logging.getLogger(__name__)

# (OKX bar code, num candles) per spike window.
# OKX bar codes: "1m","3m","5m","15m","30m","1H","2H","4H","6H","12H","1D"...
TIMEFRAME_BY_WINDOW: dict[str, tuple[str, int]] = {
    "1m":  ("1m",  60),   # 1h of 1min candles — fast-track 1m spikes
    "5m":  ("1m", 120),   # 2h of 1min candles — composite 5m spikes
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

    # Footer left: source attribution (subtle).
    fig.text(
        0.01, 0.005,
        f"Source: OKX (UTC) — {limit} × {bar} candles",
        ha="left", fontsize=8, color="gray", alpha=0.6,
    )
    # Footer right: creator credit. Italics + low alpha so it's
    # visible-but-unobtrusive on the dark theme. fontproperties is set
    # explicitly so the CJK characters in 仮想NISHI render via Noto CJK
    # rather than tofu-boxing through mpf's default font.
    credit_kwargs = dict(
        ha="right", fontsize=8, color="lightgray", alpha=0.55, style="italic",
    )
    if _CJK_FONT_PROPS is not None:
        credit_kwargs["fontproperties"] = _CJK_FONT_PROPS
    fig.text(0.99, 0.005, "Crafted by 仮想NISHI · @Nishi8maru", **credit_kwargs)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
