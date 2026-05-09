"""X (Twitter) account monitoring via Nitter RSS.

Nitter is a privacy-preserving Twitter frontend that exposes per-user
RSS feeds. Public instances are unstable — many are rate-limited,
broken, or have removed list support. We try each configured instance
until one returns non-empty parsed entries, then move on to the next
account.

Configuration:
- ``NITTER_ACCOUNTS``: comma-separated X usernames (no @ prefix) to
  monitor. Empty / unset → this analyzer is silently disabled.
- ``NITTER_INSTANCES``: comma-separated Nitter base URLs to try.
  Defaults to a small list of currently-best-known instances. If you
  self-host Nitter or have a paid alternative, point this at it.

Caveats:
- This is a *best-effort* feature. Nitter availability shifts week to
  week. Failures are logged at WARN and degrade silently to empty.
- GitHub Actions runner IPs are sometimes blocked by Nitter providers.
  If the analyzer never returns data in CI, that's the most likely cause.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests

log = logging.getLogger(__name__)

# Default instances rotate as the public Nitter ecosystem changes. Keep
# the list short — long fallbacks just slow the alert path on bad days.
DEFAULT_INSTANCES = [
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.tiekoetter.com",
]

# Tight per-request timeout: optional factor, must not delay the alert.
NITTER_TIMEOUT = 5

# Hard wall-time budget for the *entire* x_monitor pass. With multiple
# accounts × multiple dead instances the naive loop could otherwise
# eat 45s+ when Nitter is down — well past gather_factors's 30s deadline.
TOTAL_BUDGET_S = 15.0

# Look back this many minutes for tweet recency.
LOOKBACK_MIN = 60

# Cap on items returned to keep the prompt small.
MAX_ITEMS = 8

_session = requests.Session()
_session.headers.update({"User-Agent": "btc-alert-bot/0.1"})


def _try_instance(instance: str, account: str) -> list | None:
    """Fetch one Nitter instance/account pair. Returns parsed entries or None."""
    url = f"{instance.rstrip('/')}/{account}/rss"
    try:
        resp = _session.get(url, timeout=NITTER_TIMEOUT)
        if resp.status_code != 200:
            return None
        feed = feedparser.parse(resp.text)
        if not feed.entries:
            return None
        return list(feed.entries)
    except Exception:
        return None


def _fetch_account(
    account: str,
    instances: list[str],
    deadline: float,
) -> tuple[list, str | None]:
    """Try each instance in order; first non-empty wins.

    Returns ``(entries, working_instance)``. ``working_instance`` is the
    one that produced data, so the caller can prefer it for subsequent
    accounts and avoid re-probing dead hosts. Aborts early if the total
    deadline has been crossed.
    """
    for inst in instances:
        if time.monotonic() > deadline:
            log.warning(
                "Nitter total budget exceeded before @%s on %s", account, inst
            )
            return [], None
        items = _try_instance(inst, account)
        if items:
            log.info(
                "Nitter %s OK for @%s (%d entries)", inst, account, len(items)
            )
            return items, inst
    log.warning("All Nitter instances failed for @%s", account)
    return [], None


def _is_btc_relevant(title: str) -> bool:
    """Loose relevance filter — high-signal accounts post other things too."""
    t = (title or "").lower()
    keywords = (
        "bitcoin", "btc", "crypto", "etf", "fomc", "fed", "cpi",
        "powell", "sec", "binance", "coinbase", "tether", "stable",
        "halving", "etf", "treasury", "trump", "xrp",
    )
    return any(k in t for k in keywords)


def fetch_x_monitor() -> list[dict]:
    """Pull recent BTC-relevant tweets from monitored X accounts.

    Returns [] silently when NITTER_ACCOUNTS is unset (opt-in feature).
    Never raises — failures degrade to fewer items.
    """
    accounts_raw = (os.getenv("NITTER_ACCOUNTS") or "").strip()
    if not accounts_raw:
        return []
    accounts = [a.strip().lstrip("@") for a in accounts_raw.split(",") if a.strip()]
    if not accounts:
        return []

    instances_raw = (os.getenv("NITTER_INSTANCES") or "").strip()
    if instances_raw:
        instances = [
            i.strip().rstrip("/") for i in instances_raw.split(",") if i.strip()
        ]
    else:
        instances = DEFAULT_INSTANCES

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MIN)
    deadline = time.monotonic() + TOTAL_BUDGET_S
    items: list[dict] = []
    sticky: str | None = None  # last-known-working instance, tried first
    for account in accounts:
        if time.monotonic() > deadline:
            log.warning(
                "Nitter total budget hit; remaining accounts skipped: %s",
                accounts[accounts.index(account):],
            )
            break
        # Front-load the sticky instance if we have one — most likely to succeed.
        ordered = (
            [sticky] + [i for i in instances if i != sticky]
            if sticky else instances
        )
        entries, winner = _fetch_account(account, ordered, deadline)
        if winner:
            sticky = winner
        for entry in entries:
            try:
                pub_dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            except Exception:
                continue
            if pub_dt < cutoff:
                continue
            title = entry.get("title", "")
            if not _is_btc_relevant(title):
                continue
            items.append({
                "type": "x_monitor",
                "source": f"@{account}",
                "title": title[:200],
                "url": entry.get("link", ""),
                "published": pub_dt.isoformat(),
            })

    items.sort(key=lambda x: x["published"], reverse=True)
    return items[:MAX_ITEMS]
