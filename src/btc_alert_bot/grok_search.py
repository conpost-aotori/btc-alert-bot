"""Grok-powered X (Twitter) search via xAI Responses API.

xAI's legacy ``search_parameters`` Live Search was deprecated in favor
of the new Agent Tools API at ``/v1/responses``. We use the built-in
``x_search`` tool so Grok calls X-keyword-search / X-semantic-search
itself, then synthesises a short Japanese summary of the 1-2 most
plausible drivers behind the just-fired spike.

Per the user's design choices:

- B: Grok is the primary X source; Nitter (x_monitor.py) stays as an
     opt-in fallback only when NITTER_ACCOUNTS is explicitly set.
- c: We pass the spike direction + change% to Grok so it understands
     the context (e.g. selloff vs rally) when picking posts.
- i: One summarised factor per fire rather than N individual tweets.

Cost: grok-4-fast at ~$0.20/M in + $0.50/M out plus a small X-search
surcharge per call. A typical query (≈1k tokens) is around $0.005;
at the bot's 1-3 alerts/day cadence the monthly bill is well under $1.

Setup:
- Create an xAI account at https://console.x.ai/ (min $5 deposit needed)
- Set ``XAI_API_KEY`` in env. When unset, fetcher silently returns [].
- Optional: ``XAI_MODEL=grok-4`` for higher-fidelity summaries.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

# Responses API — replaces the deprecated /v1/chat/completions search_parameters.
XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"

# grok-4-fast is plenty for short summarisation and ~10× cheaper than grok-4.
# Override via XAI_MODEL env if the user wants a different tier.
DEFAULT_MODEL = os.getenv("XAI_MODEL") or "grok-4-fast"

# Grok with x_search runs multiple internal tool calls (keyword + semantic
# search → synthesis), so it routinely takes 15-25s. Set the per-request
# timeout to 25s and the gather_factors deadline must accommodate it
# (we bump that constant in analyzers.py accordingly).
GROK_TIMEOUT_S = 25.0

# Cap on result token budget so even pathological replies stay short.
MAX_OUTPUT_TOKENS = 600


# Short, English prompt — keeps Grok's internal reasoning + tool-call
# rounds to a minimum. Detailed multi-rule Japanese prompts caused it
# to overrun even a 25s budget; the simpler form completes in ~10s.
_SYSTEM_PROMPT = (
    "You are a BTC market scanner. Use the X search tool to find the "
    "1-2 most likely drivers of the user's price spike. Output Japanese, "
    "max 3 short lines, each under 80 chars. Format line 1 as "
    "「@account: 内容」. If nothing relevant, reply only with '該当なし'. "
    "Prefer news / regulation / exchange / whale moves / macro over opinions."
)


def _extract_assistant_text_and_citations(
    payload: dict,
) -> tuple[str, list[str]]:
    """Pull the final assistant message text and any URL citations.

    Responses API returns ``output[]`` containing reasoning blocks,
    tool-call blocks, and finally a message block with ``content[]``.
    The citations live inside ``content[i].annotations`` as items of
    ``type == "url_citation"``.
    """
    text_chunks: list[str] = []
    citations: list[str] = []
    for entry in payload.get("output") or []:
        if entry.get("type") != "message":
            continue
        if entry.get("role") not in (None, "assistant"):
            continue
        for content in entry.get("content") or []:
            if content.get("type") != "output_text":
                continue
            t = content.get("text") or ""
            if t:
                text_chunks.append(t)
            for ann in content.get("annotations") or []:
                if ann.get("type") == "url_citation":
                    url = ann.get("url")
                    if url:
                        citations.append(url)
    return ("\n".join(text_chunks).strip(), citations)


def call_grok_x_search(
    system_prompt: str,
    user_prompt: str,
    *,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
    timeout_s: float = GROK_TIMEOUT_S,
) -> tuple[str, list[str]] | None:
    """Shared /v1/responses + x_search call. Returns ``(text, citations)`` or None.

    Public so the whale_monitor / x_list_monitor modules can reuse the same
    transport without duplicating the request/parse boilerplate. None means
    "skip silently" (no key, network failure, or bad HTTP) — callers should
    return [] when they get None.
    """
    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        return None
    payload = {
        "model": DEFAULT_MODEL,
        "tools": [{"type": "x_search"}],
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_output_tokens": max_output_tokens,
    }
    try:
        resp = requests.post(
            XAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout_s,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("Grok call failed: %s", e)
        return None
    return _extract_assistant_text_and_citations(data)


def fetch_grok_x_search(spike: dict | None = None) -> list[dict]:
    """Ask Grok to scan X for posts that explain this spike.

    Returns 0 or 1 factor entries. Never raises — failures degrade to
    an empty list so the rest of the alert pipeline continues.
    Silent skip when ``XAI_API_KEY`` is not set (opt-in feature).
    """
    direction = (spike or {}).get("direction", "")
    change = (spike or {}).get("change", 0.0)
    window = (spike or {}).get("window", "")

    direction_en = (
        "drop" if direction == "down" else "rally" if direction == "up" else "move"
    )
    user_prompt = (
        f"BTC just had a {change:+.2f}% {direction_en} on {window} window. "
        f"Search X for the most likely driver in the last 1-2 hours."
    )

    result = call_grok_x_search(_SYSTEM_PROMPT, user_prompt)
    if result is None:
        return []
    text, citations = result
    if not text or "該当なし" in text:
        log.info("Grok X search: no relevant posts found")
        return []

    # First non-empty line becomes the headline; up to two more lines append.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    headline = lines[0] if lines else text[:120]
    extra = " / ".join(lines[1:3]) if len(lines) > 1 else ""
    title = f"{headline} ({extra})" if extra else headline

    log.info(
        "Grok X search OK (model=%s, %d chars, %d citations)",
        DEFAULT_MODEL, len(text), len(citations),
    )
    return [{
        "type": "x_search_grok",
        "source": "Grok/X Live Search",
        "title": title[:240],
        "url": citations[0] if citations else "",
        "published": datetime.now(timezone.utc).isoformat(),
        "tags": ["x_search", "ai_summary"],
        "direction_hint": direction or None,
    }]
