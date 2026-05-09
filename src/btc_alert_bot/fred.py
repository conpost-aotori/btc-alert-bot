"""FRED macro background context.

Pulls a small set of free, daily-updated macro indicators that provide
cross-asset context for BTC moves: dollar index proxy, US Treasury
yields, fed funds rate, equity vol. Cached to disk for 12h since these
series update at most once per business day.

Setup:
- Free API key at https://fredaccount.stlouisfed.org/apikeys
- Set ``FRED_API_KEY`` in env (or .env). If unset, this fetcher returns
  empty silently — the bot stays functional without a FRED account.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

FRED_BASE = "https://api.stlouisfed.org/fred"
# Tight per-request timeout — this is an *optional* factor; a slow FRED
# response shouldn't delay the alert pipeline.
FRED_TIMEOUT = 5

# Series we pull. Each tuple: (series_id, short_label, unit_suffix).
# All series are daily so we always have a recent print.
SERIES = [
    # NOTE: DTWEXBGS is FRED's broad trade-weighted dollar index (many
    # currencies, goods & services basis). It is NOT the same as ICE
    # Futures' DXY which uses only 6 currencies — the two move in the
    # same direction but levels differ. We label accordingly.
    ("DTWEXBGS", "BroadUSD",   ""),
    ("DGS10",    "US10Y",      "%"),
    ("DGS2",     "US2Y",       "%"),
    ("VIXCLS",   "VIX",        ""),
    ("DFF",      "FedFunds",   "%"),
]

CACHE_PATH = Path("data/fred_cache.json")
CACHE_TTL_HOURS = 12


# ---------------------------------------------------------------------------
# Single-series fetch
# ---------------------------------------------------------------------------

def _fetch_series(series_id: str, api_key: str) -> dict | None:
    """Return the most recent non-missing observation, or None."""
    try:
        resp = requests.get(
            f"{FRED_BASE}/series/observations",
            params={
                "series_id": series_id,
                "api_key": api_key,
                "file_type": "json",
                "limit": 5,           # last 5 obs to find one that isn't "."
                "sort_order": "desc",
            },
            timeout=FRED_TIMEOUT,
        )
        resp.raise_for_status()
        for o in resp.json().get("observations", []):
            v = o.get("value")
            if v and v != ".":
                return {"date": o["date"], "value": float(v)}
        return None
    except Exception as e:
        log.warning("FRED %s fetch failed: %s", series_id, e)
        return None


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _load_cache() -> dict | None:
    if not CACHE_PATH.exists():
        return None
    try:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(cache["fetched_at"])
        age_h = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600
        if age_h > CACHE_TTL_HOURS:
            return None
        return cache.get("data")
    except Exception as e:
        log.warning("FRED cache read failed: %s", e)
        return None


def _save_cache(data: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps(
                {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "data": data,
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning("FRED cache write failed: %s", e)


# ---------------------------------------------------------------------------
# Factor surface
# ---------------------------------------------------------------------------

def fetch_macro_background() -> list[dict]:
    """Top-level entry used by analyzers.gather_factors().

    Returns 0 or 1 items depending on whether enough series resolved.
    Never raises — failures degrade silently to an empty list.
    """
    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        return []  # opt-in feature

    data = _load_cache()
    if data is None:
        data = {}
        for series_id, _label, _unit in SERIES:
            obs = _fetch_series(series_id, api_key)
            if obs:
                data[series_id] = obs
        if not data:
            return []
        _save_cache(data)

    return [_format_factor(data)]


def _format_factor(data: dict) -> dict:
    """Build a single factor row summarizing the latest macro snapshot."""
    parts: list[str] = []
    latest_date = ""
    for series_id, label, unit in SERIES:
        d = data.get(series_id)
        if not d:
            continue
        parts.append(f"{label} {d['value']:.2f}{unit}")
        if d.get("date", "") > latest_date:
            latest_date = d["date"]

    # Yield curve flag (informational): inverted = US2Y > US10Y.
    us2 = data.get("DGS2")
    us10 = data.get("DGS10")
    if us2 and us10:
        spread = us10["value"] - us2["value"]
        curve_word = "順イールド" if spread > 0 else "逆イールド"
        parts.append(f"カーブ {spread:+.2f}% ({curve_word})")

    return {
        "type": "macro_background",
        "source": "FRED",
        "title": " / ".join(parts) if parts else "(no data)",
        "url": "https://fred.stlouisfed.org/",
        "published": latest_date,
    }
