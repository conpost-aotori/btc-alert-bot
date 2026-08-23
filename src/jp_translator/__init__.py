"""jp_translator — Gemini -> OpenAI -> Grok -> DeepL 日本語翻訳/生成フォールバックチェーン。

公開 API:
    translate(text, *, source_lang='EN', target_lang='JA') -> str | None
        直訳。Gemini -> OpenAI -> Grok -> DeepL を順に試す。

    generate(system, user, *, max_tokens=2048, temperature=0.3) -> str | None
        プロンプトに基づくテキスト生成。Gemini -> OpenAI -> Grok のみ
        (DeepL は生成に使えない)。

    generate_structured(system, user, schema, *, max_tokens=2048) -> Any | None
        Pydantic スキーマで JSON 出力を強制。Gemini -> OpenAI -> Grok のみ。

全段失敗した場合は `None` を返し、`logging.ERROR` を 1 行出す。
各 BOT 側で deterministic な fallback に流すこと。

順序は core.py で一元管理 (2026-08-23: Gemini を主に変更)。
"""
from __future__ import annotations

from .core import generate, generate_structured, translate

__all__ = ["translate", "generate", "generate_structured"]
__version__ = "0.1.0"
