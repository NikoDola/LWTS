"""Turn a raw YouTube title into a best-guess {artist, track}.

Heuristic only. Examples it handles:
  "Adele - Hello (Official Music Video)"      -> ("Adele", "Hello")
  "Hello - Adele [Lyrics]"                    -> ("Hello", "Adele")  (ambiguous; order kept)
  "Daft Punk - Get Lucky ft. Pharrell"        -> ("Daft Punk", "Get Lucky")
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

# Parenthetical / bracketed noise to strip: (Official Video), [HD], {Lyrics}, etc.
_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")

# Trailing/standalone noise words.
_NOISE = re.compile(
    r"\b(official\s*(music\s*)?video|official\s*audio|lyrics?|lyric\s*video|"
    r"audio|hd|hq|4k|mv|visualizer|remaster(ed)?|explicit|video)\b",
    re.IGNORECASE,
)

# "feat. X", "ft X", "featuring X" up to the end or a separator.
_FEAT = re.compile(r"\b(feat\.?|ft\.?|featuring)\b.*", re.IGNORECASE)


def _clean(text: str) -> str:
    text = _BRACKETS.sub(" ", text)
    text = _FEAT.sub(" ", text)
    text = _NOISE.sub(" ", text)
    text = text.strip(" -–—|·•\t")
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def parse(title: str, meta_artist: Optional[str] = None,
          meta_track: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """Return (artist, track). Prefers explicit metadata when present."""
    if meta_artist and meta_track:
        return _clean(meta_artist) or meta_artist, _clean(meta_track) or meta_track

    cleaned = _clean(title or "")

    # Split on the first artist/track separator.
    parts = re.split(r"\s[-–—]\s|\s[|·•]\s", cleaned, maxsplit=1)
    if len(parts) == 2:
        artist, track = parts[0].strip(), parts[1].strip()
        # If metadata gave us one side, trust it for that side.
        if meta_artist:
            artist = meta_artist
        if meta_track:
            track = meta_track
        return artist or None, track or None

    # No separator: best we can do is treat the whole thing as the track.
    return (meta_artist or None), (meta_track or cleaned or None)
