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
import matplotlib.patches as _mpatches  # noqa: E402
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
    "3m":  ("1m",  90),   # 1.5h of 1min candles — fast-track 3m spikes
    "5m":  ("1m", 120),   # 2h of 1min candles — composite 5m spikes
    "15m": ("5m",  72),   # 6h of 5min candles
    "1h":  ("5m",  72),   # 6h of 5min candles
    "2h":  ("15m", 48),   # 12h of 15min candles — slow-grind 2h spikes
    "12h": ("1H",  72),   # 3 days of 1H candles — 12h status reports
    "24h": ("15m", 96),   # 24h of 15min candles (legacy, no longer fired)
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
    subtitle = (
        f"BTC/USDT  ${price_data['price_usd']:,.0f}  "
        f"{arrow} {spike['change']:+.2f}% / {spike['window']}"
    )

    # tight_layout=False so we can control the top margin ourselves
    # (the banner + subtitle band needs ~22% of the figure height that
    # mpf's auto-layout otherwise reclaims for the chart).
    fig, _axes = mpf.plot(
        df,
        type="candle",
        style=style,
        # Title intentionally omitted — we draw the urgent banner +
        # subtitle ourselves below so neither collides with the chart.
        ylabel="USD",
        figsize=(10, 5.4),
        tight_layout=False,
        datetime_format="%H:%M",
        xrotation=0,
        returnfig=True,
    )

    # --- 緊急価格変動速報 banner (earthquake-EW style) -------------------
    # User wanted the chart to read like 緊急地震速報: deep red banner
    # spanning the full top, white heavy text, immediately readable as
    # "something serious just happened". Applied to every fire — every
    # alert the bot publishes is by definition urgent.
    #
    # Layout from top to bottom of figure:
    #   y=0.91-1.00  red banner with "緊急価格変動速報" (white bold)
    #   y=0.84-0.89  BTC/USDT subtitle band
    #   y=0.10-0.80  chart axes
    fig.subplots_adjust(top=0.80, bottom=0.10, left=0.07, right=0.97)
    BANNER_RED = "#C81414"  # deeper than the candle red so it stands out
    fig.patches.append(_mpatches.Rectangle(
        (0.0, 0.91), 1.0, 0.09,
        transform=fig.transFigure,
        facecolor=BANNER_RED,
        edgecolor="none",
        zorder=1,
    ))
    banner_kwargs = dict(
        ha="center", va="center",
        fontsize=22, color="white", weight="bold",
        zorder=2,
    )
    if _CJK_FONT_PROPS is not None:
        # Synthetic-bold via matplotlib weight kwarg since Noto CJK
        # Regular is the only face we addfont()'d. Good enough for the
        # banner without bundling another font.
        banner_kwargs["fontproperties"] = _CJK_FONT_PROPS
    fig.text(0.5, 0.955, "緊急価格変動速報", **banner_kwargs)

    # Subtitle in the gap between subplot top (0.80) and banner (0.91).
    # Centered at 0.855 keeps it clear of both.
    fig.text(
        0.5, 0.855, subtitle,
        ha="center", va="center",
        fontsize=14, color="white", weight="bold",
        zorder=2,
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
