"""Persist processed songs to disk so they survive restarts.

Each song already has its audio cached in cache/<audio_id>/; here we also save
the full player payload (title, artist, lyrics, ...) as cache/<audio_id>/meta.json
so the library can be listed and a song replayed without re-downloading.
"""

from __future__ import annotations

import json
import re
import shutil
from typing import List, Optional

from .downloader import CACHE_DIR

META_NAME = "meta.json"
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


def save(payload: dict) -> None:
    """Write a processed-song payload to its cache folder."""
    audio_id = payload.get("audioId")
    if not audio_id:
        return
    folder = CACHE_DIR / audio_id
    folder.mkdir(exist_ok=True)
    (folder / META_NAME).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def load(audio_id: str) -> Optional[dict]:
    """Return the saved payload for a song, or None if not stored."""
    meta = CACHE_DIR / audio_id / META_NAME
    if not meta.is_file():
        return None
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def delete(audio_id: str) -> bool:
    """Delete a song's entire cache folder (audio + meta). Returns True if removed."""
    if not _ID_RE.fullmatch(audio_id or ""):
        return False  # guard against path traversal
    folder = CACHE_DIR / audio_id
    if not folder.is_dir():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return not folder.exists()


def list_all() -> List[dict]:
    """Return lightweight cards for every saved song (newest first)."""
    cards = []
    if not CACHE_DIR.is_dir():
        return cards
    for folder in CACHE_DIR.iterdir():
        meta = folder / META_NAME
        if not meta.is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        cards.append(
            {
                "audioId": data.get("audioId", folder.name),
                "title": data.get("title", ""),
                "artist": data.get("artist"),
                "track": data.get("track"),
                "savedAt": meta.stat().st_mtime,
            }
        )
    cards.sort(key=lambda c: c.get("savedAt", 0), reverse=True)
    return cards
