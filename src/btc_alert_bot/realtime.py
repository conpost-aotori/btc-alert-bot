"""Phase 3: WebSocket-based realtime alert engine.

Replaces the 5min GitHub Actions cron with a long-running asyncio
process. The detection / analysis / posting pipeline used by ``main.py``
is reused unchanged — only the *trigger* differs:

    main.py     : GitHub cron fires every 5min, fetches everything, decides.
    realtime.py : OKX WebSocket pushes each candle close, decision happens
                  in-process within seconds of the bar closing.

Designed to run on AWS Lightsail (or any always-on host) as a
docker-compose service. See DEPLOYMENT.md.

Why we still use REST for snapshots even in WS mode:
- WS gives us *one* timely trigger (the 5m close) but features.py also
  needs the rolling kline history, OI, and funding for its z-scores.
  Re-fetching via REST keeps the code path identical to main.py and
  avoids maintaining a parallel in-memory orderbook / OI tracker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# CRITICAL: load_dotenv() MUST run before any internal imports that
# capture env vars at module load time (jp_translator.config reads
# GEMINI_API_KEY/XAI_API_KEY/etc. on first import). See main.py comment.
import websockets
from dotenv import load_dotenv

load_dotenv()

from . import event_mode  # noqa: E402
from .analyzers import gather_factors  # noqa: E402
from .chart import render_chart  # noqa: E402
from .detector import (  # noqa: E402
    GLOBAL_DEBOUNCE_MIN,
    SpikeDetector,
    append_feature_history,
    in_bear_zone,
    is_counter_trend_bounce,
    is_global_duplicate,
    load_state,
    record_alert_in_state,
    save_state,
)
from .features import compute_market_features  # noqa: E402
from .history import find_similar_alerts, record_alert  # noqa: E402
from .market import (  # noqa: E402
    fetch_market_snapshot,
    fetch_window_ohlcv,
    fetch_year_low,
)
from .milestones import (  # noqa: E402
    forced_ytd_spike,
    mark_psych_badged,
    mark_ytd_badged,
    psych_level_badge,
    ytd_commit_decision,
    ytd_low_badge,
)
from .price import fetch_btc_price  # noqa: E402
from .publishers import post_discord, post_x, x_configured  # noqa: E402
from .summarizer import summarize  # noqa: E402

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("btc_alert_bot.realtime")

# OKX has separate WS domains: /public for tickers/funding/mark-price,
# /business for candles + trades. We need candle5m → /business.
OKX_WS_URL = "wss://ws.okx.com:8443/ws/v5/business"
INST_ID = "BTC-USDT-SWAP"

# 1m fast-track: 0.6% in 60s (~$480 on $80k BTC) — second sensitivity
# bump per "もうちょっと下げよう".
FAST_TRACK_RETURN_1M_PCT = float(os.getenv("FAST_TRACK_RETURN_1M_PCT", "0.6"))

# 3m fast-track: 2.0% in 3 minutes — second sensitivity bump from 2.5%.
FAST_TRACK_RETURN_3M_PCT = float(os.getenv("FAST_TRACK_RETURN_3M_PCT", "2.0"))

STATE_PATH = Path("data/state.json")
HISTORY_DB_PATH = Path("data/history.sqlite")

# Reconnection backoff bounds.
RECONNECT_INITIAL_DELAY = 1.0
RECONNECT_MAX_DELAY = 60.0

# OKX docs say the server cuts idle connections after 30s. We send a
# plain "ping" string (NOT JSON) every PING_INTERVAL of silence.
PING_INTERVAL = 25.0

# Hard upper bound for one detection pass. If a downstream call (Gemini,
# Discord, RSS, etc.) hangs longer than this, we cancel and let the next
# candle have a fresh attempt.
DETECTION_TIMEOUT_S = 120.0

# Watchdog: if the WS loop hasn't received *any* frame (including pongs)
# for this long, we self-exit so the container's restart policy can heal
# the process. Docker-compose's healthcheck alone won't restart on
# unhealthy state with `restart: unless-stopped`.
#
# Lowered 1800 → 600s (30min → 10min) after the 2026-06-03 outage: with
# reliable container DNS (compose `dns:` block) a restart now actually
# reconnects, so faster self-exit = faster recovery. Healthy operation
# touches last_activity every ~25-60s (pings + candles), so 10min of
# total silence unambiguously means a dead connection.
WATCHDOG_STALL_S = 600.0
WATCHDOG_POLL_S = 60.0


class RealtimeBot:
    """Long-running OKX WS listener that triggers the alert pipeline."""

    def __init__(self) -> None:
        self.shutdown = asyncio.Event()
        # Single in-flight detection at a time — fire-and-forget so we
        # never block the WS receive/keepalive loop. Both pipelines do a
        # non-atomic load/mutate/save of state.json (+ history.sqlite
        # writes), so two pipelines must never actually run concurrently —
        # see detector.load_state/save_state. Kept serialized on purpose.
        self._detection_task: asyncio.Task | None = None
        # A candle that arrives while _detection_task is still running is
        # NOT dropped: it's stashed here and replayed the instant the
        # in-flight task finishes (_on_detection_done), so a genuine big
        # move can't be silently lost to the ~0.5-1s overlap window where
        # a 1m and 3m candle close at the same instant. Fast-track (a
        # real move) takes priority over a queued composite retry.
        self._pending_fast_track: tuple[str, float, float] | None = None  # (window, intra_pct, close_p)
        self._pending_composite = False
        # Watchdog liveness counter (monotonic seconds).
        self.last_activity = time.monotonic()

    # ------------------------------------------------------------------
    # Reconnect loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        log.info("%s", event_mode.describe())
        watchdog_task = asyncio.create_task(self._watchdog())
        try:
            delay = RECONNECT_INITIAL_DELAY
            while not self.shutdown.is_set():
                try:
                    async with websockets.connect(
                        OKX_WS_URL,
                        ping_interval=None,  # we manage keepalive manually
                        close_timeout=5,
                    ) as ws:
                        log.info("WS connected to %s", OKX_WS_URL)
                        delay = RECONNECT_INITIAL_DELAY  # reset on success
                        self.last_activity = time.monotonic()
                        # 1m drives BOTH fast-track AND full composite
                        # scoring now (was: composite only on 5m close).
                        # 3m kept for its own fast-track threshold.
                        # 5m subscription dropped — redundant with 1m.
                        await ws.send(json.dumps({
                            "op": "subscribe",
                            "args": [
                                {"channel": "candle3m", "instId": INST_ID},
                                {"channel": "candle1m", "instId": INST_ID},
                            ],
                        }))
                        await self._listen(ws)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.warning(
                        "WS error: %s — reconnecting in %.1fs", e, delay
                    )
                    try:
                        await asyncio.wait_for(
                            self.shutdown.wait(), timeout=delay
                        )
                        break  # shutdown flagged during sleep
                    except asyncio.TimeoutError:
                        pass
                    delay = min(delay * 2, RECONNECT_MAX_DELAY)
        finally:
            watchdog_task.cancel()
            # Allow the in-flight detection (if any) to finish before exit.
            if self._detection_task and not self._detection_task.done():
                try:
                    await asyncio.wait_for(self._detection_task, timeout=10)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
        log.info("Shutdown complete.")

    async def _watchdog(self) -> None:
        """Self-exit if the WS loop has been silent too long.

        Compose's `restart: unless-stopped` only acts on process exit, not
        on healthcheck failure. By exiting the process when stuck, we let
        the restart policy bring us back automatically.
        """
        while not self.shutdown.is_set():
            try:
                await asyncio.sleep(WATCHDOG_POLL_S)
            except asyncio.CancelledError:
                return
            elapsed = time.monotonic() - self.last_activity
            if elapsed > WATCHDOG_STALL_S:
                log.error(
                    "Watchdog: no WS activity for %.0fs (>%.0fs); exiting "
                    "so the container restart policy can recover.",
                    elapsed, WATCHDOG_STALL_S,
                )
                # exit code 2 makes intent obvious in container logs.
                os._exit(2)

    # ------------------------------------------------------------------
    # Per-connection message loop
    # ------------------------------------------------------------------

    async def _listen(self, ws) -> None:
        while not self.shutdown.is_set():
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=PING_INTERVAL)
            except asyncio.TimeoutError:
                await ws.send("ping")
                log.debug("Sent keepalive ping")
                self.last_activity = time.monotonic()  # send-side activity
                continue
            self.last_activity = time.monotonic()
            await self._handle_message(msg)

    async def _handle_message(self, msg: str) -> None:
        if msg == "pong":
            return
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            log.warning("Non-JSON WS frame: %r", msg[:120])
            return

        # Subscription ack / errors arrive with an "event" field.
        if "event" in data:
            log.info("WS event: %s", data)
            return

        arg = data.get("arg") or {}
        channel = arg.get("channel")
        if channel not in ("candle3m", "candle1m"):
            return
        rows = data.get("data") or []
        if not rows:
            return
        latest = rows[0]  # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        confirmed = len(latest) > 8 and latest[8] == "1"
        if not confirmed:
            return  # mid-bar update — wait for the close

        # Parse intra-bar move (needed for fast-track check on both 1m/3m)
        # BEFORE the in-flight check, so a busy pipeline can still tell
        # whether the skipped candle mattered enough to queue for replay.
        try:
            open_p = float(latest[1])
            close_p = float(latest[4])
        except (ValueError, TypeError):
            return
        if open_p <= 0:
            return
        intra_pct = (close_p - open_p) / open_p * 100.0

        threshold = (
            FAST_TRACK_RETURN_1M_PCT if channel == "candle1m"
            else FAST_TRACK_RETURN_3M_PCT
        )
        window = "1m" if channel == "candle1m" else "3m"
        # Event-mode (e.g. FOMC window) lowers the fast-track floor; the
        # factor is 1.0 outside any configured window → unchanged normally.
        threshold *= event_mode.threshold_factor(_now_iso())
        is_fast_track = abs(intra_pct) >= threshold

        # Single in-flight detection — same backpressure rule for all
        # channels so we never have two pipelines running at once (see
        # __init__). Queue this candle for replay instead of dropping it.
        if self._detection_task and not self._detection_task.done():
            if is_fast_track:
                self._pending_fast_track = (window, intra_pct, close_p)
                log.warning(
                    "Previous detection still running; %s fast-track "
                    "(%+.3f%%) queued for replay",
                    window, intra_pct,
                )
            elif channel == "candle1m":
                self._pending_composite = True
                log.warning(
                    "Previous detection still running; composite check "
                    "queued for replay"
                )
            else:
                log.warning(
                    "Previous detection still running; skipping this %s "
                    "candle (not fast-track)",
                    channel,
                )
            return

        # Fast-track wins if the intra-bar move clearly crossed its floor.
        if is_fast_track:
            log.info(
                "%s FAST-TRACK fired: open=%.2f close=%.2f intra=%+.3f%%",
                window, open_p, close_p, intra_pct,
            )
            self._launch_fast_track(intra_pct, close_p, window)
            return

        # Otherwise: on 1m close only, run the full composite scoring path
        # (was: every 5min via candle5m). 3m closes do nothing extra so
        # we don't double-count the 1m that fired in the same second.
        if channel != "candle1m":
            return
        log.info(
            "1m candle closed (composite): ts=%s close=%s intra=%+.3f%%",
            latest[0], latest[4], intra_pct,
        )
        self._launch_composite()

    # ------------------------------------------------------------------
    # Task launch + replay bookkeeping
    # ------------------------------------------------------------------

    def _launch_fast_track(self, intra_pct: float, close_p: float, window: str) -> None:
        self._pending_fast_track = None
        task = asyncio.create_task(self._run_fast_track_async(intra_pct, close_p, window))
        task.add_done_callback(self._on_detection_done)
        self._detection_task = task

    def _launch_composite(self) -> None:
        self._pending_composite = False
        task = asyncio.create_task(self._run_detection_async())
        task.add_done_callback(self._on_detection_done)
        self._detection_task = task

    def _on_detection_done(self, task: asyncio.Task) -> None:
        """Replay a candle that arrived while ``task`` was still running.

        Only acts if ``task`` is still the current ``_detection_task`` — if
        a fresh (non-queued) candle already launched a newer task in the
        meantime, that task's own completion will handle any replay, so
        this stale callback is a no-op.
        """
        if task is not self._detection_task or self.shutdown.is_set():
            return
        if self._pending_fast_track is not None:
            window, intra_pct, close_p = self._pending_fast_track
            log.info("Replaying queued %s fast-track (%+.3f%%)", window, intra_pct)
            self._launch_fast_track(intra_pct, close_p, window)
        elif self._pending_composite:
            log.info("Replaying queued composite check")
            self._launch_composite()

    # ------------------------------------------------------------------
    # Detection pipeline (mirrors main.py, sync)
    # ------------------------------------------------------------------

    async def _run_detection_async(self) -> None:
        """Run the sync pipeline in a thread, bounded by DETECTION_TIMEOUT_S."""
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._run_detection),
                timeout=DETECTION_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.error(
                "Detection exceeded %.0fs deadline; will retry on next candle",
                DETECTION_TIMEOUT_S,
            )
        except Exception:
            log.exception("Detection task crashed unexpectedly")

    async def _run_fast_track_async(
        self, intra_pct: float, close_p: float, window: str = "1m"
    ) -> None:
        """Async wrapper around the fast-track pipeline (1m or 3m)."""
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._run_fast_track, intra_pct, close_p, window),
                timeout=DETECTION_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.error(
                "Fast-track exceeded %.0fs deadline; will rely on 5m close",
                DETECTION_TIMEOUT_S,
            )
        except Exception:
            log.exception("Fast-track task crashed unexpectedly")

    def _run_fast_track(
        self, intra_pct: float, close_p: float, window: str = "1m"
    ) -> None:
        """Fast-track alert pipeline for 1m or 3m closes.

        Bypasses composite scoring — by definition the move was already
        large enough to be alert-worthy. Still respects the tier-aware
        cooldown so we don't spam if several consecutive bars are big.
        """
        try:
            direction = "up" if intra_pct >= 0 else "down"
            state = load_state(STATE_PATH)
            detector = SpikeDetector(state)

            # Tier-aware cooldown — same-direction same-tier suppression
            # plus cross-tier (medium suppressed by short, etc.) handled
            # inside _is_suppressed.
            if detector._is_suppressed(  # noqa: SLF001
                window, direction, _now_iso()
            ):
                log.info("%s fast-track suppressed by cooldown/tier", window)
                return

            threshold = (
                FAST_TRACK_RETURN_1M_PCT
                if window == "1m"
                else FAST_TRACK_RETURN_3M_PCT
            ) * event_mode.threshold_factor(_now_iso())
            spike = {
                "window": window,
                "change": intra_pct,
                "direction": direction,
                "score": None,  # composite scoring intentionally bypassed
                "reasons": [
                    f"{window} intra-bar move {intra_pct:+.3f}% (fast-track)",
                    f"close: ${close_p:,.2f}",
                    f"threshold: ±{threshold:.2f}%",
                ],
                "features": None,
            }
            log.info(
                "FAST-TRACK SPIKE: %s %+.3f%% (%s)", window, intra_pct, direction
            )

            price_data = fetch_btc_price()
            log.info(
                "BTC $%.2f | 1h %+.2f%% | 24h %+.2f%%",
                price_data["price_usd"],
                price_data["change_1h"],
                price_data["change_24h"],
            )

            # Counter-trend bounce filter: a small move opposite to an
            # established trend (e.g. a +0.7% 1m bounce mid-crash) would post
            # a "暴騰" title over a crash-context summary. Drop it here, before
            # the expensive factor / summary work.
            bz = in_bear_zone(state, price_data.get("price_usd", 0.0))
            if is_counter_trend_bounce(
                direction, intra_pct,
                price_data.get("change_1h", 0.0),
                price_data.get("change_24h", 0.0),
                override_mult=event_mode.threshold_factor(_now_iso()),
                bear_zone=bz,
            ):
                log.info(
                    "Fast-track %s %+.3f%% suppressed: counter-trend bounce "
                    "(1h %+.2f%%, 24h %+.2f%%%s)",
                    window, intra_pct,
                    price_data.get("change_1h", 0.0),
                    price_data.get("change_24h", 0.0),
                    ", bear-zone" if bz else "",
                )
                return

            # Global near-duplicate debounce: don't fire a same-direction
            # fast-track right after another recent alert (any window/tier).
            if is_global_duplicate(
                state, direction, intra_pct,
                price_data.get("timestamp") or _now_iso(),
            ):
                log.info(
                    "Fast-track %s %+.3f%% (%s) suppressed: global near-"
                    "duplicate (same-dir alert <%dmin ago)",
                    window, intra_pct, direction, GLOBAL_DEBOUNCE_MIN,
                )
                return

            factors = gather_factors(spike)
            similar = find_similar_alerts(HISTORY_DB_PATH, spike, limit=3)
            summary = summarize(price_data, spike, factors, similar_alerts=similar)

            # Milestone badges (YTD-low + psych-level break). Prepended to the
            # summary; committed only after a successful post (retry pattern).
            ytd_badge = ytd_low_badge(
                state, price_data["price_usd"], seed_year_low=fetch_year_low
            )
            psych_badge = psych_level_badge(state, price_data["price_usd"])
            if ytd_badge:
                summary = f"{ytd_badge}\n{summary}"
                log.info("YTD-low badge prepended: %s", ytd_badge)
            if psych_badge:
                summary = f"{psych_badge}\n{summary}"
                log.info("Psych-level badge prepended: %s", psych_badge)

            chart_png: bytes | None = None
            try:
                chart_png = render_chart(spike, price_data)
            except Exception as e:
                log.warning("Chart render failed: %s", e)

            # Fast-track has no `features` — anchor on the alert ts itself,
            # which falls inside the just-closed trigger bar.
            window_ohlcv = fetch_window_ohlcv(
                spike["window"], anchor_ts=price_data.get("timestamp")
            )

            dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
            enable_x = os.getenv("ENABLE_X_POST", "false").lower() == "true"
            # The 年初来最安値 (YTD-low) emergency bulletin reaches X even when
            # routine X posting is off, so the once-only crash bulletin gets
            # out. Needs X API keys; opt-in via ENABLE_X_YTD_LOW. Scoped to the
            # YTD-low badge ONLY — psych-level ($60k) breaks are NOT auto-
            # tweeted (they recur far more often, with no one-shot cap).
            # Routine spikes still obey ENABLE_X_POST.
            force_x = (
                os.getenv("ENABLE_X_YTD_LOW", "false").lower() == "true"
                and bool(ytd_badge)
            )
            if force_x and not x_configured():
                log.warning(
                    "ENABLE_X_YTD_LOW is set but X API keys are missing — "
                    "the YTD-low emergency will reach Discord only"
                )
            d_disc = d_x = False
            if dry_run:
                log.info("[DRY_RUN] Skipping actual posts")
                d_disc = True
            else:
                d_disc = post_discord(
                    summary, price_data, spike,
                    chart_png=chart_png, window_ohlcv=window_ohlcv,
                )
                if enable_x or force_x:
                    d_x = post_x(
                        summary, price_data, spike, chart_png=chart_png
                    )

            try:
                record_alert(
                    HISTORY_DB_PATH,
                    price_data=price_data,
                    spike=spike,
                    factors=factors,
                    summary=summary,
                    delivered_discord=d_disc,
                    delivered_x=d_x,
                )
            except Exception:
                # A history-DB failure (e.g. locked sqlite) must not abort
                # the milestone commit below — that would re-post an already
                # delivered YTD/psych bulletin on the next candle.
                log.exception("record_alert failed — continuing")

            if d_disc or d_x:
                record_alert_in_state(state, spike, price_data)
                # Commit milestone badges ONLY after a successful REAL post, so
                # a delivery failure leaves them pending and retries next candle
                # (絶対投稿 — the YTD-low break always gets out). Skipped under
                # DRY_RUN so a dry validation pass can't burn the YTD_ONESHOT
                # latch (which would suppress the next real break).
                if not dry_run:
                    # YTD badge: when the X emergency is armed AND X keys
                    # exist, X delivery is required to commit — a Discord-
                    # only success keeps the badge pending (retries next
                    # candle) so the one-shot isn't burned without a tweet.
                    if ytd_badge and ytd_commit_decision(
                        state, force_x, x_configured(), d_disc, d_x
                    ):
                        mark_ytd_badged(state, price_data["price_usd"])
                    if psych_badge:
                        mark_psych_badged(state, price_data["price_usd"])
            save_state(STATE_PATH, state)
        except Exception:
            log.exception("Fast-track pipeline failed")

    def _run_detection(self) -> None:
        try:
            price_data = fetch_btc_price()
            log.info(
                "BTC $%.2f | 1h %+.2f%% | 24h %+.2f%%",
                price_data["price_usd"],
                price_data["change_1h"],
                price_data["change_24h"],
            )

            state = load_state(STATE_PATH)

            features: dict = {}
            try:
                snapshot = fetch_market_snapshot()
                features = compute_market_features(snapshot, state=state)
            except Exception as e:
                log.warning(
                    "OKX REST fetch failed: %s — falling back to legacy detector", e
                )

            append_feature_history(state, features)

            detector = SpikeDetector(state)
            spike = (
                detector.check_composite(price_data, features)
                if features else
                detector.check_legacy(price_data)
            )

            # Counter-trend bounce filter: drop a small move opposite to an
            # established trend (its summary would contradict the title).
            # Uses the detector's own 1h return plus the 24h regime.
            bz = in_bear_zone(state, price_data.get("price_usd", 0.0))
            if spike is not None and is_counter_trend_bounce(
                spike["direction"], spike["change"],
                (features or {}).get("return_1h", price_data.get("change_1h", 0.0)),
                price_data.get("change_24h", 0.0),
                override_mult=event_mode.threshold_factor(
                    price_data.get("timestamp")
                ),
                bear_zone=bz,
            ):
                log.info(
                    "Composite %s %+.2f%% (%s) suppressed: counter-trend "
                    "bounce (1h %+.2f%%, 24h %+.2f%%%s)",
                    spike["window"], spike["change"], spike["direction"],
                    (features or {}).get("return_1h", price_data.get("change_1h", 0.0)),
                    price_data.get("change_24h", 0.0),
                    ", bear-zone" if bz else "",
                )
                spike = None

            # Year-to-date-low milestone, evaluated BEFORE the suppression
            # gate so it can override it. The first time BTC breaks its YTD
            # low, fire EVEN IF normal detection was cooldown-suppressed (or
            # crossed no window threshold) — but only once (per user
            # "クールダウン中でも発火、一度きり"). ytd_low_badge() consumes the
            # once-flag and returns the badge text only on that first break.
            ytd_badge = ytd_low_badge(
                state, price_data["price_usd"], seed_year_low=fetch_year_low
            )
            # Psychological round-number break ($60k etc.) — same special-
            # exception treatment as the YTD low (force-fire, bypass cooldown
            # + debounce, retry until delivered).
            psych_badge = psych_level_badge(state, price_data["price_usd"])
            milestone = bool(ytd_badge or psych_badge)

            if milestone and spike is None:
                spike = forced_ytd_spike(features, price_data)
                log.info(
                    "Milestone override: forcing alert through suppression "
                    "(%s %+.2f%%) ytd=%s psych=%s",
                    spike["window"], spike["change"],
                    bool(ytd_badge), bool(psych_badge),
                )

            # Global near-duplicate debounce: drop a same-direction alert
            # that lands within minutes of the previous one (any window/tier)
            # — e.g. 1h firing right after 2h for the same crash. Milestone
            # fires (YTD-low / psych-level) bypass this (絶対投稿).
            if (
                spike is not None and not milestone
                and is_global_duplicate(
                    state, spike["direction"], spike["change"],
                    price_data.get("timestamp") or _now_iso(),
                )
            ):
                log.info(
                    "Composite %s %+.2f%% (%s) suppressed: global near-"
                    "duplicate (same-dir alert <%dmin ago)",
                    spike["window"], spike["change"], spike["direction"],
                    GLOBAL_DEBOUNCE_MIN,
                )
                spike = None

            if spike is None:
                save_state(STATE_PATH, state)
                return

            log.info(
                "SPIKE: %s %+.2f%% (%s) score=%s",
                spike["window"], spike["change"], spike["direction"],
                spike.get("score"),
            )

            factors = gather_factors(spike)
            similar = find_similar_alerts(HISTORY_DB_PATH, spike, limit=3)
            summary = summarize(price_data, spike, factors, similar_alerts=similar)

            # Prepend milestone badges (most prominent first). Psych-level
            # break above the YTD-low line when both fire on the same candle.
            if ytd_badge:
                summary = f"{ytd_badge}\n{summary}"
                log.info("YTD-low badge prepended: %s", ytd_badge)
            if psych_badge:
                summary = f"{psych_badge}\n{summary}"
                log.info("Psych-level badge prepended: %s", psych_badge)

            chart_png: bytes | None = None
            try:
                chart_png = render_chart(spike, price_data)
            except Exception as e:
                log.warning("Chart render failed: %s", e)

            anchor_ts = (features or {}).get("ts") or price_data.get("timestamp")
            window_ohlcv = fetch_window_ohlcv(
                spike["window"], anchor_ts=anchor_ts
            )

            dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
            enable_x = os.getenv("ENABLE_X_POST", "false").lower() == "true"
            # The 年初来最安値 (YTD-low) emergency bulletin reaches X even when
            # routine X posting is off (opt-in ENABLE_X_YTD_LOW; needs X API
            # keys). Scoped to the YTD-low badge ONLY — psych-level breaks are
            # not auto-tweeted. Routine spikes still obey ENABLE_X_POST.
            force_x = (
                os.getenv("ENABLE_X_YTD_LOW", "false").lower() == "true"
                and bool(ytd_badge)
            )
            if force_x and not x_configured():
                log.warning(
                    "ENABLE_X_YTD_LOW is set but X API keys are missing — "
                    "the YTD-low emergency will reach Discord only"
                )
            d_disc = d_x = False
            if dry_run:
                log.info("[DRY_RUN] Skipping actual posts")
                d_disc = True
            else:
                d_disc = post_discord(
                    summary, price_data, spike,
                    chart_png=chart_png, window_ohlcv=window_ohlcv,
                )
                if enable_x or force_x:
                    d_x = post_x(
                        summary, price_data, spike, chart_png=chart_png
                    )

            try:
                record_alert(
                    HISTORY_DB_PATH,
                    price_data=price_data,
                    spike=spike,
                    factors=factors,
                    summary=summary,
                    delivered_discord=d_disc,
                    delivered_x=d_x,
                )
            except Exception:
                # A history-DB failure (e.g. locked sqlite) must not abort
                # the milestone commit below — that would re-post an already
                # delivered YTD/psych bulletin on the next candle.
                log.exception("record_alert failed — continuing")

            if d_disc or d_x:
                record_alert_in_state(state, spike, price_data)
                # Commit milestone badges ONLY after a successful REAL post, so
                # a delivery failure leaves them pending and retries next candle
                # (絶対投稿 — the YTD-low break always gets out). Skipped under
                # DRY_RUN so a dry validation pass can't burn the YTD_ONESHOT
                # latch (which would suppress the next real break).
                if not dry_run:
                    # YTD badge: when the X emergency is armed AND X keys
                    # exist, X delivery is required to commit — a Discord-
                    # only success keeps the badge pending (retries next
                    # candle) so the one-shot isn't burned without a tweet.
                    if ytd_badge and ytd_commit_decision(
                        state, force_x, x_configured(), d_disc, d_x
                    ):
                        mark_ytd_badged(state, price_data["price_usd"])
                    if psych_badge:
                        mark_psych_badged(state, price_data["price_usd"])
            save_state(STATE_PATH, state)
        except Exception as e:
            # Log but never crash the WS loop — the next candle close gets a fresh try.
            log.exception("Detection pipeline failed: %s", e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main() -> None:
    bot = RealtimeBot()
    loop = asyncio.get_running_loop()
    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, bot.shutdown.set)
        except NotImplementedError:
            # Windows: signal handlers in asyncio aren't supported; the
            # process will still exit on Ctrl-C via KeyboardInterrupt.
            pass
    log.info("Realtime bot starting...")
    await bot.run()


def main() -> int:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
