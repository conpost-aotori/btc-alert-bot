"""Send the summary (and optional chart PNG) to Discord and X."""
from __future__ import annotations

import io
import json
import logging
import os

import requests
import tweepy

log = logging.getLogger(__name__)

# Twitter weighted-character limit. CJK / kana / hangul characters count
# as 2; ASCII / Latin characters count as 1. Plain `len()` undercounts
# Japanese tweets and the API rejects the post past the budget.
X_WEIGHTED_LIMIT = 280


def _x_char_weight(c: str) -> int:
    """Return 1 or 2 per Twitter's weighted-character rules.

    Reference: https://developer.x.com/en/docs/counting-characters
    The exact spec is range-based; this approximation covers the usual
    CJK / hiragana / katakana / hangul blocks accurately.
    """
    cp = ord(c)
    # ASCII + Latin extended (matches Twitter's 1-weight ranges).
    if cp <= 0x10FF:
        return 1
    # All wider scripts (CJK, kana, hangul, fullwidth punctuation, …) = 2.
    return 2


def x_weighted_length(s: str) -> int:
    return sum(_x_char_weight(c) for c in s)


def x_truncate_to_weight(s: str, budget: int) -> str:
    """Trim ``s`` so its weighted length is <= budget. Adds an ellipsis."""
    if x_weighted_length(s) <= budget:
        return s
    out: list[str] = []
    used = 0
    # Reserve 1 weighted unit for the trailing ellipsis (… is 1-weight).
    target = max(0, budget - 1)
    for c in s:
        w = _x_char_weight(c)
        if used + w > target:
            break
        out.append(c)
        used += w
    return "".join(out) + "…"


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
    price_data: dict,  # noqa: ARG001 — present for parity with post_discord
    spike: dict,
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

    # Build tweet text: alert header + summary + hashtags.
    # Mirrors the Discord embed title for consistency.
    header = (
        f"🚨 BTC緊急価格変動速報 "
        f"({spike['change']:+.2f}% / {spike['window']})"
    )
    hashtags = "#BTC #Bitcoin #暗号資産"
    # Twitter weights CJK chars at 2 each — `len()` would undercount and
    # let the API reject long Japanese summaries. Use weighted length.
    fixed_weight = (
        x_weighted_length(header) + x_weighted_length(hashtags) + 2  # 2 newlines
    )
    body_budget = max(20, X_WEIGHTED_LIMIT - fixed_weight)
    body = x_truncate_to_weight(summary, body_budget)
    text = f"{header}\n{body}\n{hashtags}"

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
