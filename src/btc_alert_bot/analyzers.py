"""Parallel factor analysis: news, derivatives, macro events.

All sources are free / unauthenticated. Each fetcher is wrapped in
broad try/except so a single source failing does not break the alert.
"""
from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests

from .deribit import fetch_options_factor
from .fred import fetch_macro_background
from .x_monitor import fetch_x_monitor

log = logging.getLogger(__name__)

# Fast English news (CoinPost is intentionally excluded — too slow / translated).
NEWS_FEEDS = [
    ("The Block", "https://www.theblock.co/rss.xml"),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CoinTelegraph", "https://cointelegraph.com/rss/tag/bitcoin"),
]

# Exchange-official announcements often beat general media for listings,
# maintenance, hacks, regulatory responses, and margin changes — i.e. the
# events most likely to actually move BTC. These tend to NOT have the
# bitcoin/btc keyword in the title, so we accept them broader and let the
# scoring layer prioritize.
EXCHANGE_ANNOUNCEMENT_FEEDS = [
    ("Binance", "https://www.binance.com/en/support/announcement/c-48?navId=48&hl=en&__rss=1"),
    ("Coinbase", "https://blog.coinbase.com/feed"),
    ("Bybit", "https://announcements.bybit.com/en/?category=&page=1&__rss=1"),
    ("Kraken", "https://blog.kraken.com/feed/"),
]
# Lookback for exchange announcements is a bit longer because they fire less often.
EXCHANGE_LOOKBACK_MIN = 180

BYBIT_FUNDING_URL = "https://api.bybit.com/v5/market/funding/history"

# Look back this many minutes when scanning RSS for relevant items.
NEWS_LOOKBACK_MIN = 90

# Hard wall-time budget for gather_factors() so a single slow source can't
# block the whole alert pipeline. Stragglers are logged and dropped.
GATHER_FACTORS_DEADLINE_S = 30.0

# Surrounding window for matching macro events to the spike.
MACRO_WINDOW_HOURS = 2

# ForexFactory weekly calendar (custom XML, NOT standard RSS).
# Times in the feed are wall-clock America/New_York; we convert to UTC.
FOREXFACTORY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
FOREXFACTORY_TZ = ZoneInfo("America/New_York")
FOREXFACTORY_USER_AGENT = "Mozilla/5.0 (compatible; btc-alert-bot/0.1)"

# Country codes whose high-impact prints actually move BTC.
MACRO_RELEVANT_COUNTRIES = {"USD", "CNY"}

# Cache path + TTL — required because the public mirror rate-limits aggressive
# CI scrapers (we observed HTTP 429 on rapid retries). Weekly XML changes
# infrequently enough that 4h staleness is acceptable.
MACRO_CACHE_PATH = Path("data/macro_cache.json")
MACRO_CACHE_TTL_HOURS = 4


def _fetch_rss_items(
    feeds: list[tuple[str, str]],
    *,
    item_type: str,
    lookback_min: int,
    relevance_check,
    limit_per_feed: int = 15,
) -> list[dict]:
    """Generic RSS scraper used by both news and exchange-announcement fetchers."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_min)
    items: list[dict] = []
    for source, url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:limit_per_feed]:
                try:
                    pub_dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    continue
                if pub_dt < cutoff:
                    continue
                title = entry.get("title", "")
                if not relevance_check(title):
                    continue
                items.append({
                    "type": item_type,
                    "source": source,
                    "title": title,
                    "url": entry.get("link", ""),
                    "published": pub_dt.isoformat(),
                    "summary": entry.get("summary", "")[:300],
                })
        except Exception as e:
            log.warning("RSS fetch failed (%s): %s", source, e)
    return items


def fetch_news() -> list[dict]:
    """Pull recent BTC-relevant items from general crypto media RSS feeds."""
    return _fetch_rss_items(
        NEWS_FEEDS,
        item_type="news",
        lookback_min=NEWS_LOOKBACK_MIN,
        relevance_check=_is_btc_relevant,
    )


def fetch_exchange_announcements() -> list[dict]:
    """Pull recent items from major exchange announcement feeds.

    Filtering is broader here — exchange announcements about listings, hacks,
    maintenance, or margin changes can move BTC even without the word
    'bitcoin' in the title.
    """
    return _fetch_rss_items(
        EXCHANGE_ANNOUNCEMENT_FEEDS,
        item_type="exchange",
        lookback_min=EXCHANGE_LOOKBACK_MIN,
        relevance_check=_is_exchange_relevant,
    )


def fetch_derivatives() -> list[dict]:
    """Latest BTC perpetual funding rate from Bybit (free)."""
    try:
        resp = requests.get(
            BYBIT_FUNDING_URL,
            params={"category": "linear", "symbol": "BTCUSDT", "limit": 3},
            timeout=15,
        )
        resp.raise_for_status()
        rates = resp.json().get("result", {}).get("list", [])
        if not rates:
            return []
        latest = rates[0]
        rate_pct = float(latest["fundingRate"]) * 100
        # Annualized for context (funding paid every 8h → 3x/day → 365d).
        annualized = rate_pct * 3 * 365
        return [{
            "type": "derivatives",
            "source": "Bybit",
            "title": (
                f"BTC Perp Funding: {rate_pct:+.4f}% / 8h "
                f"(年率換算 {annualized:+.1f}%)"
            ),
            "url": "https://www.bybit.com/trade/usdt/BTCUSDT",
            "rate_pct": rate_pct,
        }]
    except Exception as e:
        log.warning("Bybit funding fetch failed: %s", e)
        return []


def _load_macro_cache() -> list[dict] | None:
    """Return cached macro events list if cache is fresh, else None."""
    if not MACRO_CACHE_PATH.exists():
        return None
    try:
        cache = json.loads(MACRO_CACHE_PATH.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(cache["fetched_at"])
        age_h = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600
        if age_h > MACRO_CACHE_TTL_HOURS:
            return None
        return cache["events"]
    except Exception as e:
        log.warning("Macro cache read failed: %s", e)
        return None


def _save_macro_cache(events: list[dict]) -> None:
    try:
        MACRO_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        MACRO_CACHE_PATH.write_text(
            json.dumps(
                {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "events": events,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning("Macro cache write failed: %s", e)


def _parse_forexfactory_xml(xml_text: str) -> list[dict]:
    """Parse the custom <weeklyevents><event>...</event></weeklyevents> XML.

    Returns a list of all events with parsed UTC datetimes. Filtering by
    impact / country / time-window happens in fetch_macro().
    """
    root = ET.fromstring(xml_text)
    events: list[dict] = []
    for ev in root.findall("event"):
        title = (ev.findtext("title") or "").strip()
        country = (ev.findtext("country") or "").strip()
        impact = (ev.findtext("impact") or "").strip()
        date_s = (ev.findtext("date") or "").strip()  # mm-dd-yyyy
        time_s = (ev.findtext("time") or "").strip()  # hh:mmam/pm or "All Day"
        url = (ev.findtext("url") or "").strip()
        if not (title and date_s and time_s):
            continue
        # Skip non-timed events — they won't anchor a 2h spike window meaningfully.
        if time_s.lower() in {"all day", "tentative", ""}:
            continue
        try:
            naive = datetime.strptime(f"{date_s} {time_s}", "%m-%d-%Y %I:%M%p")
            dt_utc = naive.replace(tzinfo=FOREXFACTORY_TZ).astimezone(timezone.utc)
        except Exception:
            continue
        events.append({
            "title": title,
            "country": country,
            "impact": impact,
            "url": url,
            "datetime_utc": dt_utc.isoformat(),
        })
    return events


def _fetch_forexfactory_events() -> list[dict] | None:
    """Network fetch + parse. Returns None on any failure (caller decides fallback)."""
    try:
        resp = requests.get(
            FOREXFACTORY_URL,
            headers={"User-Agent": FOREXFACTORY_USER_AGENT},
            timeout=15,
        )
        if resp.status_code == 429:
            log.warning("ForexFactory 429 rate-limited — will use cache if available")
            return None
        resp.raise_for_status()
        return _parse_forexfactory_xml(resp.text)
    except Exception as e:
        log.warning("ForexFactory fetch/parse failed: %s", e)
        return None


def fetch_macro() -> list[dict]:
    """Return high-impact USD/CNY macro events within ±MACRO_WINDOW_HOURS of now.

    Uses on-disk cache (data/macro_cache.json) with a TTL because the public
    feed mirror rate-limits aggressive scrapers. The cache is refreshed at
    most every MACRO_CACHE_TTL_HOURS hours, otherwise served from disk.
    """
    events = _load_macro_cache()
    if events is None:
        events = _fetch_forexfactory_events()
        if events is not None:
            _save_macro_cache(events)
        else:
            # Fetch failed and no fresh cache — return empty rather than error.
            return []

    now = datetime.now(timezone.utc)
    cutoff_past = now - timedelta(hours=MACRO_WINDOW_HOURS)
    cutoff_future = now + timedelta(hours=MACRO_WINDOW_HOURS)
    items: list[dict] = []
    for ev in events:
        if ev.get("impact") != "High":
            continue
        if ev.get("country") not in MACRO_RELEVANT_COUNTRIES:
            continue
        try:
            ev_dt = datetime.fromisoformat(ev["datetime_utc"])
        except Exception:
            continue
        if not (cutoff_past <= ev_dt <= cutoff_future):
            continue
        items.append({
            "type": "macro",
            "source": "ForexFactory",
            "title": f"[{ev['country']}] {ev['title']}",
            "url": ev.get("url", ""),
            "published": ev["datetime_utc"],
        })
    return items


def gather_factors(spike: dict) -> list[dict]:  # noqa: ARG001 (spike reserved for future scoring)
    """Run all analyzers in parallel and return ranked candidate factors.

    A hard deadline (GATHER_FACTORS_DEADLINE_S) bounds total wall time —
    any analyzer that misses the deadline is dropped from this alert and
    will be retried on the next cron tick.
    """
    fetchers = [
        fetch_news,
        fetch_exchange_announcements,
        fetch_derivatives,
        fetch_macro,
        fetch_options_factor,    # Deribit IV / term structure / skew / RV
        fetch_macro_background,  # FRED daily DXY/yields/VIX/FedFunds
        fetch_x_monitor,         # Nitter RSS for high-signal X accounts
    ]
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(fetchers)) as ex:
        futures = {ex.submit(f): f.__name__ for f in fetchers}
        try:
            for fut in as_completed(futures, timeout=GATHER_FACTORS_DEADLINE_S):
                name = futures[fut]
                try:
                    results.extend(fut.result())
                except Exception as e:
                    log.warning("Analyzer %s crashed: %s", name, e)
        except TimeoutError:
            stragglers = [n for f, n in futures.items() if not f.done()]
            log.warning(
                "gather_factors deadline %.0fs exceeded; dropping: %s",
                GATHER_FACTORS_DEADLINE_S, ", ".join(stragglers),
            )
            for f in futures:
                f.cancel()

    # Priority order: imminent confirmed events > X (high-signal accounts
    # often beat media on breaking news) > exchange announcements >
    # general media > spot derivatives > options positioning > FRED daily
    # background.
    type_priority = {
        "macro": 0,
        "x_monitor": 1,
        "exchange": 2,
        "news": 3,
        "derivatives": 4,
        "options": 5,
        "macro_background": 6,
    }
    results.sort(key=lambda x: type_priority.get(x["type"], 9))
    return results[:10]


def _is_btc_relevant(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in ("bitcoin", "btc", "crypto", "etf"))


def _is_exchange_relevant(title: str) -> bool:
    """Exchange announcements: accept anything that smells market-moving.

    We deliberately keep this broad because the scoring layer downstream
    (and Gemini) can drop irrelevant items, but missing a hack or margin
    change announcement is far more costly than including a noisy listing.
    """
    t = title.lower()
    keywords = (
        "bitcoin", "btc", "futures", "perpetual", "margin", "leverage",
        "delist", "list", "halt", "suspend", "maintenance", "hack",
        "exploit", "incident", "regulation", "license", "withdraw",
        "deposit", "fund", "circuit",
    )
    return any(k in t for k in keywords)
