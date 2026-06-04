"""環境変数からデフォルト設定を読む。

全ての値は呼び出し時に上書き可能 (provider モジュール側で kwargs を取る)。
"""
from __future__ import annotations

import logging
import os


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v if v is not None else default


# API keys
GEMINI_API_KEY = _env("GEMINI_API_KEY")
XAI_API_KEY = _env("XAI_API_KEY") or _env("GROK_API_KEY")  # alias
OPENAI_API_KEY = _env("OPENAI_API_KEY")
DEEPL_API_KEY = _env("DEEPL_API_KEY")

# Models (override via env if a higher-quota or different model is needed)
#
# Gemini default = gemini-2.5-flash:
#   - 2.0-flash / 2.0-flash-lite は無料枠クォータが 0 (limit:0) で実質使用
#     不可 (2026-06 時点で本番キーで確認) — 指定すると毎回 429 で失敗し、
#     OpenAI/Grok/定型文フォールバックに落ちて品質が劣化する。
#   - 2.5-flash は本番キーで利用可、3 行サマリーを完全な日本語で生成できる
#     (途中切れは確認されず)。X で本文が切れていたのはモデルではなく、
#     サマリーが長く 280 字制限を超えて 3 行目が落ちていたのが原因
#     → summarizer 側で簡潔化して解消。
#   - flash-lite に切り替えたい場合は env JP_TRANSLATOR_GEMINI_MODEL で上書き可。
GEMINI_MODEL = _env("JP_TRANSLATOR_GEMINI_MODEL", "gemini-2.5-flash")
# OpenAI/Grok は LLM チェーンの 2 番手/3 番手フォールバック。
# providers.py の reasoning 調整層が上位モデルでも API シグネチャを
# 自動で切り替える (max_completion_tokens / reasoning_effort 等)。
GROK_MODEL = _env("JP_TRANSLATOR_GROK_MODEL", "grok-2-1212")
OPENAI_MODEL = _env("JP_TRANSLATOR_OPENAI_MODEL", "gpt-4o-mini")

# DeepL endpoint: free keys end with ':fx' and need api-free.deepl.com.
DEEPL_HOST = "https://api-free.deepl.com" if DEEPL_API_KEY.endswith(":fx") else "https://api.deepl.com"

# Timeouts
HTTP_TIMEOUT = float(_env("JP_TRANSLATOR_HTTP_TIMEOUT", "20"))

# Logging
LOG_LEVEL = _env("JP_TRANSLATOR_LOG_LEVEL", "INFO").upper()


def get_logger(name: str = "jp_translator") -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        # don't reconfigure root — only set our own level
        log.setLevel(LOG_LEVEL)
    return log
