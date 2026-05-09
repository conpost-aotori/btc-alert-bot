"""Fetch current BTC price + recent change percentages from CoinGecko."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/bitcoin"


def fetch_btc_price() -> dict:
    """Return current BTC price snapshot from CoinGecko (free tier)."""
    params = {
        "localization": "false",
        "tickers": "false",
        "community_data": "false",
        "developer_data": "false",
        "sparkline": "false",
    }
    resp = requests.get(COINGECKO_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    market = data["market_data"]
    return {
        "price_usd": float(market["current_price"]["usd"]),
        # CoinGecko returns 1h change only when the *_in_currency.usd field is present.
        "change_1h": float(
            market.get("price_change_percentage_1h_in_currency", {}).get("usd", 0.0)
        ),
        "change_24h": float(market["price_change_percentage_24h"]),
        "high_24h": float(market["high_24h"]["usd"]),
        "low_24h": float(market["low_24h"]["usd"]),
        "volume_24h": float(market["total_volume"]["usd"]),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
