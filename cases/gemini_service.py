"""
Google Gemini API helper (server-side only — keep API keys out of the browser).
Uses the `google-generativeai` SDK. Set GEMINI_API_KEY in the environment.
"""
from __future__ import annotations

from typing import Optional

from django.conf import settings


def is_configured() -> bool:
    key = (getattr(settings, "GEMINI_API_KEY", None) or "").strip()
    return bool(key)


def generate_text(
    *,
    user_prompt: str,
    system_instruction: Optional[str] = None,
    max_output_tokens: int = 8192,
    temperature: float = 0.35,
) -> str:
    """
    Call Gemini and return plain text. Raises RuntimeError on failure or empty/blocked output.
    """
    if not is_configured():
        raise RuntimeError("Gemini is not configured (missing GEMINI_API_KEY).")

    try:
        import google.generativeai as genai
    except ImportError as e:
        raise RuntimeError(
            "google-generativeai is not installed. Run: pip install google-generativeai"
        ) from e

    api_key = settings.GEMINI_API_KEY.strip()
    model_name = (getattr(settings, "GEMINI_MODEL", None) or "gemini-2.0-flash").strip()

    genai.configure(api_key=api_key)

    kwargs = {"model_name": model_name}
    if system_instruction:
        kwargs["system_instruction"] = system_instruction

    model = genai.GenerativeModel(**kwargs)

    cfg = genai.types.GenerationConfig(
        max_output_tokens=max_output_tokens,
        temperature=temperature,
    )

    resp = model.generate_content(user_prompt, generation_config=cfg)
    text = _response_text(resp)
    if not text:
        raise RuntimeError("Gemini returned no text (blocked or empty response).")
    return text.strip()


def _response_text(resp) -> str:
    """Best-effort extraction of text from GenerateContentResponse."""
    if getattr(resp, "text", None):
        return resp.text
    candidates = getattr(resp, "candidates", None) or []
    parts_out = []
    for c in candidates:
        content = getattr(c, "content", None)
        parts = getattr(content, "parts", None) if content else None
        if not parts:
            continue
        for p in parts:
            t = getattr(p, "text", None)
            if t:
                parts_out.append(t)
    return "".join(parts_out)
