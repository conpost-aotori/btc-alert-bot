"""On-chain large-transfer monitor via Grok X search → @whale_alert.

whale-alert.io does not expose a free public RSS — only paid HTTP API
($30+/month). Their @whale_alert X account posts the same data with the
same latency, so we point Grok's x_search tool at it and extract recent
large BTC / stablecoin transfers.

Design:
- Single Grok call per spike (parallel-safe in gather_factors).
- Threshold ≥ $10M USD-equivalent — anything smaller is noise.
- Returns 0-3 factor entries (one per distinct transfer). We let Grok
  do the parsing — its summarisation is more robust than a regex
  against the rolling tweet format.
- Direction hint: outflow FROM exchange = potential bullish (coins
  going to cold storage / OTC); inflow TO exchange = potential bearish
  (coins being readied for sale). Grok is asked to set this hint per
  transfer when the source/destination labels are clear.

Cost: same per-call basis as fetch_grok_x_search (~$0.005 / fire).
Silent skip when ``XAI_API_KEY`` is not set.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from .grok_search import call_grok_x_search

log = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "You scan @whale_alert and @whale_alert_io on X for large BTC and "
    "stablecoin transfers in the last 2 hours. Use the X search tool. "
    "Report each transfer >= $10M USD on its own line in this format: "
    "'$XX.X(M|B) BTC|USDT|USDC: From → To [dir:bullish|bearish|neutral]'. "
    "dir is bullish for exchange→unknown, bearish for unknown→exchange, "
    "neutral otherwise. Max 5 lines. If nothing >= $10M, reply '該当なし'."
)


# Parse one whale line into (size_usd, direction_hint).
# Matches "$120.5M BTC" or "$1.2B USDT".
_SIZE_RX = re.compile(
    r"\$(\d+(?:\.\d+)?)\s*([MB])\s+(BTC|USDT|USDC|ETH)",
    re.IGNORECASE,
)
_DIR_RX = re.compile(r"\[dir:(bullish|bearish|neutral)\]", re.IGNORECASE)


def _parse_line(line: str) -> tuple[float, str | None] | None:
    """Extract magnitude in USD and direction hint from one whale line."""
    m_size = _SIZE_RX.search(line)
    if not m_size:
        return None
    val = float(m_size.group(1))
    mult = 1_000_000.0 if m_size.group(2).upper() == "M" else 1_000_000_000.0
    magnitude = val * mult
    m_dir = _DIR_RX.search(line)
    hint = None
    if m_dir:
        d = m_dir.group(1).lower()
        if d == "bullish":
            hint = "up"
        elif d == "bearish":
            hint = "down"
    return (magnitude, hint)


def fetch_whale_alerts(spike: dict | None = None) -> list[dict]:
    """Return recent large on-chain transfers via Grok x_search.

    ``spike`` is accepted for signature symmetry with other Grok-backed
    fetchers but not used — whale activity is independent of which
    timeframe triggered the alert; we want the same lookup either way.
    """
    user_prompt = (
        "Find BTC and stablecoin transfers >= $10M reported by @whale_alert "
        "or @whale_alert_io in the last 2 hours. Format each on one line."
    )
    result = call_grok_x_search(_SYSTEM_PROMPT, user_prompt)
    if result is None:
        return []
    text, citations = result
    if not text or "該当なし" in text:
        log.info("Whale monitor: no transfers >= $10M in last 2h")
        return []

    items: list[dict] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        parsed = _parse_line(line)
        if parsed is None:
            continue
        magnitude_usd, dir_hint = parsed
        # Cite per-line if we have at least that many citations, else fall
        # back to the first one — Grok's annotation order tracks lines.
        cite_url = (
            citations[i] if i < len(citations) else (citations[0] if citations else "")
        )
        items.append({
            "type": "whale_transfer",
            "source": "Grok/X @whale_alert",
            "title": line[:240],
            "url": cite_url,
            "published": datetime.now(timezone.utc).isoformat(),
            "tags": ["whale_alert", "on_chain"],
            "magnitude_usd": magnitude_usd,
            "direction_hint": dir_hint,
        })
        if len(items) >= 5:  # safety cap matching system prompt
            break

    log.info(
        "Whale monitor OK (%d transfers, %d citations from Grok)",
        len(items), len(citations),
    )
    return items
