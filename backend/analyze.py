"""Explain a single lyric word (translation + grammar) via OpenAI.

A single structured call per *new* word. Cached to disk **by word** (not by
sentence), so the same word reuses across every song — instant and free after
the first lookup. Used by the language-learning word popup.

Provider: OpenAI (set OPENAI_API_KEY). Model via OPENAI_MODEL, default a cheap one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from pydantic import BaseModel, Field

from .downloader import CACHE_DIR

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
WORD_CACHE = CACHE_DIR / "words"
WORD_CACHE.mkdir(parents=True, exist_ok=True)

_client = None  # lazily created so the server starts without a key

_EDGE_PUNCT = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)

SYSTEM_PROMPT = (
    "You are a language tutor for ENGLISH speakers learning from song lyrics. "
    "You are given a single WORD and the lyric LINE it appears in. Detect the "
    "source language. ALWAYS answer in English.\n"
    "Translate ONLY the single given WORD — never the whole line, and never the "
    "neighbouring words. The line is background context ONLY, used to pick the "
    "correct SENSE of the one word. Do NOT let the meaning of nearby words leak "
    "into the gloss. The 'translation' field is a short English gloss (1-4 words) "
    "of just that one word, as it would appear in a dictionary.\n"
    "Mentally test yourself: the gloss must be a valid translation of the WORD on "
    "its OWN, with the line hidden. If your gloss only makes sense because of the "
    "next word, it is WRONG — strip it back to just this word.\n"
    "Examples (Italian):\n"
    "- WORD 'cantare' / LINE 'Lasciatemi cantare' -> 'to sing' (NOT 'let me sing').\n"
    "- WORD 'lasciatemi' / LINE 'Lasciatemi cantare' -> 'let me' (NOT 'let me sing').\n"
    "- WORD 'per' / LINE 'Io vivrò per avere te' -> 'for' or 'in order to' "
    "(NOT 'to have' — that is the next word 'avere', not 'per').\n"
    "- WORD 'avere' / LINE 'per avere te' -> 'to have' (the verb itself).\n"
    "Always fill 'translation', 'lemma', and 'part_of_speech'. For "
    "gender/number/tense/mood/person, give the value when it applies, or \"-\" when "
    "it does not (e.g. tense for a noun). Keep every field short."
)


class WordAnalysis(BaseModel):
    language: str = Field(description="Detected source language in English, e.g. 'Italian'")
    translation: str = Field(description="The word's meaning IN ENGLISH (1-4 words). Never the source language.")
    lemma: str = Field(description="Base/dictionary form of the word in the SOURCE language (infinitive for verbs)")
    part_of_speech: str = Field(description="Always fill: noun, verb, adjective, adverb, preposition, article, pronoun, ...")
    gender: str = Field(description="masculine / feminine / - if not applicable")
    number: str = Field(description="singular / plural / - if not applicable")
    tense: str = Field(description="verb tense in English, or - if not a verb")
    mood: str = Field(description="verb mood (indicative, ...) in English, or -")
    person: str = Field(description="verb person (1st singular, ...), or -")
    example: str = Field(description="A short example sentence in the source language + its English translation")
    note: str = Field(description="One short learner tip about this word, in English")


def _get_client():
    global _client
    if _client is None:
        import openai  # lazy import
        # OPENAI_BASE_URL lets us point at a local Ollama server (OpenAI-compatible)
        # instead of api.openai.com — same SDK, no key/billing needed.
        _client = openai.OpenAI(base_url=os.environ.get("OPENAI_BASE_URL") or None)
    return _client


_TRANSLATE_SYSTEM = (
    "Translate the given song-lyric line into English. Translate the COMPLETE line "
    "— every word and clause, in order. Do NOT omit, shorten, summarize, or skip any "
    "part of it. Keep it natural but faithful to the full meaning. "
    "Reply with ONLY the English translation — no quotes, notes, or extra text."
)


def _translate_one(client, text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": _TRANSLATE_SYSTEM},
            {"role": "user", "content": text},
        ],
    )
    out = (completion.choices[0].message.content or "").strip()
    return out.strip('"').strip()


def translate_lines(texts: list[str]) -> list[str]:
    """Translate lyric lines to English, ONE line per request.

    Small local models can't reliably return a list of N translations for N
    lines (they mash several into one), so we translate each line individually.
    This guarantees exactly `len(texts)` results, each aligned to its own line.
    """
    if not texts:
        return []
    client = _get_client()
    return [_translate_one(client, t) for t in texts]


def _normalize(word: str) -> str:
    """Lowercase + strip surrounding punctuation — the cross-song cache key."""
    return _EDGE_PUNCT.sub("", (word or "").strip().lower())


def _cache_path(word: str) -> Path:
    key = hashlib.sha1(_normalize(word).encode("utf-8")).hexdigest()
    return WORD_CACHE / f"{key}.json"


def save_word(result: dict) -> None:
    """Persist a word analysis to the shared word cache (keyed by its `word`)."""
    word = (result or {}).get("word", "")
    if not _normalize(word):
        return
    try:
        _cache_path(word).write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def cached_word(word: str):
    """Return the cached analysis for `word`, or None — never calls the API."""
    if not _normalize(word):
        return None
    path = _cache_path(word)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def analyze_word(word: str, sentence: str) -> dict:
    """Return a WordAnalysis dict for `word`, using `sentence` for context.

    Cached by word, so the same word reuses across all songs.
    """
    word = (word or "").strip()
    sentence = (sentence or "").strip()
    if not _normalize(word):
        raise ValueError("No word provided.")

    hit = cached_word(word)
    if hit is not None:
        return hit
    cache_file = _cache_path(word)

    client = _get_client()
    user_msg = f"WORD: {word}\nLINE: {sentence or '(no surrounding line)'}"
    completion = client.chat.completions.parse(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        response_format=WordAnalysis,
    )
    parsed = completion.choices[0].message.parsed
    if parsed is None:
        refusal = completion.choices[0].message.refusal
        raise RuntimeError(refusal or "The model returned no result.")

    result = parsed.model_dump()
    result["word"] = word

    try:
        cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass

    return result
