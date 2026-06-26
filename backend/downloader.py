"""Download audio from a YouTube URL with yt-dlp and extract metadata."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional, TypedDict

import yt_dlp

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)


class SongInfo(TypedDict):
    audio_id: str        # stable id used in the /audio/{id} route + cache key
    audio_path: str      # absolute path to the downloaded audio file
    title: str           # raw video title
    artist: Optional[str]  # metadata artist (YouTube Music) if available
    track: Optional[str]   # metadata track name if available
    duration: int        # seconds


def _video_id(url: str) -> str:
    """Best-effort stable id from a YouTube URL; falls back to a url hash."""
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def _existing_audio(folder: Path) -> Optional[Path]:
    if not folder.is_dir():
        return None
    for f in folder.iterdir():
        if f.is_file() and f.suffix.lower() in (".mp3", ".m4a", ".webm", ".opus"):
            return f
    return None


def download(url: str) -> SongInfo:
    """Download bestaudio as mp3 into cache/<id>/ and return metadata.

    If the song was already downloaded, it is reused from cache.
    """
    audio_id = _video_id(url)
    folder = CACHE_DIR / audio_id
    folder.mkdir(exist_ok=True)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(folder / "audio.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }

    cached = _existing_audio(folder)
    # We still call extract_info (without re-downloading) to get fresh metadata,
    # but skip the heavy download when audio already exists.
    with yt_dlp.YoutubeDL({**ydl_opts, "skip_download": cached is not None}) as ydl:
        info = ydl.extract_info(url, download=cached is None)

    audio_path = _existing_audio(folder)
    if audio_path is None:
        raise RuntimeError("Audio download failed: no output file produced.")

    return SongInfo(
        audio_id=audio_id,
        audio_path=str(audio_path),
        title=info.get("title") or "",
        artist=info.get("artist") or info.get("creator") or None,
        track=info.get("track") or None,
        duration=int(info.get("duration") or 0),
    )


def audio_path_for(audio_id: str) -> Optional[Path]:
    """Locate a previously downloaded audio file by id (for the /audio route)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", audio_id or ""):
        return None  # guard against path traversal
    return _existing_audio(CACHE_DIR / audio_id)
