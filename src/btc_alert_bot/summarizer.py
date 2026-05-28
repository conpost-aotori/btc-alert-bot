"""Generate Japanese summary via shared jp_translator chain.

呼び出しは共有モジュール ``jp_translator.generate`` (Gemini → OpenAI → Grok の
フォールバック) に委譲する。``DEFAULT_MODEL`` は後方互換のため残しているが、
実際のモデル選択は ``JP_TRANSLATOR_GEMINI_MODEL`` / ``JP_TRANSLATOR_GROK_MODEL``
環境変数で行う。
"""
from __future__ import annotations

import logging
import os

from jp_translator import generate

log = logging.getLogger(__name__)

# Kept for back-compat (caller logging only). The actual model is chosen by
# the shared jp_translator package via JP_TRANSLATOR_GEMINI_MODEL env.
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

# system prompt is split out so generate(system, user) can pass it through
# to whichever provider is active in the chain. Japanese is enforced here
# (not just in the user prompt) so the OpenAI / Grok fallback paths don't
# accidentally lapse into English when Gemini fails.
SYSTEM_PROMPT = (
    "あなたはBTC市場のリアルタイム解説アナリストです。"
    "出力は必ず日本語のみ。英語・中国語・混在は禁止。"
    "断定を避け「〜の可能性」「〜と見られる」等の慎重な語法を用い、"
    "事実 (市場観測) と仮説 (外部要因) を明確に区別すること。"
    "3行以内、各行60字以内、合計260字以内。"
)

PROMPT_TEMPLATE = """以下の急変イベントについて、**3行**で要約してください。

## 厳守事項
1. **必ず日本語のみで出力** (英語・中国語・混在は禁止)
2. 候補要因に無い情報を推測で足さない
3. 清算データは**原因ではなく増幅要因**として扱う
   ✗「ロング清算でBTC下落」 / ◎「下落を清算連鎖が増幅した可能性」
4. 同一ニュースが複数ソースに出ても **1事象として扱う** (corroboration は信頼度の参考までに)
5. 数字・固有名詞は厳密に転記、各行60字以内、絵文字は1行目冒頭のみ可

## 用語ヒント
- ATM IV: 市場が織り込む将来ボラ (高い=波乱予想)
- コンタンゴ (遠期>近期 IV): 先のリスク警戒 / バックワーデーション (遠期<近期): 足元ストレス強
- プット高 Skew: 下落ヘッジ需要 / コール高 Skew: 上昇追随
- IV-RV (vol risk premium): 正常+、深いマイナスは実現ボラ超え
- Funding 正: ロング過剰 / Funding 負: ショート過剰
- BroadUSD↑: ドル高 (BTC逆風) / VIX↑: 株式不安 / 米10年金利↑: リスクオフ
- [x_search_grok]: Grok の X 検索結果。単独なら「Xで言及あり (未確認)」、他ソースで裏が取れたら信頼度高

## 価格情報
- 現在価格: ${price_usd:,.0f}
- {window}変動: {change:+.2f}% ({direction_jp})
- 24h高値/安値: ${high_24h:,.0f} / ${low_24h:,.0f}

## 過去の類似アラート (参考、未検証)
{similar_alerts_text}

## 市場観測 (検知器が掴んだ事実)
{market_observations}

## 外部要因候補 (上位{n}件、未検証)
{factors_text}

## 出力フォーマット (厳守、3行ちょうど)
1行目: 価格・変動率・方向感を端的に (絵文字1つ可)
2行目: 市場観測 or オプション市場で最も注目すべき点を1つ
3行目: 外部要因候補があれば1つ引用、無ければテクニカル/需給で推測される動因を示唆
"""


def summarize(
    price_data: dict,
    spike: dict,
    factors: list[dict],
    similar_alerts: list[dict] | None = None,
) -> str:
    """Return a 3-line Japanese summary, or a deterministic fallback.

    Uses the shared jp_translator chain (Gemini → OpenAI → Grok). If both LLM providers
    fail, falls back to a deterministic template.
    """
    factors_text = "\n".join(_render_factor(f) for f in factors) or "- (要因情報なし)"

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

    similar_alerts_text = _format_similar_alerts(similar_alerts)

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
        similar_alerts_text=similar_alerts_text,
    )

    text = generate(
        system=SYSTEM_PROMPT,
        user=prompt,
        max_tokens=600,
        temperature=0.4,
    )
    if not text:
        log.warning("jp_translator chain (gemini→openai→grok) failed — using fallback")
        return _fallback_summary(price_data, spike, factors)
    log.info("jp_translator OK (%d chars)", len(text))
    return text.strip()


def _render_factor(f: dict) -> str:
    """Render one factor line for the Gemini prompt with optional metadata.

    The score and corroboration count are exposed so the model can decide
    which items to actually quote in the 3-line summary, while a
    direction_hint mismatch is shown explicitly to invite caution.
    """
    parts = [f"- [{f.get('type','?')}/{f.get('source','?')}]"]
    score = f.get("_score")
    if score is not None:
        parts.append(f"(score={score})")
    corr = f.get("_corroboration") or 1
    if corr >= 2:
        parts.append(f"(×{corr} sources)")
    direction = f.get("direction_hint")
    if direction:
        parts.append(f"(implies {direction})")
    parts.append(f.get("title", ""))
    return " ".join(parts)


def _format_similar_alerts(similar: list[dict] | None) -> str:
    """Render the past-similar-alerts block injected into the Gemini prompt.

    Each line is prefixed with a similarity bucket label (極めて類似 /
    類似 / やや類似) so the model can weight the match qualitatively
    without inventing precision from the raw Euclidean distance.
    """
    if not similar:
        return "- (類似履歴なし)"
    lines: list[str] = []
    for s in similar:
        ts_short = (s.get("ts") or "")[:16].replace("T", " ")
        change = s.get("change_pct", 0.0)
        window = s.get("window", "?")
        label = s.get("similarity_label") or "類似"
        # First line of the past summary, truncated.
        past_summary = (s.get("summary") or "").split("\n", 1)[0][:80]
        # Top 2 factors of that alert.
        top_factors = ", ".join(
            f"{f.get('type', '?')}:{(f.get('title') or '')[:40]}"
            for f in (s.get("factors") or [])[:2]
        )
        line = (
            f"- [{label}] {ts_short} [{change:+.2f}% / {window}] {past_summary}"
        )
        if top_factors:
            line += f" / 要因: {top_factors}"
        lines.append(line)
    return "\n".join(lines)


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
