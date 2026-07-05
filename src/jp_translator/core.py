"""高レベル API: translate / generate / generate_structured.

フォールバック順は **OpenAI -> Grok -> Gemini -> DeepL** で固定。
OpenAI を主、Gemini はセーフティネット (OpenAI と Grok が両方失敗したときのみ)。
Gemini 2.5 系の thinking モードによる途中切れを避けるため、通常運用では
OpenAI を優先する方針。

DeepL は直訳しかできない (生成プロンプトに従えない) ので、``generate`` と
``generate_structured`` では除外され、LLM 3 段 (OpenAI→Grok→Gemini) のみ。
"""
from __future__ import annotations

from typing import Any, Type

from . import config, providers

log = config.get_logger(__name__)


# ---------------------------------------------------------------------------
# translate — 直訳。3段全部試す。
# ---------------------------------------------------------------------------

_TRANSLATE_SYSTEM_TEMPLATE = (
    "あなたは正確な機械翻訳エンジンです。入力文を {target_name} に翻訳して "
    "ください。\n"
    "- 出力は翻訳結果テキストのみ。前置き・引用符・説明は禁止。\n"
    "- 数値・固有名詞・通貨記号・パーセント記号は原文のまま保持。\n"
    "- 文体は簡潔・自然・中立。"
)


_LANG_NAMES = {"JA": "日本語", "EN": "English"}


def translate(
    text: str,
    *,
    source_lang: str = "EN",
    target_lang: str = "JA",
    style_hint: str | None = None,
    gemini_api_key: str | None = None,
    xai_api_key: str | None = None,
    openai_api_key: str | None = None,
    deepl_api_key: str | None = None,
) -> str | None:
    """直訳。OpenAI → Grok → Gemini → DeepL の順に試行。全段失敗で None。

    Args:
        text: 翻訳元テキスト。
        source_lang: ISO 639 (DeepL用)。LLM 段のプロンプトには影響なし。
        target_lang: ``JA`` または ``EN`` を想定。
        style_hint: 任意。LLM 段の system に追記される (例: "金融市場の文体で")。
            DeepL には影響しない。
    """
    if not text or not text.strip():
        return ""

    target_name = _LANG_NAMES.get(target_lang.upper(), target_lang)
    system = _TRANSLATE_SYSTEM_TEMPLATE.format(target_name=target_name)
    if style_hint:
        system += "\n- " + style_hint

    # 1. OpenAI (主)
    out = providers.openai_generate_text(
        system, text, api_key=openai_api_key, temperature=0.2,
    )
    if out:
        return out

    # 2. Grok
    out = providers.grok_generate_text(
        system, text, api_key=xai_api_key, temperature=0.2,
    )
    if out:
        return out

    # 3. Gemini (セーフティネット)
    out = providers.gemini_generate_text(
        system, text, api_key=gemini_api_key, temperature=0.2,
    )
    if out:
        return out

    # 4. DeepL (literal)
    out = providers.deepl_translate(
        text,
        api_key=deepl_api_key,
        source_lang=source_lang,
        target_lang=target_lang,
    )
    if out:
        return out

    log.error("translate: all 4 providers failed (openai/grok/gemini/deepl)")
    return None


def translate_batch(
    texts: list[str],
    *,
    source_lang: str = "EN",
    target_lang: str = "JA",
    gemini_api_key: str | None = None,
    xai_api_key: str | None = None,
    openai_api_key: str | None = None,
    deepl_api_key: str | None = None,
) -> list[str | None]:
    """バッチ直訳。要素ごとに ``translate`` を呼ぶ。

    戻り値の長さは ``texts`` と同じ。失敗要素は ``None`` が入る。
    """
    if not texts:
        return []
    out: list[str | None] = []
    for t in texts:
        out.append(
            translate(
                t,
                source_lang=source_lang,
                target_lang=target_lang,
                gemini_api_key=gemini_api_key,
                xai_api_key=xai_api_key,
                openai_api_key=openai_api_key,
                deepl_api_key=deepl_api_key,
            )
        )
    return out


# ---------------------------------------------------------------------------
# generate — プロンプト生成 (OpenAI → Grok → Gemini)
# ---------------------------------------------------------------------------

def generate(
    system: str,
    user: str,
    *,
    max_tokens: int = 2048,
    temperature: float = 0.3,
    gemini_api_key: str | None = None,
    xai_api_key: str | None = None,
    openai_api_key: str | None = None,
) -> str | None:
    """プロンプトに基づくテキスト生成。OpenAI → Grok → Gemini の順に試行。

    DeepL は使わない (生成不可)。3 段とも失敗で None。
    Gemini は最後尾のセーフティネット (OpenAI と Grok が両方失敗時のみ)。
    """
    out = providers.openai_generate_text(
        system, user,
        api_key=openai_api_key, max_tokens=max_tokens, temperature=temperature,
    )
    if out:
        return out
    out = providers.grok_generate_text(
        system, user,
        api_key=xai_api_key, max_tokens=max_tokens, temperature=temperature,
    )
    if out:
        return out
    out = providers.gemini_generate_text(
        system, user,
        api_key=gemini_api_key, max_tokens=max_tokens, temperature=temperature,
    )
    if out:
        return out
    log.error("generate: all 3 LLM providers failed (openai/grok/gemini)")
    return None


def generate_structured(
    system: str,
    user: str,
    schema: Type[Any],
    *,
    max_tokens: int = 2048,
    temperature: float = 0.3,
    gemini_api_key: str | None = None,
    xai_api_key: str | None = None,
    openai_api_key: str | None = None,
) -> Any | None:
    """Pydantic schema を強制した構造化出力。OpenAI → Grok → Gemini の順に試行。"""
    out = providers.openai_generate_structured(
        system, user, schema,
        api_key=openai_api_key, max_tokens=max_tokens, temperature=temperature,
    )
    if out is not None:
        return out
    out = providers.grok_generate_structured(
        system, user, schema,
        api_key=xai_api_key, max_tokens=max_tokens, temperature=temperature,
    )
    if out is not None:
        return out
    out = providers.gemini_generate_structured(
        system, user, schema,
        api_key=gemini_api_key, max_tokens=max_tokens, temperature=temperature,
    )
    if out is not None:
        return out
    log.error("generate_structured: all 3 LLM providers failed (openai/grok/gemini)")
    return None
