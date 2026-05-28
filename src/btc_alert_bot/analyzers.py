"""Parallel factor analysis: news, derivatives, macro events, social.

All sources are free / unauthenticated (a couple require a free API key
that's opt-in via env var). Each fetcher is wrapped in broad try/except
so a single source failing does not break the alert.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

import feedparser
import requests

from .deribit import fetch_options_factor
from .derivatives import fetch_derivatives_context
from .fred import fetch_macro_background
from .grok_search import fetch_grok_x_search
from .whale_monitor import fetch_whale_alerts
from .x_list_monitor import fetch_x_list_signals
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

# Aggregator news (Google News RSS): single endpoint, 500+ underlying outlets.
# Useful as a recall-boost layer — duplicates with the dedicated feeds are
# resolved by the dedup step downstream.
GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?"
    "q=bitcoin+OR+BTC+when:6h&hl=en-US&gl=US&ceid=US:en"
)

# Reddit RSS — early signal for community-known events (rumors before media).
# Reddit requires a unique User-Agent. /new/ is chronological so we always
# pick the freshest items, then filter by keyword/title.
REDDIT_FEEDS = [
    ("r/Bitcoin", "https://www.reddit.com/r/Bitcoin/new/.rss"),
    ("r/CryptoMarkets", "https://www.reddit.com/r/CryptoMarkets/new/.rss"),
]
REDDIT_USER_AGENT = "btc-alert-bot/0.2 (+https://github.com/VirtualNISHI/btc-alert-bot)"
REDDIT_LOOKBACK_MIN = 60
REDDIT_TIMEOUT = 8

# CryptoPanic aggregates ~50 crypto news outlets. Free tier requires a token
# (free signup at https://cryptopanic.com/developers/api/). When unset, the
# fetcher silently returns []. Refresh cadence on the free tier is ~5min.
CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"
CRYPTOPANIC_LOOKBACK_MIN = 60
CRYPTOPANIC_TIMEOUT = 8

# Look back this many minutes when scanning RSS for relevant items.
NEWS_LOOKBACK_MIN = 90

# Hard wall-time budget for gather_factors() so a single slow source can't
# block the whole alert pipeline. Stragglers are logged and dropped.
# Raised 30 → 40s after adding Grok x_search which takes 15-25s for its
# multi-step tool-call pipeline (keyword + semantic search + synthesis).
GATHER_FACTORS_DEADLINE_S = 40.0

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


def gather_factors(spike: dict | None = None) -> list[dict]:
    """Run all analyzers in parallel, dedupe, score, and return top factors.

    A hard deadline (GATHER_FACTORS_DEADLINE_S) bounds total wall time —
    any analyzer that misses the deadline is dropped from this alert.

    The result is ranked by ``rank_factors()``, which combines source
    credibility, recency, keyword strength, direction match, and
    cross-source corroboration. Items are deduplicated first so the
    Gemini summarizer doesn't mistake aggregator copies for independent
    confirmations.
    """
    # Most fetchers are parameter-less; Grok needs the spike context so
    # it can query X for the exact move that just fired. We submit it
    # separately with the spike bound rather than refactoring every other
    # fetcher's signature.
    parameterless_fetchers = [
        # Primary news + aggregators
        fetch_news,
        fetch_google_news,
        fetch_cryptopanic,
        # Exchange + macro-event triggers
        fetch_exchange_announcements,
        fetch_macro,
        # Market microstructure (amplifiers)
        fetch_derivatives_context,
        fetch_options_factor,
        # Macro / social context
        fetch_macro_background,
        fetch_reddit_signal,
        fetch_x_monitor,
    ]
    # Grok-backed fetchers all share the same /v1/responses + x_search
    # transport; running them in parallel just multiplies the wall-time
    # safe ceiling (each ~10-25s, parallel total still ~25s). Each is
    # an independent question (general drivers / whales / curated list)
    # so we deliberately don't merge them — the ranker uses the type
    # distinction for credibility weighting.
    grok_fetchers = [
        (fetch_grok_x_search, "fetch_grok_x_search"),
        (fetch_whale_alerts, "fetch_whale_alerts"),
        (fetch_x_list_signals, "fetch_x_list_signals"),
    ]
    raw: list[dict] = []
    with ThreadPoolExecutor(
        max_workers=len(parameterless_fetchers) + len(grok_fetchers)
    ) as ex:
        futures = {
            ex.submit(f): f.__name__ for f in parameterless_fetchers
        }
        for fn, name in grok_fetchers:
            futures[ex.submit(fn, spike)] = name
        try:
            for fut in as_completed(futures, timeout=GATHER_FACTORS_DEADLINE_S):
                name = futures[fut]
                try:
                    raw.extend(fut.result())
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

    deduped, dup_groups = _deduplicate_factors(raw)
    return rank_factors(deduped, spike, dup_groups, top_k=10)


# ---------------------------------------------------------------------------
# Deduplication: collapse aggregator copies of the same story
# ---------------------------------------------------------------------------

# Stop-words ignored when fingerprinting titles for similarity.
_TITLE_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "in", "on",
    "at", "by", "for", "and", "or", "but", "as", "with", "after", "amid",
    "btc", "bitcoin", "crypto", "says", "report", "amid", "while",
}


def _normalize_url(url: str) -> str:
    """Strip query strings + fragments + trailing slashes for dedup."""
    if not url:
        return ""
    try:
        p = urlparse(url)
        # Drop tracking params like ?utm_source=...; keep only path identity.
        return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", "", ""))
    except Exception:
        return url


def _title_fingerprint(title: str) -> str:
    """Coarse fingerprint of a headline: lowercase, alphanum, no stopwords.

    Two titles with the same fingerprint are extremely likely to be the
    same story published by different outlets (e.g. Reuters → CoinDesk,
    Reuters → CryptoPanic, Reuters → Google News).
    """
    if not title:
        return ""
    words = re.findall(r"[a-z0-9]+", title.lower())
    significant = [w for w in words if w not in _TITLE_STOPWORDS and len(w) > 2]
    # Truncate to first ~10 significant tokens — enough for identity, not so
    # narrow that minor edits break the match.
    key = "-".join(significant[:10])
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16] if key else ""


def _deduplicate_factors(items: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Collapse near-duplicate news items.

    Returns ``(unique_items, dup_count_by_key)``. The dup count is fed to
    the ranker so we can *reward* corroborated stories without inflating
    the prompt with copies. Per dup group we keep the earliest-published
    item (more credit to the original source).
    """
    by_key: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for it in items:
        # Non-text factors (derivatives / options / macro_background) get
        # unique synthetic keys so they're never deduped against each other.
        if it.get("type", "").startswith(("derivatives", "options", "macro_background")):
            key = f"struct:{it.get('type')}:{it.get('source')}"
            by_key.setdefault(key, it)
            continue

        # For news/social items, title fingerprint is a better cross-source
        # identity than URL — the same headline appears at different URLs
        # when it's syndicated through aggregators (Google News etc.).
        title_key = _title_fingerprint(it.get("title", ""))
        url_key = _normalize_url(it.get("url", ""))
        key = title_key or url_key
        if not key:
            # Last resort: random-ish key so we never silently drop a unique item.
            key = f"raw:{id(it)}"

        counts[key] = counts.get(key, 0) + 1
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = it
            continue
        # Keep the earliest-published copy. Prefer dedicated-feed items
        # over aggregator items when timestamps tie (originals over copies).
        existing_pub = existing.get("published") or ""
        cur_pub = it.get("published") or ""
        if cur_pub < existing_pub:
            by_key[key] = it
        elif cur_pub == existing_pub and not existing.get("type", "").startswith("news_aggregator"):
            # Existing is already a dedicated feed; keep it.
            pass
        elif cur_pub == existing_pub and not it.get("type", "").startswith("news_aggregator"):
            # Replace aggregator copy with dedicated feed copy.
            by_key[key] = it
    return list(by_key.values()), counts


# ---------------------------------------------------------------------------
# Ranking: source × recency × keyword × direction × corroboration
# ---------------------------------------------------------------------------

# Source credibility weights (higher = more authoritative for a market move).
_SOURCE_WEIGHTS = {
    "macro": 40,                 # imminent confirmed govt/macro release
    "exchange": 38,              # listing / hack / halt is binary truth
    "whale_transfer": 34,        # observed on-chain flow (>= $10M) — hard evidence
    "x_list": 33,                # curated high-signal account list (Saylor/Trump/etc)
    "x_search_grok": 32,         # Grok broad X search — AI-curated
    "derivatives_liq": 30,       # observed cascade
    "news": 26,                  # dedicated crypto media
    "derivatives_pos": 22,
    "news_aggregator": 20,       # Google News / CryptoPanic
    "derivatives_fund": 18,
    "options": 16,               # Deribit IV/skew = backdrop
    "macro_background": 12,      # FRED daily = backdrop
    "social_reddit": 10,
    "x_monitor": 8,              # Nitter fallback — usually broken, deprioritized
    "derivatives": 18,           # legacy bucket
}

_BULL_KEYWORDS = (
    "etf", "approval", "approve", "ruling", "win", "rally", "soar",
    "surge", "buy", "accumulate", "treasury buys", "spot etf",
    "short squeeze", "short liquidation",
)
_BEAR_KEYWORDS = (
    "hack", "exploit", "outage", "halt", "delist", "ban", "lawsuit",
    "indict", "fraud", "investigation", "crash", "dump", "sell-off",
    "selloff", "long liquidation", "depeg", "bankrupt", "outflow",
    "sec sue", "sec charge",
)
_HIGH_SIGNAL_KEYWORDS = (
    "fomc", "fed", "powell", "cpi", "ppi", "nfp", "jobs", "treasury",
    "etf", "sec", "binance", "coinbase", "tether", "usdt", "usdc",
    "regulation", "stablecoin", "halving", "fork",
)


def _classify_keywords(title: str) -> tuple[int, str | None]:
    """Return (keyword_weight, direction_hint)."""
    t = (title or "").lower()
    weight = 0
    if any(k in t for k in _HIGH_SIGNAL_KEYWORDS):
        weight += 18
    direction: str | None = None
    if any(k in t for k in _BULL_KEYWORDS):
        weight += 12
        direction = "up"
    if any(k in t for k in _BEAR_KEYWORDS):
        weight += 12
        # If both directions trigger, leave hint None — ambiguous.
        direction = None if direction else "down"
    return weight, direction


def _recency_weight(published: str | None, now: datetime) -> int:
    if not published:
        return 0
    try:
        ts = datetime.fromisoformat(published)
    except Exception:
        return 0
    # Some sources publish naive timestamps; assume UTC.
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    elapsed_min = (now - ts).total_seconds() / 60
    if elapsed_min < 0:
        return 5  # future events (e.g. macro calendar) — small bonus
    if elapsed_min <= 10:
        return 30
    if elapsed_min <= 30:
        return 22
    if elapsed_min <= 60:
        return 14
    if elapsed_min <= 180:
        return 6
    return -10  # actively penalize stale items


def _score_factor(
    factor: dict,
    spike: dict | None,
    corroboration: int,
    now: datetime,
) -> int:
    score = _SOURCE_WEIGHTS.get(factor.get("type", ""), 5)
    score += _recency_weight(factor.get("published"), now)
    kw_weight, kw_direction = _classify_keywords(factor.get("title", ""))
    score += kw_weight

    # Direction match — does this factor point the same way as the spike?
    spike_dir = (spike or {}).get("direction") if spike else None
    factor_dir = factor.get("direction_hint") or kw_direction
    if spike_dir and factor_dir:
        if factor_dir == spike_dir:
            score += 15
        else:
            score -= 8  # mismatched direction is suspicious

    # Corroboration bonus: this story showed up in multiple aggregators.
    if corroboration >= 3:
        score += 25
    elif corroboration == 2:
        score += 12

    # Magnitude bonus for liquidations (USD scale).
    mag = factor.get("magnitude_usd") or 0
    if mag >= 50_000_000:
        score += 25
    elif mag >= 10_000_000:
        score += 12
    elif mag >= 1_000_000:
        score += 4

    return score


def rank_factors(
    factors: list[dict],
    spike: dict | None,
    corroboration: dict[str, int],
    *,
    top_k: int = 10,
) -> list[dict]:
    """Score every factor, sort desc, return the top_k.

    Mutates each factor in-place to add ``_score`` and ``_corroboration``
    so the summarizer can show them in the prompt for transparency.
    """
    now = datetime.now(timezone.utc)
    scored: list[tuple[int, dict]] = []
    for f in factors:
        # Recompute the same key the dedup step used so corroboration
        # lookups actually hit. Order matters: title fingerprint first
        # (cross-aggregator identity), URL second.
        ftype = f.get("type", "")
        if ftype.startswith(("derivatives", "options", "macro_background")):
            key = f"struct:{ftype}:{f.get('source')}"
        else:
            title_key = _title_fingerprint(f.get("title", ""))
            url_key = _normalize_url(f.get("url", ""))
            key = title_key or url_key or f"raw:{id(f)}"
        corr = corroboration.get(key, 1)
        s = _score_factor(f, spike, corr, now)
        f["_score"] = s
        f["_corroboration"] = corr
        scored.append((s, f))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [f for _, f in scored[:top_k]]


def _is_btc_relevant(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in ("bitcoin", "btc", "crypto", "etf"))


# ---------------------------------------------------------------------------
# Google News RSS (aggregator) — recall layer
# ---------------------------------------------------------------------------

def fetch_google_news() -> list[dict]:
    """BTC keyword search on Google News RSS — 500+ outlets aggregated.

    Latency: ~5 min behind original publish, but coverage is far wider
    than the dedicated CoinDesk/Block/CoinTelegraph trio. The dedup step
    downstream collapses duplicates with the same story.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=NEWS_LOOKBACK_MIN)
    items: list[dict] = []
    try:
        feed = feedparser.parse(GOOGLE_NEWS_RSS)
        for entry in feed.entries[:25]:
            try:
                pub_dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            except Exception:
                continue
            if pub_dt < cutoff:
                continue
            title = entry.get("title", "")
            if not _is_btc_relevant(title):
                continue
            # Google News titles look like "Headline - Outlet Name". Split
            # the outlet off so the dedup fingerprint matches dedicated
            # feeds (e.g. CoinDesk RSS produces the same headline minus
            # the suffix).
            outlet = "Google News"
            clean_title = title
            if " - " in title:
                head, _, tail = title.rpartition(" - ")
                outlet = tail.strip() or outlet
                clean_title = head.strip() or title
            items.append({
                "type": "news_aggregator",
                "source": f"GoogleNews/{outlet}",
                "title": clean_title,
                "url": entry.get("link", ""),
                "published": pub_dt.isoformat(),
                "summary": entry.get("summary", "")[:300],
            })
    except Exception as e:
        log.warning("Google News fetch failed: %s", e)
    return items


# ---------------------------------------------------------------------------
# Reddit RSS — community early signal
# ---------------------------------------------------------------------------

def fetch_reddit_signal() -> list[dict]:
    """Recent /r/Bitcoin and /r/CryptoMarkets posts.

    High-noise but sometimes catches rumors / outages before the media
    picks them up. Filter aggressively on title keywords so we don't
    flood the prompt with shitposts.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=REDDIT_LOOKBACK_MIN)
    items: list[dict] = []
    headers = {"User-Agent": REDDIT_USER_AGENT}
    for subreddit, url in REDDIT_FEEDS:
        try:
            resp = requests.get(url, headers=headers, timeout=REDDIT_TIMEOUT)
            if resp.status_code != 200:
                log.warning("Reddit %s HTTP %d", subreddit, resp.status_code)
                continue
            feed = feedparser.parse(resp.text)
            for entry in feed.entries[:25]:
                try:
                    pub_dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    continue
                if pub_dt < cutoff:
                    continue
                title = entry.get("title", "")
                if not _is_reddit_signal(title):
                    continue
                items.append({
                    "type": "social_reddit",
                    "source": subreddit,
                    "title": title[:200],
                    "url": entry.get("link", ""),
                    "published": pub_dt.isoformat(),
                })
        except Exception as e:
            log.warning("Reddit fetch failed (%s): %s", subreddit, e)
    return items


def _is_reddit_signal(title: str) -> bool:
    """Aggressive filter so /r/Bitcoin's daily memes don't drown the prompt."""
    t = (title or "").lower()
    # Strong signal keywords — events that actually move price.
    strong = (
        "hack", "exploit", "outage", "halt", "suspend", "delist", "list",
        "etf", "approval", "rejection", "sec", "fed", "fomc", "cpi", "ppi",
        "powell", "treasury", "lawsuit", "indict", "ban", "regulation",
        "liquidation", "whale", "dump", "pump", "rally", "crash", "flash",
        "binance", "coinbase", "bybit", "tether", "usdt", "usdc", "stable",
        "bitcoin", "btc",
    )
    return any(k in t for k in strong)


# ---------------------------------------------------------------------------
# CryptoPanic API — multi-outlet aggregator (opt-in)
# ---------------------------------------------------------------------------

def fetch_cryptopanic() -> list[dict]:
    """CryptoPanic posts API (~50 outlet aggregator).

    Free tier requires a token from https://cryptopanic.com/developers/api/
    If CRYPTOPANIC_AUTH_TOKEN is unset, returns [] silently.
    Refresh cadence on free tier is ~5min, so we don't poll faster than that.
    """
    token = os.getenv("CRYPTOPANIC_AUTH_TOKEN")
    if not token:
        return []
    try:
        resp = requests.get(
            CRYPTOPANIC_URL,
            params={
                "auth_token": token,
                "currencies": "BTC",
                "filter": "important",  # surface market-moving items first
                "kind": "news",
            },
            timeout=CRYPTOPANIC_TIMEOUT,
        )
        if resp.status_code != 200:
            log.warning("CryptoPanic HTTP %d", resp.status_code)
            return []
        data = resp.json()
    except Exception as e:
        log.warning("CryptoPanic fetch failed: %s", e)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=CRYPTOPANIC_LOOKBACK_MIN)
    items: list[dict] = []
    for r in data.get("results", []) or []:
        published_str = r.get("published_at") or r.get("created_at")
        try:
            pub_dt = datetime.fromisoformat(
                (published_str or "").replace("Z", "+00:00")
            )
        except Exception:
            continue
        if pub_dt < cutoff:
            continue
        outlet = (r.get("source") or {}).get("title", "CryptoPanic")
        items.append({
            "type": "news_aggregator",
            "source": f"CryptoPanic/{outlet}",
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "published": pub_dt.isoformat(),
            # CryptoPanic flags: 'important', 'positive', 'negative', etc.
            "tags": list((r.get("kind"),) + tuple(r.get("votes", {}).keys())),
        })
    return items


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
