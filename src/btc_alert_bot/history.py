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

Future work (Phase 2.5):
- Similarity search (cosine on the features_json vector)
- Outcome backfill (did the spike continue / reverse 30min later?)
- Threshold tuning by replaying past spikes against new score weights
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

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
# CLI: python -m btc_alert_bot.history {list|show <id>|init}
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
