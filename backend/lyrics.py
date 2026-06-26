"""Look up time-synced lyrics from LRCLIB (https://lrclib.net)."""

from __future__ import annotations

from typing import List, Optional, TypedDict

import httpx

from .lrc import Line, parse_lrc

BASE = "https://lrclib.net/api"
USER_AGENT = "synced-lyrics-player (https://github.com/local/testing-hendo)"
HEADERS = {"User-Agent": USER_AGENT}


class LyricsResult(TypedDict):
    source: str            # "lrclib" | "none"
    instrumental: bool
    lines: List[Line]      # empty when unsynced / not found
    plain: Optional[str]   # plain lyrics if that's all LRCLIB had


def _empty(source: str = "none", instrumental: bool = False,
           plain: Optional[str] = None) -> LyricsResult:
    return LyricsResult(source=source, instrumental=instrumental, lines=[], plain=plain)


def _from_record(rec: dict) -> LyricsResult:
    if rec.get("instrumental"):
        return _empty(source="lrclib", instrumental=True)
    synced = rec.get("syncedLyrics")
    if synced:
        return LyricsResult(
            source="lrclib", instrumental=False,
            lines=parse_lrc(synced), plain=rec.get("plainLyrics"),
        )
    # Only plain lyrics available -> caller will fall back to Whisper for timing.
    return _empty(source="none", plain=rec.get("plainLyrics"))


def fetch(artist: Optional[str], track: Optional[str], title: str,
          duration: int = 0) -> LyricsResult:
    """Try an exact get first, then a fuzzy search. Returns synced lines if found."""
    with httpx.Client(headers=HEADERS, timeout=15.0) as client:
        # 1) Exact-ish match using parsed fields + duration.
        if artist and track:
            params = {"artist_name": artist, "track_name": track}
            if duration:
                params["duration"] = duration
            try:
                r = client.get(f"{BASE}/get", params=params)
                if r.status_code == 200:
                    res = _from_record(r.json())
                    if res["lines"] or res["instrumental"]:
                        return res
            except httpx.HTTPError:
                pass

        # 2) Fuzzy search; pick the closest by duration when we know it.
        query = " ".join(p for p in (artist, track) if p) or title
        try:
            r = client.get(f"{BASE}/search", params={"q": query})
            if r.status_code == 200:
                results = r.json() or []
                best = _pick_best(results, duration)
                if best:
                    res = _from_record(best)
                    if res["lines"] or res["instrumental"]:
                        return res
                    # keep plain lyrics around for the caller / Whisper note
                    return _empty(source="none", plain=res.get("plain"))
        except httpx.HTTPError:
            pass

    return _empty()


def _pick_best(results: list, duration: int) -> Optional[dict]:
    if not results:
        return None
    if not duration:
        # Prefer the first result that actually has synced lyrics.
        for rec in results:
            if rec.get("syncedLyrics"):
                return rec
        return results[0]
    synced = [r for r in results if r.get("syncedLyrics")] or results
    return min(synced, key=lambda r: abs((r.get("duration") or 0) - duration))
