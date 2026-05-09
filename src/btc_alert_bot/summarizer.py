"""Generate Japanese summary via Gemini.

Uses the modern ``google-genai`` SDK (the older ``google.generativeai`` is EOL).
Default model is ``gemini-2.5-flash-lite`` because it sits in a different
free-tier quota bucket than ``gemini-2.0-flash`` and is more than enough
for a 3-line summary.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# Override via env if needed (e.g. fall back to gemini-1.5-flash).
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

PROMPT_TEMPLATE = """あなたはBTC市場アナリストです。以下の急変イベントについて、
日本語で**3行以内**の要約を作成してください。

## 厳守ルール
- 断定を避ける(「〜の可能性」「〜と見られる」を使う)
- 候補要因に挙がっていない情報は推測で足さない
- 「市場観測 (score, ATR, OI, funding 等)」「オプション市場 (IV/Skew)」「外部要因候補 (ニュース等)」を区別する
- 数字・固有名詞は正確に転記する
- 各行は60文字以内、絵文字は冒頭1つだけ可

## 用語ヒント (出てきたら正しく扱う)
- ATM IV: 市場が織り込む将来ボラ。高い=波乱予想
- コンタンゴ: 遠期 IV > 近期 IV (落ち着いた現状、先のリスク警戒)
- バックワーデーション: 遠期 < 近期 (足元のストレス強)
- プット高 Skew: 下落ヘッジ需要 / コール高 Skew: 上昇追随
- IV - RV (vol risk premium): 正常はプラス、マイナス深掘り=実現超え
- Funding 正: ロング過剰 / Funding 負: ショート過剰

## 価格情報
- 現在価格: ${price_usd:,.0f}
- {window}変動: {change:+.2f}% ({direction_jp})
- 24h高値/安値: ${high_24h:,.0f} / ${low_24h:,.0f}

## 市場観測 (検知器が観測した事実)
{market_observations}

## 外部要因候補 (上位{n}件、未検証)
{factors_text}

## 出力フォーマット (3行)
1行目: 価格・変動率・方向 (絵文字可)
2行目: 市場観測またはオプション市場で目立つ点を1つ
3行目: 外部要因候補があれば1点、なければテクニカル要因の可能性を示唆
"""


def summarize(price_data: dict, spike: dict, factors: list[dict]) -> str:
    """Return a 3-line Japanese summary, or a deterministic fallback."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        log.warning("GEMINI_API_KEY missing — using template fallback")
        return _fallback_summary(price_data, spike, factors)

    factors_text = "\n".join(
        f"- [{f['type']}/{f['source']}] {f['title']}" for f in factors
    ) or "- (要因情報なし)"

    # Market observations: prefer the structured `reasons` list from the
    # composite detector; fall back to a one-line summary for legacy spikes.
    reasons = spike.get("reasons") or []
    score = spike.get("score")
    obs_lines: list[str] = []
    if score is not None:
        obs_lines.append(f"- 合成スコア: {score}")
    obs_lines.extend(f"- {r}" for r in reasons)
    if not obs_lines:
        obs_lines = ["- (詳細観測なし — レガシー閾値判定)"]
    market_observations = "\n".join(obs_lines)

    prompt = PROMPT_TEMPLATE.format(
        price_usd=price_data["price_usd"],
        window=spike["window"],
        change=spike["change"],
        direction_jp="上昇" if spike["direction"] == "up" else "下落",
        high_24h=price_data["high_24h"],
        low_24h=price_data["low_24h"],
        n=len(factors),
        factors_text=factors_text,
        market_observations=market_observations,
    )

    try:
        # Lazy-import so a missing SDK doesn't break the bot at startup.
        from google import genai  # type: ignore

        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=DEFAULT_MODEL,
            contents=prompt,
        )
        text = (getattr(resp, "text", "") or "").strip()
        if not text:
            log.warning("Gemini returned empty text — using fallback")
            return _fallback_summary(price_data, spike, factors)
        log.info("Gemini OK (model=%s, %d chars)", DEFAULT_MODEL, len(text))
        return text
    except ImportError:
        log.warning("google-genai not installed — using fallback")
        return _fallback_summary(price_data, spike, factors)
    except Exception as e:
        log.warning("Gemini call failed (%s) — using fallback", e)
        return _fallback_summary(price_data, spike, factors)


def _fallback_summary(price_data: dict, spike: dict, factors: list[dict]) -> str:
    """Used when Gemini is unavailable. Keeps the bot still functional."""
    arrow = "📈" if spike["direction"] == "up" else "📉"
    lines = [
        f"{arrow} BTC ${price_data['price_usd']:,.0f} "
        f"({spike['change']:+.2f}% / {spike['window']})"
    ]
    if factors:
        lines.append(f"候補要因: {factors[0]['title'][:80]}")
    else:
        lines.append("特定要因なし — テクニカル要因の可能性")
    return "\n".join(lines)
