"""High-signal X account list monitor via Grok x_search.

Replaces the unreliable Nitter-based x_monitor.py for the *primary*
path. (Nitter is kept as opt-in fallback when NITTER_ACCOUNTS env is
set — useful for self-hosted Nitter instances).

The curated list is intentionally short: each account has a documented
history of moving BTC price within minutes of a post. Adding noisy
accounts dilutes the signal Grok returns.

Designed accounts:
- @saylor          : MicroStrategy treasury buys
- @realDonaldTrump : crypto/tariff/Fed-related posts
- @elonmusk        : crypto + macro (BTC/DOGE pumps historically)
- @WuBlockchain    : Chinese / Asian crypto news
- @WatcherGuru     : breaking macro + crypto headlines
- @DeItaone        : terminal-style breaking news
- @nick_timiraos   : WSJ Fed reporter — pre-FOMC leak source; the
                     two-axis-of-BTC-pain that is Fed pivots gets
                     telegraphed here first.
- @EricBalchunas   : Bloomberg ETF analyst; spot BTC ETF flow data
                     (IBIT/FBTC inflows-outflows) drives BTC supply
                     dynamics and was the documented driver of the
                     5/28 -2% fire (id=6).

Default list is hardcoded but overridable via env ``X_SIGNAL_ACCOUNTS``
(comma-separated, no @ prefix).

Cost: ~$0.005 per fire (one Grok call). Silent skip when XAI_API_KEY unset.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

from .grok_search import call_grok_x_search

log = logging.getLogger(__name__)


DEFAULT_ACCOUNTS = [
    # Established crypto-native signals
    "saylor",
    "realDonaldTrump",
    "elonmusk",
    "WuBlockchain",
    "WatcherGuru",
    "DeItaone",
    # Macro / institutional axis — added per user expansion (recommended)
    "nick_timiraos",   # WSJ Fed insider, primary FOMC leak source
    "EricBalchunas",   # Bloomberg ETF analyst, spot BTC ETF flows
]


_SYSTEM_PROMPT = (
    "You monitor a curated list of high-signal X accounts for posts that "
    "could move BTC. Use the X search tool to find posts from these accounts "
    "in the last 2 hours that mention: bitcoin, BTC, crypto, ETF, Fed, "
    "rates, tariff, regulation, sanction, treasury buy, or large macro "
    "shock. Output each finding on its own line: "
    "'@account: <one-line quote or paraphrase, JP>'. Max 5 lines. "
    "If nothing relevant, reply '該当なし'."
)


# Matches '@account:' prefix or 'from @account' to attribute author.
_AUTHOR_RX = re.compile(r"@([A-Za-z0-9_]{2,15})", re.ASCII)


def _accounts_for_query() -> list[str]:
    raw = (os.getenv("X_SIGNAL_ACCOUNTS") or "").strip()
    if raw:
        return [a.strip().lstrip("@") for a in raw.split(",") if a.strip()]
    return DEFAULT_ACCOUNTS


def fetch_x_list_signals(spike: dict | None = None) -> list[dict]:
    """Pull notable recent posts from the curated high-signal X list.

    Returns 0-5 factor entries (one per notable post). Direction hint is
    derived from spike alignment + keyword (bullish: 'buy', 'approve',
    'rally'; bearish: 'sell', 'ban', 'sue', 'sanction'). Conservative
    when uncertain — leaves direction_hint None.
    """
    accounts = _accounts_for_query()
    if not accounts:
        return []
    accounts_str = " ".join("@" + a for a in accounts)
    user_prompt = (
        f"Search X for recent posts from these accounts ({accounts_str}) "
        f"in the last 2 hours that could move BTC. Cite each post."
    )

    result = call_grok_x_search(_SYSTEM_PROMPT, user_prompt)
    if result is None:
        return []
    text, citations = result
    if not text or "該当なし" in text:
        log.info("X list monitor: no notable posts from %d accounts", len(accounts))
        return []

    items: list[dict] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        m = _AUTHOR_RX.search(line)
        author = f"@{m.group(1)}" if m else "@?"
        cite_url = (
            citations[i] if i < len(citations) else (citations[0] if citations else "")
        )
        items.append({
            "type": "x_list",
            "source": f"Grok/X List {author}",
            "title": line[:240],
            "url": cite_url,
            "published": datetime.now(timezone.utc).isoformat(),
            "tags": ["x_list", "curated_signal"],
            # direction_hint left None — the ranker will derive one from
            # keyword matching against the title, same as for news items.
        })
        if len(items) >= 5:
            break

    log.info(
        "X list monitor OK (%d posts, %d citations, accounts=%d)",
        len(items), len(citations), len(accounts),
    )
    return items
