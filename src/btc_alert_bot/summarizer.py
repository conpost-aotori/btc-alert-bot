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
# to whichever provider is active in the chain.
SYSTEM_PROMPT = "あなたはBTC市場アナリストです。日本語で3行以内の市場サマリーを書きます。"

PROMPT_TEMPLATE = """あなたはBTC市場アナリストです。以下の急変イベントについて、
日本語で**3行以内**の要約を作成してください。

## 厳守ルール
- 断定を避ける(「〜の可能性」「〜と見られる」を使う)
- 候補要因に挙がっていない情報は推測で足さない
- 「市場観測 (score, ATR, OI, funding 等)」「オプション市場 (IV/Skew)」「外部要因候補 (ニュース等)」を区別する
- **清算データは「原因」ではなく「増幅要因」**として扱う
  例: ✗ 「ロング清算でBTC下落」 → ◎ 「下落を清算連鎖が増幅した可能性」
- 同じニュースが複数アグリゲーター (Google News / CryptoPanic 等) に出ていても
  独立した複数の証拠とは扱わず、1つの事象として要約する
- 数字・固有名詞は正確に転記する
- 各行は60文字以内、絵文字は冒頭1つだけ可

## 用語ヒント (出てきたら正しく扱う)
- ATM IV: 市場が織り込む将来ボラ。高い=波乱予想
- コンタンゴ: 遠期 IV > 近期 IV (落ち着いた現状、先のリスク警戒)
- バックワーデーション: 遠期 < 近期 (足元のストレス強)
- プット高 Skew: 下落ヘッジ需要 / コール高 Skew: 上昇追随
- IV - RV (vol risk premium): 正常はプラス、マイナス深掘り=実現超え
- Funding 正: ロング過剰 / Funding 負: ショート過剰
- BroadUSD (広いドル指数) 上昇: ドル高 (BTC逆風) / VIX 上昇: 株式不安 (BTC波乱要因)
- 米10年金利 上昇: リスクオフ (BTC逆風) / 逆イールド: リセッション懸念
- [x_search_grok/Grok/X Live Search]: Grok が X を AI 検索した結果。
  単一factor で 1-2 件の事象を要約。他ソースで裏が取れたら信頼度高、
  単独なら「未確認だが X で言及あり」程度に扱う

## 価格情報
- 現在価格: ${price_usd:,.0f}
- {window}変動: {change:+.2f}% ({direction_jp})
- 24h高値/安値: ${high_24h:,.0f} / ${low_24h:,.0f}

## 過去の類似アラート (参考、最大3件、未検証)
{similar_alerts_text}

## 市場観測 (検知器が観測した事実)
{market_observations}

## 外部要因候補 (上位{n}件、未検証)
{factors_text}

## 出力フォーマット (3行)
1行目: 価格・変動率・方向 (絵文字可)
2行目: 市場観測またはオプション市場で目立つ点を1つ
3行目: 外部要因候補/類似履歴があれば1点、なければテクニカル要因の可能性を示唆
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
    """Render the past-similar-alerts block injected into the Gemini prompt."""
    if not similar:
        return "- (類似履歴なし)"
    lines: list[str] = []
    for s in similar:
        ts_short = (s.get("ts") or "")[:16].replace("T", " ")
        change = s.get("change_pct", 0.0)
        window = s.get("window", "?")
        # First line of the past summary, truncated.
        past_summary = (s.get("summary") or "").split("\n", 1)[0][:80]
        # Top 2 factors of that alert.
        top_factors = ", ".join(
            f"{f.get('type', '?')}:{(f.get('title') or '')[:40]}"
            for f in (s.get("factors") or [])[:2]
        )
        line = f"- {ts_short} [{change:+.2f}% / {window}] {past_summary}"
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
