"""Provider call-order regression test for jp_translator (2026-08-23:
switched to Gemini-first after discovering OPENAI_API_KEY was unset on the
VM, so every translation call was silently skipping OpenAI and burning a
guaranteed-fail Grok round-trip before finally succeeding on Gemini).

Monkeypatches providers.* — no live API calls, no keys needed.

Runs standalone:  python tests/test_jp_translator_order.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jp_translator import core  # noqa: E402


def _record(order, name, ret):
    def _fn(*a, **k):
        order.append(name)
        return ret
    return _fn


def test_translate_tries_gemini_first_and_short_circuits():
    order = []
    with patch.object(core.providers, "gemini_generate_text", _record(order, "gemini", "訳文")), \
         patch.object(core.providers, "openai_generate_text", _record(order, "openai", "x")), \
         patch.object(core.providers, "grok_generate_text", _record(order, "grok", "x")), \
         patch.object(core.providers, "deepl_translate", _record(order, "deepl", "x")):
        out = core.translate("hello")
    assert out == "訳文"
    assert order == ["gemini"], f"expected gemini-only short-circuit, got {order}"


def test_translate_falls_back_gemini_openai_grok_deepl_in_order():
    order = []
    with patch.object(core.providers, "gemini_generate_text", _record(order, "gemini", None)), \
         patch.object(core.providers, "openai_generate_text", _record(order, "openai", None)), \
         patch.object(core.providers, "grok_generate_text", _record(order, "grok", None)), \
         patch.object(core.providers, "deepl_translate", _record(order, "deepl", "訳文")):
        out = core.translate("hello")
    assert out == "訳文"
    assert order == ["gemini", "openai", "grok", "deepl"], order


def test_generate_tries_gemini_first():
    order = []
    with patch.object(core.providers, "gemini_generate_text", _record(order, "gemini", "out")), \
         patch.object(core.providers, "openai_generate_text", _record(order, "openai", "x")), \
         patch.object(core.providers, "grok_generate_text", _record(order, "grok", "x")):
        out = core.generate("sys", "user")
    assert out == "out"
    assert order == ["gemini"], order


def test_generate_structured_tries_gemini_first():
    order = []
    with patch.object(core.providers, "gemini_generate_structured", _record(order, "gemini", {"a": 1})), \
         patch.object(core.providers, "openai_generate_structured", _record(order, "openai", None)), \
         patch.object(core.providers, "grok_generate_structured", _record(order, "grok", None)):
        out = core.generate_structured("sys", "user", dict)
    assert out == {"a": 1}
    assert order == ["gemini"], order


def test_matches_2026_08_22_real_incident():
    """Reproduces the actual observed pattern: OpenAI absent (returns None
    immediately, as it does with no key), Grok fails, Gemini succeeds —
    but now Gemini is tried FIRST so openai/grok are never even called."""
    order = []
    with patch.object(core.providers, "gemini_generate_text", _record(order, "gemini", "BTCは$76,340へ-0.62%下落。")), \
         patch.object(core.providers, "openai_generate_text", _record(order, "openai", None)), \
         patch.object(core.providers, "grok_generate_text", _record(order, "grok", None)):
        out = core.translate("BTC fell 0.62% to $76,340")
    assert out
    assert order == ["gemini"]  # openai/grok never invoked — 2 fewer wasted hops


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e!r}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
