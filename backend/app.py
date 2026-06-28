"""FastAPI app: process a YouTube URL into a synced-lyrics player payload.

Run from the project root (testing-hendo/):
    uvicorn backend.app:app --reload --port 8000
Then open http://localhost:8000
"""

from __future__ import annotations

import os
# Allow torch + faster-whisper (CTranslate2) to coexist (duplicate OpenMP runtime).
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

# Load API keys etc. from a .env file in the project root (if present).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import json
import mimetypes
import queue
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import downloader, ffmpeg_setup, library, lyrics, titleparse, transcribe

# Locate ffmpeg before anything that needs it (yt-dlp downloads, Demucs reads).
ffmpeg_setup.ensure_ffmpeg_on_path()

FRONTEND_DIR = Path(__file__).parent.parent  # serves index.html / script.js / style.css

# Auto-run forced alignment ("sharpening") right after download. On by default;
# set AUTO_SHARPEN=0 in .env to keep it a manual button instead (it's heavy).
AUTO_SHARPEN = (os.environ.get("AUTO_SHARPEN", "1").strip().lower()
                not in ("0", "false", "no", "off", ""))

# Percent budget for the unified /process progress bar, so one bar covers the
# whole pipeline: download -> lyrics/transcribe -> sharpening.
_DL_FROM, _DL_TO = 2, 40       # downloading audio
_LYR_FROM, _LYR_TO = 40, 55    # finding/transcribing lyrics
_AL_FROM, _AL_TO = 55, 100     # forced-alignment sharpening

app = FastAPI(title="Synced-Lyrics Player")


class ProcessRequest(BaseModel):
    url: str


class OffsetRequest(BaseModel):
    offset: float


class LineEdit(BaseModel):
    index: int
    text: str


class WordRequest(BaseModel):
    word: str
    sentence: str = ""


def _friendly_dl_error(e: Exception) -> str:
    """Map yt-dlp's raw error to something actionable for the user."""
    msg = str(e)
    if "sign in to confirm" in msg.lower() or "not a bot" in msg.lower():
        return (
            "YouTube asked to confirm you're not a bot. Set "
            "YTDLP_COOKIES_FROM_BROWSER=chrome (or firefox/edge) in your .env, "
            "close that browser, and restart the server."
        )
    return msg


def _download_with_progress(url: str):
    """Download in a worker thread, yielding ('progress', frac) events as the
    bytes come in, then a final ('done', SongInfo) — or raising on failure.

    yt-dlp's progress hook fires synchronously inside the (blocking) download,
    so we run it off-thread and pipe the hook's updates through a queue.
    """
    q: queue.Queue = queue.Queue()

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            q.put(("progress", (got / total) if total else 0.0))

    def worker():
        try:
            q.put(("done", downloader.download(url, progress_hook=hook)))
        except Exception as e:  # yt-dlp raises many error types
            q.put(("error", e))

    threading.Thread(target=worker, daemon=True).start()
    while True:
        kind, val = q.get()
        if kind == "error":
            raise val
        yield kind, val
        if kind == "done":
            return


@app.post("/process")
def process(req: ProcessRequest):
    """Run the full pipeline (download -> lyrics -> sharpen), streaming progress.

    Emits Server-Sent-Events with {phase, percent, detail} as it works and a
    final {phase:"done", song:<payload>}. The whole thing shares one progress
    bar so the user sees continuous movement and a description of each step.
    """
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Please provide a YouTube URL.")

    def stream():
        # --- 1) Download audio (real byte progress from yt-dlp). ---
        yield _sse({"phase": "downloading", "percent": _DL_FROM,
                    "detail": "Starting download…"})
        song = None
        try:
            for kind, val in _download_with_progress(url):
                if kind == "progress":
                    pct = _DL_FROM + int(val * (_DL_TO - _DL_FROM))
                    yield _sse({"phase": "downloading", "percent": pct,
                                "detail": f"Downloading audio… {int(val * 100)}%"})
                else:  # done
                    song = val
        except Exception as e:
            yield _sse({"phase": "error",
                        "message": f"Could not download audio: {_friendly_dl_error(e)}"})
            return

        # --- 2) Metadata + lyrics (LRCLIB, then Whisper as a fallback). ---
        artist, track = titleparse.parse(song["title"], song["artist"], song["track"])
        yield _sse({"phase": "lyrics", "percent": _LYR_FROM,
                    "detail": "Looking for synced lyrics…"})
        result = lyrics.fetch(artist, track, song["title"], song["duration"])
        source = result["source"]
        lines = result["lines"]

        warning = None
        if not lines and not result["instrumental"]:
            yield _sse({"phase": "transcribing", "percent": _LYR_FROM + 3,
                        "detail": "No synced lyrics found — transcribing with AI…"})
            try:
                lines = transcribe.transcribe(song["audio_path"])
                source = "whisper" if lines else "none"
            except Exception as e:
                source = "none"
                warning = f"Transcription failed: {e}"

        # Detect the song's language (Italian word lookups are offline; other
        # languages get a friendly "not supported yet" in the UI).
        from . import language
        lang_code = language.detect(" ".join((l.get("text") or "") for l in lines))

        payload = {
            "title": song["title"],
            "artist": artist,
            "track": track,
            "audioId": song["audio_id"],
            "thumbnail": song["thumbnail"],
            "source": source,
            "instrumental": result["instrumental"],
            "language": lang_code,
            "languageName": language.name(lang_code) if lang_code else "",
            "lyrics": lines,
        }
        if warning:
            payload["warning"] = warning
        library.save(payload)
        word_count = sum(len((ln.get("text") or "").split()) for ln in lines)
        src_label = {"lrclib": "synced lyrics",
                     "whisper": "AI transcription"}.get(source, "lyrics")
        yield _sse({"phase": "lyrics", "percent": _LYR_TO,
                    "detail": (f"Found {len(lines)} lines · {word_count} words "
                               f"({src_label})" if lines else "No lyrics available")})

        # --- 3) Sharpen timing (forced alignment) right away, if possible. ---
        if AUTO_SHARPEN and lines:
            try:
                yield _sse({"phase": "loading", "percent": _AL_FROM,
                            "detail": "Loading AI models (first run can take ~30s)…"})
                from . import align  # heavy import (torch/torchaudio/demucs)
                for ev in align.align_stream(song["audio_path"], lines):
                    if ev.get("phase") == "done":
                        payload["lyrics"] = ev["lyrics"]
                        payload["aligned"] = True
                        library.save(payload)
                    else:
                        # Remap alignment's own 0–100 into the sharpening band.
                        pct = _AL_FROM + int(ev.get("percent", 0) / 100
                                             * (_AL_TO - _AL_FROM))
                        yield _sse({"phase": "sharpening", "percent": pct,
                                    "detail": ev.get("detail", "Sharpening timing…")})
            except Exception as e:
                # Non-fatal: keep the even-spaced timing and let the user play.
                yield _sse({"phase": "sharpening", "percent": _AL_TO,
                            "detail": f"Skipped sharpening ({e})"})

        yield _sse({"phase": "done", "percent": 100, "song": payload})

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/library")
def get_library():
    """List every saved song as a lightweight card."""
    return library.list_all()


@app.get("/song/{audio_id}")
def get_song(audio_id: str):
    """Return a saved song's full payload for replay (no re-download)."""
    data = library.load(audio_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Song not found in library.")
    # Backfill language detection for songs saved before that feature existed.
    if "language" not in data and data.get("lyrics"):
        from . import language
        code = language.detect(" ".join((l.get("text") or "") for l in data["lyrics"]))
        data["language"] = code
        data["languageName"] = language.name(code) if code else ""
        library.save(data)
    return data


@app.delete("/song/{audio_id}")
def delete_song(audio_id: str):
    """Remove a song from the library (deletes its audio + saved data)."""
    if not library.delete(audio_id):
        raise HTTPException(status_code=404, detail="Song not found in library.")
    return {"deleted": audio_id}


@app.post("/offset/{audio_id}")
def set_offset(audio_id: str, req: OffsetRequest):
    """Save a manual lyric-timing offset (seconds) for a song."""
    data = library.load(audio_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Song not found in library.")
    data["offset"] = round(float(req.offset), 2)
    library.save(data)
    return {"audioId": audio_id, "offset": data["offset"]}


@app.patch("/lyrics/{audio_id}")
def edit_line(audio_id: str, req: LineEdit):
    """Manually correct a single lyric line's text (e.g. fix an AI mishearing).

    The corrected text is saved to the song's meta.json — that persisted file is
    the trusted record of "right words". Editing a line drops its per-word
    alignment (the old word-times no longer match the new text); the line keeps
    its start `time`, so rendering falls back to even word spacing.
    """
    data = library.load(audio_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Song not found in library.")

    lyrics = data.get("lyrics") or []
    if not 0 <= req.index < len(lyrics):
        raise HTTPException(status_code=400, detail="Line index out of range.")

    line = lyrics[req.index]
    line["text"] = (req.text or "").strip()
    line.pop("words", None)        # stale alignment — word-times no longer match
    line["edited"] = True          # marker for the future cross-song word DB

    # Drop the now-mismatched whole-song translation for this line, if any.
    translations = data.get("translations")
    if isinstance(translations, list) and req.index < len(translations):
        translations[req.index] = ""

    library.save(data)
    return data


@app.post("/word")
def explain_word(req: WordRequest):
    """Translate + grammar-explain a single lyric word (language learning).

    Lookup order: shared word cache -> local Italian dataset (spaCy + Wiktionary,
    offline & free) -> OpenAI fallback (other languages / dataset misses).
    """
    import os

    from . import analyze  # lazy import (openai SDK)

    # 1) Cached words are served without a key or any model (cross-song reuse).
    hit = analyze.cached_word(req.word)
    if hit is not None:
        return hit

    # 2) Local offline dataset (Italian). Cache the hit so it's instant next time.
    try:
        from . import dictionary
        local = dictionary.lookup(req.word, req.sentence)
        if local is not None:
            analyze.save_word(local)
            return local
    except Exception:
        pass  # dataset unavailable (e.g. spaCy/DB missing) -> fall through to AI

    # 3) OpenAI fallback.
    if not os.environ.get("OPENAI_API_KEY"):
        raise HTTPException(
            status_code=400,
            detail="Word not in the offline dictionary. Set OPENAI_API_KEY in .env "
                   "(and restart) to look up words via AI.",
        )

    try:
        return analyze.analyze_word(req.word, req.sentence)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Word lookup failed: {e}")


@app.post("/translate/{audio_id}")
def translate_song(audio_id: str):
    """Translate the whole song's lyrics to English (chunked, streamed, cached)."""
    import os

    data = library.load(audio_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Song not found in library.")

    lyrics = data.get("lyrics") or []
    line_idx = [i for i, l in enumerate(lyrics) if (l.get("text") or "").strip()]

    def stream():
        # Already translated? Serve from the saved song (no key, no model call).
        existing = data.get("translations")
        if isinstance(existing, list) and len(existing) == len(lyrics):
            yield _sse({"phase": "done", "percent": 100, "song": data})
            return

        if not line_idx:
            data["translations"] = [""] * len(lyrics)
            data["translated"] = True
            library.save(data)
            yield _sse({"phase": "done", "percent": 100, "song": data})
            return

        if not os.environ.get("OPENAI_API_KEY"):
            yield _sse({"phase": "error",
                        "message": "Set OPENAI_API_KEY (or Ollama) in .env and restart."})
            return

        from . import analyze  # lazy import
        translations = [""] * len(lyrics)
        chunk = 10
        total = len(line_idx)
        try:
            for start in range(0, total, chunk):
                group = line_idx[start:start + chunk]
                outs = analyze.translate_lines([lyrics[i]["text"] for i in group])
                for j, i in enumerate(group):
                    translations[i] = outs[j] if j < len(outs) else ""
                done = start + len(group)
                yield _sse({"phase": "translating",
                            "percent": int(done / total * 100),
                            "detail": f"Translating… {done}/{total} lines"})

            data["translations"] = translations
            data["translated"] = True
            library.save(data)
            yield _sse({"phase": "done", "percent": 100, "song": data})
        except Exception as e:
            yield _sse({"phase": "error", "message": f"Translation failed: {e}"})

    return StreamingResponse(stream(), media_type="text/event-stream")


def _sse(obj) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/align/{audio_id}")
def align_song(audio_id: str):
    """Forced-align lyrics to the vocals, streaming live progress as SSE.

    The browser reads the `data: {...}` events to show a % progress bar. The
    heavy ML stack is imported lazily, and the final event carries the updated
    song payload (which is also saved to disk).
    """
    data = library.load(audio_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Song not found in library.")

    path = downloader.audio_path_for(audio_id)
    lines = data.get("lyrics") or []

    def stream():
        # Nothing to do — finish immediately.
        if data.get("aligned") or not lines or path is None or not path.is_file():
            if not data.get("aligned") and lines:
                # No audio to align against; mark done so we don't keep retrying.
                data["aligned"] = True
                library.save(data)
            yield _sse({"phase": "done", "percent": 100, "song": data})
            return

        # Tell the browser something is happening before the (slow, first-run)
        # import of the AI stack so the bar doesn't sit silently at 0%.
        yield _sse({"phase": "loading", "percent": 1,
                    "detail": "Loading AI models (first run can take ~30s)…"})

        from . import align  # heavy import (torch/torchaudio/demucs)
        try:
            for ev in align.align_stream(str(path), lines):
                if ev.get("phase") == "done":
                    data["lyrics"] = ev["lyrics"]
                    data["aligned"] = True
                    library.save(data)
                    yield _sse({"phase": "done", "percent": 100, "song": data})
                else:
                    yield _sse(ev)
        except Exception as e:
            yield _sse({"phase": "error", "message": f"Alignment failed: {e}"})

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/audio/{audio_id}")
def audio(audio_id: str):
    path = downloader.audio_path_for(audio_id)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Audio not found.")
    media_type = mimetypes.guess_type(str(path))[0] or "audio/mpeg"
    # FileResponse handles HTTP range requests, so seeking in <audio> works.
    return FileResponse(path, media_type=media_type, filename=path.name)


# Mount the static frontend last so the API routes above take precedence.
# Visiting "/" serves index.html; same origin as the API -> no CORS needed.
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
