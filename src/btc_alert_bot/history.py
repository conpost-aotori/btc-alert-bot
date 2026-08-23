"""SQLite-backed alert history for Phase 2.

Every fired alert (spike + factors + summary + per-channel delivery status)
is persisted to ``data/history.sqlite``. The DB file is committed by the
GitHub Actions workflow so history accumulates across runner instances.

Design notes:
- Writes happen *only* on alert fire — keeps commit cadence low.
- Schema is intentionally narrow; we store features as a JSON blob rather
  than columnizing, so future ML/analytics work doesn't require migrations.
- foreign_keys=ON enforces referential integrity for cascade-delete.
- A small CLI at the bottom of this file lets you browse the DB:
      python -m btc_alert_bot.history list
      python -m btc_alert_bot.history show <id>

Phase 2.5 (done): similarity search (cosine on the features_json vector).
Phase 2.6 (2026-08-23): **outcome labels** — ``alert_outcomes`` records, for
every delivered alert, the OKX price at +12h / +24h and whether the alert's
direction call continued / reversed / went flat. Joined with
``alerts.features_json`` this is the self-learning dataset for threshold
and zone tuning. See outcome_review.py (scheduler) and the ``outcomes`` /
``export`` CLI subcommands below.

Future work:
- Threshold tuning by replaying past spikes against new score weights
- Counterfactual logging of SUPPRESSED candidates (bear/rally/cooldown) so
  the zones can be graded too, not just the alerts that fired
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# Outcome horizons graded for every delivered alert (hours). Lives here
# (not in outcome_review.py) so the CSV export always emits a fixed column
# set — a learning dataset must have a stable schema regardless of how
# many horizons have been graded so far.
DEFAULT_HORIZONS_H: tuple[int, ...] = (12, 24)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    price_usd REAL NOT NULL,
    spike_window TEXT NOT NULL,
    spike_change_pct REAL NOT NULL,
    spike_direction TEXT NOT NULL,
    spike_score REAL,
    reasons_json TEXT NOT NULL,
    features_json TEXT,
    summary TEXT,
    delivered_discord INTEGER NOT NULL DEFAULT 0,
    delivered_x INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
CREATE INDEX IF NOT EXISTS idx_alerts_dir_win ON alerts(spike_direction, spike_window);

CREATE TABLE IF NOT EXISTS factors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT,
    url TEXT,
    published TEXT,
    FOREIGN KEY (alert_id) REFERENCES alerts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_factors_alert ON factors(alert_id);

-- Phase 2.6: outcome labels. One row per (alert, horizon). price_at_alert
-- is re-priced on OKX at the alert ts so both ends use the same instrument
-- (alerts.price_usd is the CoinGecko fire price, kept for reference).
-- label: continued (move kept going = direction call correct),
--        reversed  (went the other way), flat (within the neutral band).
-- posted: 1 if the Discord review note went out; 0 for silent backfill.
CREATE TABLE IF NOT EXISTS alert_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    horizon_h INTEGER NOT NULL,
    due_ts TEXT NOT NULL,
    eval_ts TEXT NOT NULL,
    price_at_alert REAL,
    price_at_eval REAL NOT NULL,
    change_pct REAL NOT NULL,
    label TEXT NOT NULL,
    posted INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (alert_id) REFERENCES alerts(id) ON DELETE CASCADE,
    UNIQUE (alert_id, horizon_h)
);

CREATE INDEX IF NOT EXISTS idx_outcomes_alert ON alert_outcomes(alert_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_due ON alert_outcomes(due_ts);
"""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(path: Path) -> None:
    """Create the schema if missing and stamp ``schema_version``."""
    with _connect(path) as conn:
        conn.executescript(SCHEMA_SQL)
        cur = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if not cur or cur[0] != str(SCHEMA_VERSION):
            # Only write when it actually changes — this runs on every
            # record_alert / reviewer tick and each write takes the lock.
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            conn.commit()


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def record_alert(
    path: Path,
    *,
    price_data: dict,
    spike: dict,
    factors: list[dict],
    summary: str,
    delivered_discord: bool,
    delivered_x: bool,
) -> int | None:
    """Insert one alert row + its factors. Returns ``alert.id`` or None on error.

    All fields are validated minimally — failures are logged and swallowed
    so a DB issue never blocks the live alert path.
    """
    try:
        init_db(path)
        with _connect(path) as conn:
            cur = conn.execute(
                """
                INSERT INTO alerts (
                    ts, price_usd, spike_window, spike_change_pct,
                    spike_direction, spike_score, reasons_json, features_json,
                    summary, delivered_discord, delivered_x
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    price_data.get("timestamp")
                    or datetime.now(timezone.utc).isoformat(),
                    float(price_data["price_usd"]),
                    spike["window"],
                    float(spike["change"]),
                    spike["direction"],
                    spike.get("score"),
                    json.dumps(spike.get("reasons") or [], ensure_ascii=False),
                    json.dumps(spike.get("features") or {}, ensure_ascii=False),
                    summary,
                    1 if delivered_discord else 0,
                    1 if delivered_x else 0,
                ),
            )
            alert_id = cur.lastrowid
            for f in factors:
                conn.execute(
                    """
                    INSERT INTO factors (alert_id, type, source, title, url, published)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        alert_id,
                        f.get("type", ""),
                        f.get("source", ""),
                        f.get("title", ""),
                        f.get("url", ""),
                        f.get("published"),
                    ),
                )
            conn.commit()
            log.info(
                "Recorded alert id=%s with %d factors", alert_id, len(factors)
            )
            return alert_id
    except Exception as e:
        log.warning("Failed to record alert in history DB: %s", e)
        return None


# ---------------------------------------------------------------------------
# Reads (for CLI / future similarity search)
# ---------------------------------------------------------------------------

def list_recent_alerts(path: Path, limit: int = 20) -> list[dict]:
    """Return recent alerts (newest first) as plain dicts."""
    if not path.exists():
        return []
    try:
        with _connect(path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        log.warning("Failed to list alerts: %s", e)
        return []


# ---------------------------------------------------------------------------
# Outcome labels (Phase 2.6)
# ---------------------------------------------------------------------------

def pending_outcome_checks(
    path: Path,
    *,
    now: datetime,
    horizons_h: tuple[int, ...],
    limit: int | None = None,
) -> list[dict]:
    """Delivered alerts whose +horizon is due but not yet evaluated.

    Returns ALL due (alert, horizon) pairs by default, OLDEST DUE FIRST
    (``limit`` truncates if given). The reviewer deliberately fetches the
    whole due set and prioritises in-grace pairs itself — truncating here
    (oldest-first) would hide a fresh, timely pair behind a large backlog.
    Only alerts that actually reached a channel count — an undelivered
    row never made a user-facing "call" to grade.
    A pair becomes due one minute AFTER ``ts + horizon`` so the 1m bar
    that contains the due instant has closed (otherwise the eval price
    would be read off a still-open candle).
    """
    if not path.exists():
        return []
    from datetime import timedelta
    try:
        init_db(path)
        with _connect(path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, ts, price_usd, spike_window, spike_change_pct,
                       spike_direction, spike_score
                FROM alerts
                WHERE (delivered_discord = 1 OR delivered_x = 1)
                ORDER BY ts ASC
                """
            ).fetchall()
            done = {
                (r["alert_id"], r["horizon_h"])
                for r in conn.execute(
                    "SELECT alert_id, horizon_h FROM alert_outcomes"
                ).fetchall()
            }
    except Exception as e:
        log.warning("pending_outcome_checks failed: %s", e)
        return []
    out: list[dict] = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["ts"])
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        for h in horizons_h:
            if (r["id"], h) in done:
                continue
            due = ts + timedelta(hours=h)
            if due + timedelta(minutes=1) <= now:
                out.append({
                    "alert_id": r["id"],
                    "ts": ts,
                    "price_usd": r["price_usd"],
                    "window": r["spike_window"],
                    "change_pct": r["spike_change_pct"],
                    "direction": r["spike_direction"],
                    "score": r["spike_score"],
                    "horizon_h": h,
                    "due": due,
                })
    out.sort(key=lambda d: d["due"])
    return out[:limit] if limit else out


def record_outcome(
    path: Path,
    *,
    alert_id: int,
    horizon_h: int,
    due_ts: datetime,
    eval_ts: datetime,
    price_at_alert: float | None,
    price_at_eval: float,
    change_pct: float,
    label: str,
    posted: bool,
) -> bool:
    """Insert one outcome row. Idempotent via UNIQUE(alert_id, horizon_h).

    Returns True ONLY when a new row was actually inserted — the caller
    uses this as the gate for posting the Discord note, so a duplicate
    (already-recorded) pair can never produce a second note.
    """
    try:
        with _connect(path) as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO alert_outcomes (
                    alert_id, horizon_h, due_ts, eval_ts, price_at_alert,
                    price_at_eval, change_pct, label, posted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id, horizon_h, due_ts.isoformat(), eval_ts.isoformat(),
                    price_at_alert, price_at_eval, change_pct, label,
                    1 if posted else 0,
                ),
            )
            conn.commit()
            return cur.rowcount == 1
    except Exception as e:
        log.warning("record_outcome failed (alert %s, +%dh): %s", alert_id, horizon_h, e)
        return False


def mark_outcome_posted(path: Path, *, alert_id: int, horizon_h: int) -> None:
    """Flip ``posted`` to 1 after the Discord note went out."""
    try:
        with _connect(path) as conn:
            conn.execute(
                "UPDATE alert_outcomes SET posted = 1 WHERE alert_id = ? AND horizon_h = ?",
                (alert_id, horizon_h),
            )
            conn.commit()
    except Exception as e:
        log.warning("mark_outcome_posted failed (alert %s, +%dh): %s", alert_id, horizon_h, e)


def outcome_stats(
    path: Path, *, days: int = 30, now: datetime | None = None
) -> dict:
    """Accuracy summary over the last ``days``.

    Returns ``{horizon_h: {"n", "continued", "reversed", "flat",
    "by_window": {window: {"n", "continued"}}}}``. "continued" means the
    direction call was right. Empty dict on error / no data.
    """
    if not path.exists():
        return {}
    from datetime import timedelta
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    stats: dict = {}
    try:
        with _connect(path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT o.horizon_h, o.label, a.spike_window AS window
                FROM alert_outcomes o JOIN alerts a ON a.id = o.alert_id
                WHERE a.ts >= ?
                """,
                (since,),
            ).fetchall()
    except Exception as e:
        log.warning("outcome_stats failed: %s", e)
        return {}
    for r in rows:
        h = stats.setdefault(
            int(r["horizon_h"]),
            {"n": 0, "continued": 0, "reversed": 0, "flat": 0, "by_window": {}},
        )
        h["n"] += 1
        if r["label"] in ("continued", "reversed", "flat"):
            h[r["label"]] += 1
        w = h["by_window"].setdefault(r["window"], {"n": 0, "continued": 0})
        w["n"] += 1
        if r["label"] == "continued":
            w["continued"] += 1
    return stats


def export_outcomes_csv(path: Path, out_path: Path) -> int:
    """Write the self-learning dataset: one row per delivered alert with
    features flattened and +12h/+24h outcome columns. Returns row count."""
    import csv
    if not path.exists():
        return 0
    init_db(path)
    with _connect(path) as conn:
        conn.row_factory = sqlite3.Row
        alerts = conn.execute(
            "SELECT * FROM alerts WHERE delivered_discord = 1 OR delivered_x = 1 "
            "ORDER BY ts ASC"
        ).fetchall()
        outs = conn.execute("SELECT * FROM alert_outcomes").fetchall()
    by_alert: dict[int, dict[int, sqlite3.Row]] = {}
    for o in outs:
        by_alert.setdefault(int(o["alert_id"]), {})[int(o["horizon_h"])] = o
    feature_keys: list[str] = []
    parsed: list[tuple[sqlite3.Row, dict]] = []
    for a in alerts:
        try:
            f = json.loads(a["features_json"] or "{}") or {}
        except Exception:
            f = {}
        for k in f:
            if k not in feature_keys and k != "ts":
                feature_keys.append(k)
        parsed.append((a, f))
    horizons = sorted(set(DEFAULT_HORIZONS_H) | {int(o["horizon_h"]) for o in outs})
    cols = [
        "alert_id", "ts", "window", "direction", "change_pct", "score",
        "price_usd",
    ] + [f"f_{k}" for k in feature_keys]
    for h in horizons:
        cols += [f"h{h}_price", f"h{h}_change_pct", f"h{h}_label"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for a, f in parsed:
            row = [
                a["id"], a["ts"], a["spike_window"], a["spike_direction"],
                a["spike_change_pct"], a["spike_score"], a["price_usd"],
            ] + [f.get(k, "") for k in feature_keys]
            oc = by_alert.get(int(a["id"]), {})
            for h in horizons:
                o = oc.get(h)
                row += (
                    [o["price_at_eval"], o["change_pct"], o["label"]]
                    if o else ["", "", ""]
                )
            w.writerow(row)
            n += 1
    return n


# ---------------------------------------------------------------------------
# Similarity search (Phase 2.5)
# ---------------------------------------------------------------------------

# Components of the similarity vector and the divisor each is normalized
# by. The divisors are rough "typical max" values so distance comparisons
# stay scale-invariant across components.
#
# Microstructure (vol regime, OI flow, funding) + magnitude (return_5m,
# return_15m, volume) together describe both "what kind of move" and "how
# big a move". Direction is filtered separately in the WHERE clause, so
# return signs always match — magnitude carries directly.
_SIMILARITY_VECTOR = [
    # --- microstructure: what regime are we in? ---
    ("atr_pct",          0.5),    # vol regime
    ("move_per_atr",     4.0),    # how unusual is this move vs ATR
    ("oi_change_1h_pct", 5.0),    # is OI flowing in/out
    ("funding_rate",     0.001),  # crowd positioning
    # --- magnitude: how big a move, on what timeframe? ---
    ("return_5m",        2.0),    # short-window move magnitude
    ("return_15m",       3.0),    # medium-window move magnitude
    ("volume_5bar",      1e6),    # rough volume scale (BTC contracts)
]

# How many past alerts (per direction) to consider before ranking.
SIMILARITY_CANDIDATE_LIMIT = 200

# Distance thresholds for the human-readable label attached to each
# similar alert. Tuned against the small-N history we have today — will
# need re-tuning once the DB has 1000+ alerts to bucket against.
_DIST_VERY_SIMILAR = 1.5
_DIST_SIMILAR = 3.0


def _vectorize(features: dict | None) -> list[float] | None:
    """Build a normalized feature vector. Returns None if too thin."""
    if not features:
        return None
    vec: list[float] = []
    for key, divisor in _SIMILARITY_VECTOR:
        v = features.get(key)
        try:
            vec.append(float(v) / divisor if v is not None else 0.0)
        except (TypeError, ValueError):
            vec.append(0.0)
    return vec


def find_similar_alerts(
    path: Path,
    current_spike: dict,
    limit: int = 3,
) -> list[dict]:
    """Find past alerts most similar to the current one (same direction).

    Ranks candidates by Euclidean distance on a small normalized feature
    vector. Returns up to ``limit`` alerts with their factors attached.
    Designed to be a Gemini prompt enrichment, not a strict classifier —
    so we never raise; missing data just yields fewer results.
    """
    if not path.exists():
        return []

    cur_features = current_spike.get("features") or {}
    cur_vec = _vectorize(cur_features)
    if cur_vec is None:
        return []

    direction = current_spike.get("direction")
    try:
        with _connect(path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM alerts
                WHERE spike_direction = ?
                ORDER BY ts DESC
                LIMIT ?
                """,
                (direction, SIMILARITY_CANDIDATE_LIMIT),
            ).fetchall()
            candidates: list[tuple[float, dict]] = []
            for r in rows:
                try:
                    past_features = json.loads(r["features_json"] or "{}")
                except Exception:
                    continue
                past_vec = _vectorize(past_features)
                if past_vec is None:
                    continue
                dist = sum(
                    (a - b) ** 2 for a, b in zip(cur_vec, past_vec)
                ) ** 0.5
                candidates.append((dist, dict(r)))

            candidates.sort(key=lambda x: x[0])
            out: list[dict] = []
            for dist, alert in candidates[:limit]:
                factors = conn.execute(
                    """
                    SELECT type, source, title FROM factors
                    WHERE alert_id = ? ORDER BY id
                    """,
                    (alert["id"],),
                ).fetchall()
                # Human label for the prompt so the model can weight the
                # match qualitatively without parsing the raw distance.
                if dist <= _DIST_VERY_SIMILAR:
                    similarity_label = "極めて類似"
                elif dist <= _DIST_SIMILAR:
                    similarity_label = "類似"
                else:
                    similarity_label = "やや類似"
                out.append({
                    "id": alert["id"],
                    "ts": alert["ts"],
                    "direction": alert["spike_direction"],
                    "change_pct": alert["spike_change_pct"],
                    "window": alert["spike_window"],
                    "score": alert.get("spike_score"),
                    "summary": alert.get("summary"),
                    "distance": dist,
                    "similarity_label": similarity_label,
                    "factors": [dict(f) for f in factors],
                })
            return out
    except Exception as e:
        log.warning("Similarity search failed: %s", e)
        return []


def get_alert(path: Path, alert_id: int) -> dict | None:
    """Return one alert with its factors attached as ``factors`` list."""
    if not path.exists():
        return None
    try:
        with _connect(path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM alerts WHERE id = ?", (alert_id,)
            ).fetchone()
            if not row:
                return None
            alert = dict(row)
            factors = conn.execute(
                "SELECT * FROM factors WHERE alert_id = ? ORDER BY id",
                (alert_id,),
            ).fetchall()
            alert["factors"] = [dict(f) for f in factors]
            return alert
    except Exception as e:
        log.warning("Failed to load alert %s: %s", alert_id, e)
        return None


# ---------------------------------------------------------------------------
# CLI: python -m btc_alert_bot.history {list|show <id>|outcomes|export|init}
# ---------------------------------------------------------------------------

def _cli() -> int:
    import argparse
    import io
    import sys

    # Some stdouts (notably Windows cp932) can't encode emoji in summaries.
    # Rewrap with UTF-8 + replace so the CLI never crashes on display chars.
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace"
            )
        except Exception:
            pass

    parser = argparse.ArgumentParser(prog="btc_alert_bot.history")
    parser.add_argument(
        "--db", default="data/history.sqlite", help="Path to SQLite file"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Create schema if missing")

    p_list = sub.add_parser("list", help="Show recent alerts")
    p_list.add_argument("--limit", type=int, default=20)

    p_show = sub.add_parser("show", help="Show one alert with its factors")
    p_show.add_argument("id", type=int)

    p_out = sub.add_parser("outcomes", help="Direction-call accuracy (self-learning labels)")
    p_out.add_argument("--days", type=int, default=30)

    p_exp = sub.add_parser("export", help="Write the labeled dataset as CSV")
    p_exp.add_argument("--out", default="data/outcomes_export.csv")

    args = parser.parse_args()
    db_path = Path(args.db)

    if args.cmd == "init":
        init_db(db_path)
        print(f"Initialized {db_path}")
        return 0

    if args.cmd == "list":
        rows = list_recent_alerts(db_path, args.limit)
        if not rows:
            print("(no alerts yet)")
            return 0
        print(f"{len(rows)} alert(s):")
        for r in rows:
            score = r.get("spike_score")
            score_s = f"score={score}" if score is not None else "legacy"
            print(
                f"  #{r['id']:<4} {r['ts']}  "
                f"{r['spike_direction']:<4} {r['spike_change_pct']:+.2f}% "
                f"/ {r['spike_window']:<4}  {score_s}"
            )
            if r.get("summary"):
                first_line = r["summary"].split("\n", 1)[0][:120]
                print(f"        {first_line}")
        return 0

    if args.cmd == "outcomes":
        stats = outcome_stats(db_path, days=args.days)
        if not stats:
            print("(no outcome labels yet)")
            return 0
        print(f"Direction-call accuracy, last {args.days} days:")
        for h in sorted(stats):
            st = stats[h]
            n = st["n"] or 1
            print(
                f"  +{h:>2}h  n={st['n']:<3} 正解(継続) {st['continued']/n*100:5.1f}%  "
                f"反転 {st['reversed']/n*100:5.1f}%  横ばい {st['flat']/n*100:5.1f}%"
            )
            for w, ws in sorted(st["by_window"].items()):
                wn = ws["n"] or 1
                print(f"        {w:<4} n={ws['n']:<3} 正解 {ws['continued']/wn*100:5.1f}%")
        return 0

    if args.cmd == "export":
        n = export_outcomes_csv(db_path, Path(args.out))
        print(f"Wrote {n} rows to {args.out}")
        return 0

    if args.cmd == "show":
        a = get_alert(db_path, args.id)
        if not a:
            print(f"Alert #{args.id} not found")
            return 1
        print(f"=== Alert #{a['id']} ===")
        print(f"  ts:        {a['ts']}")
        print(f"  price:     ${a['price_usd']:,.2f}")
        print(f"  spike:     {a['spike_change_pct']:+.2f}% / {a['spike_window']} ({a['spike_direction']})")
        print(f"  score:     {a.get('spike_score')}")
        print(f"  delivered: discord={bool(a['delivered_discord'])} x={bool(a['delivered_x'])}")
        print(f"\n  reasons:")
        for r in json.loads(a.get("reasons_json") or "[]"):
            print(f"    - {r}")
        print(f"\n  summary:\n{a.get('summary') or '(none)'}")
        print(f"\n  factors ({len(a.get('factors', []))}):")
        for f in a.get("factors", []):
            print(f"    [{f['type']}/{f['source']}] {f.get('title', '')[:120]}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
