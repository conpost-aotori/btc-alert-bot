"""Deribit options-market context — IV, term structure, skew, RV.

All endpoints are public/free/no-auth. We compute four aggregates that
explain price moves in terms of options-market positioning:

- ATM IV (near / mid / far expiries) — implied vol the market is paying.
- Term structure (far - near) — flags risk pricing across horizons.
  Negative = backwardation = "stress now"; positive = contango.
- 25Δ-proxy Risk Reversal (mid expiry) — call IV vs put IV at strikes
  ~7.5% above/below spot. Negative = put skew ("downside hedging");
  positive = call skew ("upside chase"). True 25Δ requires per-contract
  greeks (extra N round-trips); the % offset is a reasonable proxy.
- 30d Realized Vol from Deribit's dedicated endpoint. IV - RV gives
  the volatility risk premium.

The output is shaped as a single factor entry consumable by
analyzers.gather_factors().
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

DERIBIT_BASE = "https://www.deribit.com/api/v2"
# Tight per-request timeout: this is an *optional* factor, so we'd rather
# return empty than delay the whole alert. Three sequential calls at 5s
# each puts the worst case at ~15s.
TIMEOUT = 5

# Target days-to-expiry for the term-structure points. We pick whichever
# listed expiry is closest to each target.
TARGET_DTE = {"near": 7, "mid": 30, "far": 90}

# Strike offsets used as ~25Δ proxies. 7.5% is roughly 25Δ for a 30d
# option at 60% IV — close enough for a directional skew read.
SKEW_CALL_OFFSET_PCT = 7.5
SKEW_PUT_OFFSET_PCT = -7.5

# Sample expiry → "BTC-22MAY26-75000-C"
_OPT_RE = re.compile(r"^BTC-(\d{1,2}[A-Z]{3}\d{2})-(\d+)-([CP])$")

_session = requests.Session()
_session.headers.update({"User-Agent": "btc-alert-bot/0.1"})


# ---------------------------------------------------------------------------
# Low-level fetchers
# ---------------------------------------------------------------------------

def _get(path: str, params: dict | None = None):
    resp = _session.get(f"{DERIBIT_BASE}{path}", params=params or {}, timeout=TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    if "result" not in payload:
        raise RuntimeError(f"Deribit unexpected payload: {payload}")
    return payload["result"]


def fetch_btc_index() -> float:
    """Current BTC index price (Deribit composite spot)."""
    return float(_get("/public/get_index_price", {"index_name": "btc_usd"})["index_price"])


def _parse_instrument(name: str) -> dict | None:
    m = _OPT_RE.match(name)
    if not m:
        return None
    expiry_s, strike_s, kind = m.groups()
    try:
        expiry = datetime.strptime(expiry_s, "%d%b%y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return {"expiry": expiry, "strike": int(strike_s), "kind": kind}


def fetch_option_book() -> list[dict]:
    """All BTC option contracts with ``mark_iv`` (~900-1000 contracts)."""
    items = _get(
        "/public/get_book_summary_by_currency",
        {"currency": "BTC", "kind": "option"},
    )
    out: list[dict] = []
    for it in items:
        meta = _parse_instrument(it.get("instrument_name", ""))
        if not meta:
            continue
        mark_iv = it.get("mark_iv")
        if mark_iv is None:
            continue
        out.append({**meta, "mark_iv": float(mark_iv)})
    return out


def fetch_realized_vol() -> float | None:
    """Latest annualized realized vol from Deribit's HV endpoint.

    The endpoint returns a series ``[[ts_ms, hv_pct], ...]``. We take the
    most recent sample as the headline RV figure.
    """
    try:
        result = _get("/public/get_historical_volatility", {"currency": "BTC"})
    except Exception as e:
        log.warning("Deribit HV fetch failed: %s", e)
        return None
    if not result:
        return None
    return float(result[-1][1])


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------

def _select_expiry(expiries: list[datetime], target_days: int) -> datetime | None:
    """Pick the expiry whose days-to-expiry is closest to target_days."""
    if not expiries:
        return None
    now = datetime.now(timezone.utc)
    return min(
        expiries,
        key=lambda e: abs((e - now).total_seconds() / 86400 - target_days),
    )


def _atm_iv_for_expiry(book: list[dict], expiry: datetime, spot: float) -> float | None:
    """Average IV of the call & put closest to spot at the given expiry."""
    same = [b for b in book if b["expiry"] == expiry]
    if not same:
        return None
    calls = [b for b in same if b["kind"] == "C"]
    puts = [b for b in same if b["kind"] == "P"]
    nearest_call = min(calls, key=lambda b: abs(b["strike"] - spot)) if calls else None
    nearest_put = min(puts, key=lambda b: abs(b["strike"] - spot)) if puts else None
    ivs = [b["mark_iv"] for b in (nearest_call, nearest_put) if b]
    if not ivs:
        return None
    return sum(ivs) / len(ivs)


def _skew_iv(
    book: list[dict], expiry: datetime, spot: float, offset_pct: float, kind: str
) -> float | None:
    """IV at the strike closest to spot * (1 + offset_pct/100), filtered by kind."""
    target_strike = spot * (1 + offset_pct / 100)
    same = [b for b in book if b["expiry"] == expiry and b["kind"] == kind]
    if not same:
        return None
    nearest = min(same, key=lambda b: abs(b["strike"] - target_strike))
    return nearest["mark_iv"]


def compute_options_context() -> dict:
    """Single call surface — fetch and aggregate all options-market signals.

    Returns a dict with keys:
        spot, rv_recent, expiry_{near,mid,far}, dte_{near,mid,far},
        atm_iv_{near,mid,far}, skew_25d_30d_proxy, term_structure_pct
    Missing keys mean that aggregate could not be computed.
    """
    spot = fetch_btc_index()
    book = fetch_option_book()
    rv_recent = fetch_realized_vol()

    out: dict = {"spot": spot, "rv_recent": rv_recent}
    expiries = sorted({b["expiry"] for b in book})

    for label, target in TARGET_DTE.items():
        exp = _select_expiry(expiries, target)
        if not exp:
            continue
        atm_iv = _atm_iv_for_expiry(book, exp, spot)
        if atm_iv is None:
            continue
        out[f"expiry_{label}"] = exp.isoformat()
        out[f"dte_{label}"] = round(
            (exp - datetime.now(timezone.utc)).total_seconds() / 86400, 1
        )
        out[f"atm_iv_{label}"] = round(atm_iv, 2)

    near, far = out.get("atm_iv_near"), out.get("atm_iv_far")
    if near is not None and far is not None:
        out["term_structure_pct"] = round(far - near, 2)

    # Skew at the mid expiry.
    mid_exp_iso = out.get("expiry_mid")
    if mid_exp_iso:
        mid_exp = datetime.fromisoformat(mid_exp_iso)
        call_iv = _skew_iv(book, mid_exp, spot, SKEW_CALL_OFFSET_PCT, "C")
        put_iv = _skew_iv(book, mid_exp, spot, SKEW_PUT_OFFSET_PCT, "P")
        if call_iv is not None and put_iv is not None:
            out["skew_25d_30d_proxy"] = round(call_iv - put_iv, 2)

    return out


# ---------------------------------------------------------------------------
# Factor formatter — turns the context into a gather_factors() entry
# ---------------------------------------------------------------------------

def options_to_factor(ctx: dict) -> dict | None:
    """Convert options context into a single ``factor`` dict for analyzers.py.

    Returns None if too little data was retrievable for a useful summary.
    """
    if not ctx:
        return None

    parts: list[str] = []

    # Term structure read.
    near = ctx.get("atm_iv_near")
    mid = ctx.get("atm_iv_mid")
    far = ctx.get("atm_iv_far")
    ts = ctx.get("term_structure_pct")
    if near is not None and mid is not None and far is not None and ts is not None:
        if ts > 1.0:
            shape = "コンタンゴ"  # forward IV > spot IV → calm now
        elif ts < -1.0:
            shape = "バックワーデーション"  # forward IV < spot IV → stress now
        else:
            shape = "フラット"
        parts.append(
            f"ATM IV {near:.0f}/{mid:.0f}/{far:.0f}% ({shape} {ts:+.1f})"
        )

    # Skew read.
    skew = ctx.get("skew_25d_30d_proxy")
    if skew is not None:
        if skew < -2.0:
            skew_word = "プット高 (下方ヘッジ需要)"
        elif skew > 2.0:
            skew_word = "コール高 (上方追随)"
        else:
            skew_word = "中立"
        parts.append(f"30d Skew {skew:+.1f}% ({skew_word})")

    # Vol risk premium.
    rv = ctx.get("rv_recent")
    if mid is not None and rv is not None:
        vrp = mid - rv
        # VRP positive (IV>RV) is normal; very negative is rare and noteworthy.
        if abs(vrp) >= 5:
            parts.append(f"IV-RV {vrp:+.1f}%")

    if not parts:
        return None

    return {
        "type": "options",
        "source": "Deribit",
        "title": " / ".join(parts),
        "url": "https://www.deribit.com/options/BTC",
    }


def fetch_options_factor() -> list[dict]:
    """Top-level entry used by analyzers.py — never raises, returns 0 or 1 items."""
    try:
        ctx = compute_options_context()
        factor = options_to_factor(ctx)
        return [factor] if factor else []
    except Exception as e:
        log.warning("Deribit options fetch failed: %s", e)
        return []
