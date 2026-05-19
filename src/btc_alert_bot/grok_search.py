"""Grok-powered X (Twitter) search for the spike-explanation factor.

xAI's chat-completions endpoint supports a ``search_parameters`` block that
makes Grok perform live retrieval before generating its answer. With
``sources=[{"type": "x"}]`` it pulls fresh posts directly from X, which
is more reliable than the public Nitter mirrors (most of which are dead).

This module asks Grok to identify the 1-2 most plausible drivers behind
the just-fired alert and returns a single factor entry summarizing what
it found. Per the user's design choices:

- B: Grok is the primary X source; Nitter (x_monitor.py) stays as an
     opt-in fallback only when the user explicitly sets NITTER_ACCOUNTS.
- c: We pass the spike direction + change% to Grok so it understands the
     context (e.g. selloff vs rally) when picking posts to highlight.
- i: One summarised factor per fire rather than N individual tweets, so
     the Gemini summariser stays focused on the macro story.

Cost: grok-4-fast at ~$0.20/M in + $0.50/M out + a small live-search
surcharge. A typical query (≈1k tokens) is sub-cent; at the bot's
1-3 alerts/day cadence the monthly bill is well under $1.

Setup:
- Create an xAI account at https://console.x.ai/ (min $5 deposit needed)
- Set ``XAI_API_KEY`` in .env / Lightsail env. When unset, this fetcher
  silently returns [] and the rest of the pipeline keeps working.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"
# grok-4-fast is plenty for short summarisation and ~10× cheaper than grok-4.
# Override via env if the user wants a different tier.
DEFAULT_MODEL = os.getenv("XAI_MODEL", "grok-4-fast")

# Tight timeout — Grok with Live Search takes a few seconds longer than
# plain chat. Bounded so a slow xAI day doesn't blow the 30s
# gather_factors deadline.
GROK_TIMEOUT_S = 12.0

# X search lookback window. Anything older than this is unlikely to have
# caused a spike that just fired.
LOOKBACK_MIN = 60

# Cap on result token budget so even pathological replies stay short.
MAX_OUTPUT_TOKENS = 600


_SYSTEM_PROMPT = (
    "あなたは Crypto 速報スキャナーです。X 上の直近の BTC 関連投稿を\n"
    "スキャンし、価格変動を説明する蓋然性が高い 1〜2 件の事象を抽出します。\n\n"
    "厳守ルール:\n"
    "- 1行目に最も影響度が高い事象を「@account: 内容」形式で日本語で書く\n"
    "- 2行目以降は補足や反対意見があれば最大2行まで\n"
    "- 各行 80文字以内\n"
    "- 該当する投稿が見つからない場合は『該当なし』とだけ返す\n"
    "- 推測は禁止、必ず実際の投稿に基づくこと\n"
    "- 個人の感想ではなく、ニュース/規制/取引所/大口動向/著名人発言を優先\n"
)


def fetch_grok_x_search(spike: dict | None = None) -> list[dict]:
    """Ask Grok to scan X for posts that explain this spike.

    Returns 0 or 1 factor entries. Never raises — failures degrade to
    an empty list so the rest of the alert pipeline continues.
    Silent skip when ``XAI_API_KEY`` is not set (opt-in feature).
    """
    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        return []

    direction = (spike or {}).get("direction", "")
    change = (spike or {}).get("change", 0.0)
    window = (spike or {}).get("window", "")
    direction_jp = (
        "上昇" if direction == "up" else "下落" if direction == "down" else ""
    )

    user_prompt = (
        f"BTC で {window} ウィンドウ {change:+.2f}% の{direction_jp}アラートが\n"
        f"発火しました。直近1時間以内に X で投稿された、この値動きを説明\n"
        f"する可能性が高い情報を探し、ルールに従って 1〜2 件にまとめてください。\n"
        f"候補: ニュース・規制・取引所イベント・大口動向・著名人発言・マクロ指標。"
    )

    now = datetime.now(timezone.utc)
    from_date = (now - timedelta(minutes=LOOKBACK_MIN)).strftime("%Y-%m-%d")
    to_date = now.strftime("%Y-%m-%d")

    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        # Live Search forces Grok to actually search rather than guess.
        "search_parameters": {
            "mode": "on",
            "sources": [{"type": "x"}],
            "max_search_results": 10,
            "from_date": from_date,
            "to_date": to_date,
        },
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0.3,
    }

    try:
        resp = requests.post(
            XAI_CHAT_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=GROK_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("Grok X search failed: %s", e)
        return []

    text = (
        ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        or ""
    ).strip()
    if not text or "該当なし" in text:
        log.info("Grok X search: no relevant posts found")
        return []

    # Citations may appear at top level or nested in the message — accept both.
    citations = (
        data.get("citations")
        or ((data.get("choices") or [{}])[0].get("message") or {}).get("citations")
        or []
    )
    first_url = ""
    if citations and isinstance(citations[0], str):
        first_url = citations[0]
    elif citations and isinstance(citations[0], dict):
        first_url = citations[0].get("url") or citations[0].get("source") or ""

    # First non-empty line becomes the headline; up to two more lines append.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    headline = lines[0] if lines else text[:120]
    extra = " / ".join(lines[1:3]) if len(lines) > 1 else ""
    title = f"{headline} ({extra})" if extra else headline

    log.info("Grok X search OK (model=%s, %d chars)", DEFAULT_MODEL, len(text))
    return [{
        "type": "x_search_grok",
        "source": "Grok/X Live Search",
        "title": title[:240],
        "url": first_url,
        "published": now.isoformat(),
        "tags": ["x_search", "ai_summary"],
        "direction_hint": direction or None,
    }]
