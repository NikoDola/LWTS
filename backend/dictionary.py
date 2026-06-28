"""Offline Italian word lookup: spaCy morphology + a Wiktionary gloss DB.

This replaces the per-word OpenAI call for Italian. spaCy (it_core_news_md) turns
the clicked surface form into a lemma + grammar, using the lyric LINE as context
to disambiguate; the lemma is then looked up in the it_en.sqlite dictionary built
by build_dict.py. The result uses the same dict shape as analyze.WordAnalysis, so
the /word endpoint and the frontend popup are unchanged.

Returns None whenever it can't resolve a word (DB not built, word missing, or a
non-Italian token) so the caller can fall back to OpenAI.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "it_en.sqlite"

_nlp = None
_EDGE_PUNCT = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)

# spaCy UPOS -> (friendly English label, Wiktionary `pos` strings to prefer).
_POS = {
    "NOUN": ("noun", ("noun",)),
    "PROPN": ("proper noun", ("name", "noun")),
    "VERB": ("verb", ("verb",)),
    "AUX": ("verb", ("verb",)),
    "ADJ": ("adjective", ("adj",)),
    "ADV": ("adverb", ("adv",)),
    "ADP": ("preposition", ("prep",)),
    "DET": ("article", ("det", "article", "determiner")),
    "PRON": ("pronoun", ("pron",)),
    "NUM": ("numeral", ("num",)),
    "CCONJ": ("conjunction", ("conj",)),
    "SCONJ": ("conjunction", ("conj",)),
    "INTJ": ("interjection", ("intj",)),
}

# spaCy morph feature values -> English.
_GENDER = {"Masc": "masculine", "Fem": "feminine"}
_NUMBER = {"Sing": "singular", "Plur": "plural"}
_TENSE = {"Pres": "present", "Past": "past", "Imp": "imperfect", "Fut": "future"}
_MOOD = {"Ind": "indicative", "Sub": "subjunctive", "Cnd": "conditional",
         "Imp": "imperative"}
_PERSON = {"1": "1st", "2": "2nd", "3": "3rd"}

# Wiktionary form-of tags -> English (used when following an inflected form).
_TAG_GENDER = {"masculine": "masculine", "feminine": "feminine"}
_TAG_NUMBER = {"singular": "singular", "plural": "plural"}
_TAG_TENSE = {"present": "present", "past": "past", "imperfect": "imperfect",
              "future": "future"}
_TAG_MOOD = {"indicative": "indicative", "subjunctive": "subjunctive",
             "conditional": "conditional", "imperative": "imperative"}
_TAG_PERSON = {"first-person": "1st", "second-person": "2nd", "third-person": "3rd"}


def _norm(w: str) -> str:
    return _EDGE_PUNCT.sub("", (w or "").strip().lower())


def _get_nlp():
    global _nlp
    if _nlp is None:
        import spacy  # lazy: server starts even if spaCy isn't installed
        _nlp = spacy.load("it_core_news_md", disable=["ner"])
    return _nlp


def available() -> bool:
    """True if the dictionary DB has been built (build_dict.py)."""
    return DB_PATH.is_file()


def _pick_token(nlp, surface: str, sentence: str):
    """The spaCy token for the clicked word — within its line for context,
    else the word parsed on its own."""
    if sentence.strip():
        for tok in nlp(sentence):
            if _norm(tok.text) == surface:
                return tok
    doc = nlp(surface)
    return doc[0] if len(doc) else None


def _first_morph(morph, key: str):
    vals = morph.get(key) if morph is not None else []
    return vals[0] if vals else None


_COLS = "pos, senses, gender, form_of, tags"


def _rows(con, lemma: str):
    return con.execute(
        f"SELECT {_COLS} FROM entries WHERE lemma=?", (lemma,)
    ).fetchall()


def _pick(rows, pos_matches):
    """The row whose POS matches spaCy (e.g. come-as-adverb), else the first."""
    for r in rows:
        if r[0] in pos_matches:
            return r
    return rows[0]


def _merge_senses(rows) -> list:
    """All distinct meanings across every POS entry for a word, in Wiktionary's
    natural order (so come -> how, as, like, ..., as soon as). Natural order is
    more reliable than spaCy's POS for function words like come/che/da."""
    out, seen = [], set()
    for r in rows:
        for s in json.loads(r[1]) or []:
            g = s.get("g")
            if g and g not in seen:
                seen.add(g)
                out.append(s)
    return out


def _grammar_from_morph(morph) -> dict:
    return {
        "gender": _GENDER.get(_first_morph(morph, "Gender"), "-"),
        "number": _NUMBER.get(_first_morph(morph, "Number"), "-"),
        "tense": _TENSE.get(_first_morph(morph, "Tense"), "-"),
        "mood": _MOOD.get(_first_morph(morph, "Mood"), "-"),
        "person": _PERSON.get(_first_morph(morph, "Person"), "-"),
    }


def _grammar_from_tags(tags) -> dict:
    pick = lambda m: next((m[t] for t in tags if t in m), "-")
    return {
        "gender": pick(_TAG_GENDER), "number": pick(_TAG_NUMBER),
        "tense": pick(_TAG_TENSE), "mood": pick(_TAG_MOOD),
        "person": pick(_TAG_PERSON),
    }


def _merge(primary: dict, secondary: dict) -> dict:
    """Take each field from `primary`, falling back to `secondary` for '-'."""
    return {k: (primary[k] if primary[k] != "-" else secondary[k]) for k in primary}


# Wiktionary describes clitic compounds ("dimmi") and some inflections with a
# grammatical sentence instead of a plain gloss. Detect those so we can show a
# SHORT meaning as the headline and demote the description to a detail line.
_DESC_RE = re.compile(
    r"\b(compound of|inflection of|form of|the infinitive|"
    r"(?:feminine|masculine|singular|plural)(?: \w+)? of|"
    r"participle of|gerund of|diminutive of|augmentative of|"
    r"superlative of|comparative of|alternative form of|obsolete \w+ of)\b",
    re.I,
)
_VERB_OF = re.compile(r"(?:form of|infinitive)\s+([a-zà-ù']+)", re.I)


def _short(gloss: str) -> str:
    """A concise headline: keep the text before any parenthetical clarification
    (e.g. 'to utter (produce speech…)' -> 'to utter')."""
    g = gloss.split(" (")[0].strip(" ;,")
    return g or gloss


def _refine(gloss: str, con) -> tuple[str, str, str]:
    """Return (short_meaning, detail, base_lemma) for description-style glosses.

    For clitic compounds / inflections (e.g. "compound of da', ... with mi
    (give me)") pull out a short headline — the parenthesised English if present,
    else the base verb — and report the base lemma so the caller can show its
    full meaning list. `detail` keeps the original description. For an ordinary
    gloss everything passes through unchanged.
    """
    if not _DESC_RE.search(gloss):
        return gloss, "", ""
    m = re.search(r"\(([^)]+)\)", gloss)               # explicit English in parens
    if m:
        inner = m.group(1).strip(" “”\"'")
        if re.search("[A-Za-z]", inner):
            return inner, gloss, ""
    mv = _VERB_OF.search(gloss)                          # else fall back to the verb
    if mv:
        base_lemma = mv.group(1).lower()
        rows = _rows(con, base_lemma)
        if rows:
            base_senses = json.loads(rows[0][1]) or []
            base_gloss = next((s["g"] for s in base_senses
                               if s.get("g") and not _DESC_RE.search(s["g"])), "")
            if base_gloss:
                return base_gloss, gloss, base_lemma
    return gloss, "", ""


def lookup(word: str, sentence: str = "") -> dict | None:
    """Resolve an Italian word to a WordAnalysis-shaped dict, or None."""
    surface = _norm(word)
    if not surface or not available():
        return None

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        tok = _pick_token(_get_nlp(), surface, sentence or "")
        lemma = _norm(tok.lemma_) if tok else surface
        upos = tok.pos_ if tok else ""
        friendly_pos, pos_matches = _POS.get(upos, (upos.lower(), ()))

        rows = _rows(con, lemma)
        if not rows and lemma != surface:
            rows = _rows(con, surface)
        if not rows:
            return None

        primary = _pick(rows, pos_matches)
        db_pos, db_gender, form_of = primary[0], primary[2], primary[3]
        tags = json.loads(primary[4] or "[]")
        senses = _merge_senses(rows)
        morph = tok.morph if tok else None
        display_lemma = tok.lemma_ if tok else surface
        if upos != "PROPN":
            display_lemma = display_lemma.lower()
        detail = ""

        # Inflected/derived form (e.g. vivrò -> vivere): follow the link to the
        # base word's senses, and trust the form's tags for the grammar.
        if form_of:
            base_rows = _rows(con, form_of)
            if base_rows:
                senses = _merge_senses(base_rows)
                db_gender = _pick(base_rows, pos_matches)[2]
                display_lemma = form_of
            grammar = _merge(_grammar_from_tags(tags), _grammar_from_morph(morph))
            translation = _short(senses[0]["g"])
        else:
            grammar = _merge(_grammar_from_morph(morph), _grammar_from_tags(tags))
            # Clitic compound / inflection described in prose -> short headline,
            # and swap in the base word's full meaning list (e.g. dimmi -> dire).
            translation, detail, base_lemma = _refine(senses[0]["g"], con)
            if base_lemma:
                base_rows = _rows(con, base_lemma)
                if base_rows:
                    senses = _merge_senses(base_rows)
                    display_lemma = base_lemma
            translation = _short(translation if not base_lemma else senses[0]["g"])
    finally:
        con.close()

    if grammar["gender"] == "-" and db_gender and db_gender != "-":
        grammar["gender"] = db_gender

    # Drop grammatical descriptions ("feminine plural of amaro") from the meaning
    # list when there's at least one real gloss, so come -> how/as/like stays clean.
    real = [s for s in senses if s.get("g") and not _DESC_RE.search(s["g"])] or senses
    meanings = [s["g"] for s in real if s.get("g")]
    examples = [
        {"meaning": s["g"], "text": s["ex"], "en": s.get("en", "")}
        for s in real if s.get("ex")
    ]

    return {
        "word": word,
        "language": "Italian",
        "translation": translation,
        "detail": detail,
        "meanings": meanings,
        "examples": examples,
        "lemma": display_lemma,
        "part_of_speech": friendly_pos or db_pos or "-",
        **grammar,
        "source": "dictionary",
    }
