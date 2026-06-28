"""Download audio from a YouTube URL with yt-dlp and extract metadata."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Optional, TypedDict

import yt_dlp

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)


def _cookie_opts() -> dict:
    """yt-dlp cookie options, to get past YouTube's "confirm you're not a bot".

    Configure in .env (then restart the server), in priority order:
      YTDLP_COOKIES_FROM_BROWSER=chrome   # or firefox / edge / brave …
      YTDLP_COOKIES_FILE=C:\\path\\to\\cookies.txt   # exported cookies.txt

    With the browser option, that browser must be fully closed while
    downloading (it locks its own cookie database).
    """
    browser = (os.environ.get("YTDLP_COOKIES_FROM_BROWSER") or "").strip()
    if browser:
        return {"cookiesfrombrowser": (browser,)}
    cookie_file = (os.environ.get("YTDLP_COOKIES_FILE") or "").strip()
    if cookie_file:
        return {"cookiefile": cookie_file}
    return {}


class SongInfo(TypedDict):
    audio_id: str        # stable id used in the /audio/{id} route + cache key
    audio_path: str      # absolute path to the downloaded audio file
    title: str           # raw video title
    artist: Optional[str]  # metadata artist (YouTube Music) if available
    track: Optional[str]   # metadata track name if available
    duration: int        # seconds
    thumbnail: Optional[str]  # cover-art image URL (YouTube CDN), if available


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


def download(url: str, progress_hook=None) -> SongInfo:
    """Download bestaudio as mp3 into cache/<id>/ and return metadata.

    If the song was already downloaded, it is reused from cache.

    `progress_hook`, if given, is a yt-dlp progress hook (called with a status
    dict during the download) so callers can stream a live progress bar.
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
        **_cookie_opts(),   # auth cookies to bypass the YouTube bot check
    }
    if progress_hook is not None:
        ydl_opts["progress_hooks"] = [progress_hook]

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
        thumbnail=_best_thumbnail(info, audio_id),
    )


def _best_thumbnail(info: dict, audio_id: str) -> Optional[str]:
    """Pick a cover-art URL from yt-dlp metadata.

    Prefers the highest-resolution entry in `thumbnails`, falls back to the
    single `thumbnail` field, and finally to a predictable YouTube URL built
    from the video id (works whenever audio_id is a real 11-char video id).
    """
    thumbs = info.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        best = max(
            thumbs,
            key=lambda t: (t.get("preference", 0), t.get("width") or 0),
        )
        if best.get("url"):
            return best["url"]
    if info.get("thumbnail"):
        return info["thumbnail"]
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", audio_id):
        return f"https://i.ytimg.com/vi/{audio_id}/hqdefault.jpg"
    return None


def audio_path_for(audio_id: str) -> Optional[Path]:
    """Locate a previously downloaded audio file by id (for the /audio route)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", audio_id or ""):
        return None  # guard against path traversal
    return _existing_audio(CACHE_DIR / audio_id)
