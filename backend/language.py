"""Detect a song's language so the app can offer Italian word lookups only.

Uses `lingua`, which discriminates similar languages (Italian vs Spanish) well
even on short text. We restrict it to a handful of likely song languages to keep
it fast and accurate. The detector is built lazily and reused.
"""

from __future__ import annotations

_detector = None

NAMES = {  # ISO 639-1 -> friendly English name
    "it": "Italian", "es": "Spanish", "en": "English",
    "fr": "French", "pt": "Portuguese", "de": "German",
}


def _get():
    global _detector
    if _detector is None:
        from lingua import Language, LanguageDetectorBuilder
        langs = [Language.ITALIAN, Language.SPANISH, Language.ENGLISH,
                 Language.FRENCH, Language.PORTUGUESE, Language.GERMAN]
        _detector = LanguageDetectorBuilder.from_languages(*langs).build()
    return _detector


def detect(text: str) -> str:
    """Return an ISO 639-1 code ('it', 'es', …), or '' if undetermined."""
    text = (text or "").strip()
    if len(text) < 8:
        return ""
    try:
        lang = _get().detect_language_of(text)
    except Exception:
        return ""
    return lang.iso_code_639_1.name.lower() if lang else ""


def name(code: str) -> str:
    return NAMES.get(code, (code or "").upper() or "Unknown")
