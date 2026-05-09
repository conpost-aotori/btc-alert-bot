"""Send the summary (and optional chart PNG) to Discord and X."""
from __future__ import annotations

import io
import json
import logging
import os

import requests
import tweepy

log = logging.getLogger(__name__)


def post_discord(
    summary: str,
    price_data: dict,
    spike: dict,
    chart_png: bytes | None = None,
) -> bool:
    """Post an embed alert to Discord. Returns True on success, False on failure.

    Caller should treat False as "alert did not reach users" — typically meaning
    cooldown state should NOT be persisted, so the next cron tick can retry.
    """
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook:
        log.warning("DISCORD_WEBHOOK_URL missing — skipping Discord")
        return False

    color = 0x00C853 if spike["direction"] == "up" else 0xD32F2F
    embed = {
        "title": f"🚨 BTC緊急価格変動速報 ({spike['change']:+.2f}% / {spike['window']})",
        "description": summary,
        "color": color,
        "fields": [
            {
                "name": "現在価格",
                "value": f"${price_data['price_usd']:,.2f}",
                "inline": True,
            },
            {
                "name": "24h Range",
                "value": f"${price_data['low_24h']:,.0f} – ${price_data['high_24h']:,.0f}",
                "inline": True,
            },
            {
                "name": "24h Volume",
                "value": f"${price_data['volume_24h'] / 1e9:,.1f}B",
                "inline": True,
            },
        ],
        "footer": {"text": "Source: CoinGecko + Bybit + RSS feeds"},
    }

    # If we have a chart, reference the multipart attachment by filename.
    if chart_png:
        embed["image"] = {"url": "attachment://chart.png"}

    payload = {"embeds": [embed]}

    try:
        if chart_png:
            # Discord webhook with file attachment requires multipart/form-data.
            resp = requests.post(
                webhook,
                data={"payload_json": json.dumps(payload)},
                files={"chart.png": ("chart.png", chart_png, "image/png")},
                timeout=30,
            )
        else:
            resp = requests.post(webhook, json=payload, timeout=15)
        resp.raise_for_status()
        log.info(
            "Discord OK (status=%d, with_chart=%s)",
            resp.status_code, bool(chart_png),
        )
        return True
    except Exception as e:
        log.error("Discord post failed: %s", e)
        return False


def post_x(
    summary: str,
    price_data: dict,  # noqa: ARG001
    spike: dict,  # noqa: ARG001
    chart_png: bytes | None = None,
) -> bool:
    """Post a tweet, optionally with the chart PNG attached. Returns success.

    Note: media upload requires v1.1 (api.media_upload), but the tweet itself
    is created via v2 (client.create_tweet). Both are supported by Free tier
    for the 500 posts/month limit.
    """
    keys = {
        "consumer_key": os.getenv("X_API_KEY"),
        "consumer_secret": os.getenv("X_API_SECRET"),
        "access_token": os.getenv("X_ACCESS_TOKEN"),
        "access_token_secret": os.getenv("X_ACCESS_SECRET"),
    }
    if not all(keys.values()):
        log.warning("X API keys incomplete — skipping X post")
        return False

    text = summary
    if len(text) > 270:
        text = text[:267] + "..."

    try:
        media_ids: list[int] | None = None
        if chart_png:
            # v1.1 path for media upload.
            auth = tweepy.OAuth1UserHandler(
                keys["consumer_key"], keys["consumer_secret"],
                keys["access_token"], keys["access_token_secret"],
            )
            api_v1 = tweepy.API(auth)
            with io.BytesIO(chart_png) as buf:
                media = api_v1.media_upload(filename="chart.png", file=buf)
            media_ids = [media.media_id]

        client = tweepy.Client(**keys)
        resp = client.create_tweet(text=text, media_ids=media_ids)
        tweet_id = resp.data.get("id") if resp.data else "?"
        log.info("X OK (id=%s, with_chart=%s)", tweet_id, bool(chart_png))
        return True
    except Exception as e:
        log.error("X post failed: %s", e)
        return False
