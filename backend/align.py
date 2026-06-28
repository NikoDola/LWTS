"""Forced alignment: snap known lyrics onto the actual (sung) audio.

Pipeline:
  1. Demucs isolates the vocals from the music.
  2. torchaudio's MMS forced-alignment model places each *known* lyric word
     onto the vocal audio, giving real per-word timestamps.

Alignment is done per line, using each LRCLIB line's start time as an anchor and
the next line's start as the window end. This keeps it fast, bounded in memory,
and prevents a single mistake from drifting the whole song.

Models download on first use (Demucs ~hundreds of MB, MMS ~hundreds of MB) and
run on CPU, so the first alignment of a song is slow — results are cached by the
caller, so it only happens once per song.
"""

from __future__ import annotations

import os
# torch and faster-whisper (CTranslate2) each ship an OpenMP runtime; loading
# both in one process trips an init error. Allow the duplicate before they load.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import difflib
import re
import statistics
import unicodedata
from collections import Counter
from typing import Dict, List

import torch
import torchaudio
from torchaudio.pipelines import MMS_FA as _BUNDLE

SR = 16000          # MMS forced-alignment model sample rate
WINDOW_PAD = 0.2    # seconds of padding around each line's window
LAST_LINE_SECONDS = 4.0

# Use the GPU automatically when available (e.g. your GTX 1650, or a rented
# card later); fall back to CPU otherwise. The rest of the code is unchanged.
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

_demucs_model = None
_fa_model = None
_tokenizer = None
_aligner = None

_NORM_RE = re.compile(r"[^a-z]+")


# --- model loaders (lazy, cached for the process) ------------------------

def _load_demucs():
    global _demucs_model
    if _demucs_model is None:
        from demucs.pretrained import get_model
        _demucs_model = get_model("htdemucs")
        _demucs_model.eval()
        _demucs_model.to(DEVICE)
    return _demucs_model


def _load_fa():
    global _fa_model, _tokenizer, _aligner
    if _fa_model is None:
        # with_star=True keeps the model's "star" output column, which lets the
        # aligner absorb audio that matches no lyric token (intros, ad-libs,
        # instrument bleed) instead of dragging real words onto it.
        _fa_model = _BUNDLE.get_model(with_star=True).to(DEVICE)
        _fa_model.eval()
        _tokenizer = _BUNDLE.get_tokenizer()
        _aligner = _BUNDLE.get_aligner()
    return _fa_model, _tokenizer, _aligner


# --- vocal separation ----------------------------------------------------

SEP_CHUNK_SECONDS = 20.0   # process the mix in chunks so we can report progress
SEP_OVERLAP_SECONDS = 1.0  # context carried into each chunk (trimmed after)


def _separate_iter(audio_path: str):
    """Generator: yields ('progress', fraction) then ('result', vocals[1,time]@16k).

    Demucs in this version has no progress callback, so we run it on consecutive
    chunks of the song ourselves and report after each — giving a real %.
    """
    from demucs.apply import apply_model
    from demucs.audio import AudioFile

    model = _load_demucs()
    wav = AudioFile(audio_path).read(
        streams=0, samplerate=model.samplerate, channels=model.audio_channels
    )
    ref = wav.mean(0)
    wav = (wav - ref.mean()) / (ref.std() + 1e-8)

    sr = model.samplerate
    total = wav.shape[-1]
    chunk = int(SEP_CHUNK_SECONDS * sr)
    overlap = int(SEP_OVERLAP_SECONDS * sr)
    voc_idx = model.sources.index("vocals")
    out = torch.zeros(model.audio_channels, total)

    pos = 0
    while pos < total:
        end = min(pos + chunk, total)
        seg_start = max(0, pos - overlap)          # extra context, trimmed below
        seg = wav[:, seg_start:end]
        with torch.no_grad():
            sources = apply_model(
                model, seg[None], device=DEVICE, progress=False, split=True, overlap=0.1
            )[0]
        voc = sources[voc_idx].to("cpu")            # [channels, seg_time]
        lead = pos - seg_start
        out[:, pos:end] = voc[:, lead:lead + (end - pos)]
        pos = end
        if DEVICE == "cuda":
            torch.cuda.empty_cache()                # keep VRAM low on small GPUs
        yield ("progress", min(1.0, pos / total))

    out = out * ref.std() + ref.mean()
    mono = out.mean(0, keepdim=True)
    yield ("result", torchaudio.functional.resample(mono, sr, SR))


def separate_vocals(audio_path: str) -> torch.Tensor:
    """Return the isolated vocals as a [1, time] mono tensor at 16 kHz."""
    vocals = None
    for kind, val in _separate_iter(audio_path):
        if kind == "result":
            vocals = val
    return vocals


# --- text + alignment helpers --------------------------------------------

def _normalize(word: str) -> str:
    # Fold accents to base letters (Italian à/è/é/ì/ò/ù -> a/e/e/i/o/u) so the
    # alignment model keeps the full word, then drop anything non a-z.
    decomposed = unicodedata.normalize("NFKD", word.lower())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NORM_RE.sub("", stripped)


def _interp(disp_words: List[str], start: float, end: float) -> List[dict]:
    """Even fallback when alignment isn't possible for a line/word."""
    n = len(disp_words)
    span = max(0.001, end - start)
    return [
        {"text": disp_words[j], "time": round(start + (j / n) * span, 3)}
        for j in range(n)
    ]


def _align_line(vocals: torch.Tensor, total_sec: float, disp_words: List[str],
                start: float, end: float, model, tokenizer, aligner) -> List[dict]:
    ws = max(0.0, start - WINDOW_PAD)
    we = min(total_sec, end + WINDOW_PAD)
    seg = vocals[:, int(ws * SR): int(we * SR)]

    norm = [_normalize(w) for w in disp_words]
    alignable = [k for k, n in enumerate(norm) if n]
    transcript = [norm[k] for k in alignable]

    if not transcript or seg.size(1) < int(SR * 0.2):
        return _interp(disp_words, start, end)

    try:
        with torch.inference_mode():
            emission, _ = model(seg.to(DEVICE))
        emission = emission.to("cpu")                  # run forced_align on CPU
        # Wrap with leading/trailing star tokens so non-lyric audio at the window
        # edges (intro speech, ad-libs) is absorbed by the star, not the words.
        starred = ["*"] + transcript + ["*"]
        token_spans = aligner(emission[0], tokenizer(starred))
        word_spans = token_spans[1:-1]                 # drop the two star groups
        ratio = seg.size(1) / emission.size(1)        # samples per frame
        times: Dict[int, float] = {}
        for spans, k in zip(word_spans, alignable):
            times[k] = ws + spans[0].start * ratio / SR
    except Exception:
        return _interp(disp_words, start, end)

    # Build result; interpolate words that had no alignable characters.
    n = len(disp_words)
    span = max(0.001, end - start)
    result = []
    for j in range(n):
        t = times.get(j, start + (j / n) * span)
        result.append({"text": disp_words[j], "time": round(t, 3)})

    # Keep times non-decreasing so the highlight never jumps backwards.
    for j in range(1, len(result)):
        if result[j]["time"] < result[j - 1]["time"]:
            result[j]["time"] = result[j - 1]["time"]
    return result


# --- global intro-offset detection ---------------------------------------

def _lyric_word_times(lines: List[dict]):
    """Flatten lyric lines into [(normalized_word, approx_time)] using LRCLIB
    line times with even within-line interpolation."""
    out = []
    n = len(lines)
    for i, line in enumerate(lines):
        words = (line.get("text") or "").split()
        if not words:
            continue
        start = float(line["time"])
        end = float(lines[i + 1]["time"]) if i + 1 < n else start + LAST_LINE_SECONDS
        span = max(0.001, end - start)
        for j, w in enumerate(words):
            nw = _normalize(w)
            if nw:
                out.append((nw, start + (j / len(words)) * span))
    return out


def detect_offset(audio_path: str, lines: List[dict]) -> float:
    """Estimate a global time shift between LRCLIB timing and this recording.

    Transcribes the audio (Whisper understands *content*, so it tells spoken
    intros apart from sung lyrics), matches the transcript to the known lyrics,
    and takes the median time difference. Returns 0.0 when it can't tell.
    """
    from . import transcribe
    try:
        whisper_words = transcribe.transcribe_words(audio_path)
    except Exception:
        return 0.0

    wnorm = [(_normalize(w), t) for w, t in whisper_words]
    wnorm = [(w, t) for w, t in wnorm if w]
    lyric = _lyric_word_times(lines)
    if len(wnorm) < 5 or len(lyric) < 5:
        return 0.0

    sm = difflib.SequenceMatcher(
        None, [w for w, _ in lyric], [w for w, _ in wnorm], autojunk=False
    )
    diffs = []
    for block in sm.get_matching_blocks():
        for k in range(block.size):
            diffs.append(wnorm[block.b + k][1] - lyric[block.a + k][1])
    if len(diffs) < 8:
        return 0.0

    # Repeated lyrics (choruses) create spurious matches, so the median is
    # unreliable. The true offset is the dominant *cluster* of time-differences.
    # Bin to 1 s, take the largest clusters, and prefer the earliest one (chorus
    # repeats show up as larger-offset harmonics of the real shift).
    binned = Counter(round(d) for d in diffs)
    max_count = max(binned.values())
    candidates = [b for b, c in binned.items() if c >= 0.5 * max_count]
    chosen = min(candidates)
    cluster = [d for d in diffs if abs(d - chosen) <= 1.5]
    if len(cluster) < max(8, 0.12 * len(diffs)):
        return 0.0

    offset = statistics.median(cluster)
    return round(offset, 2) if abs(offset) > 1.0 else 0.0


# Progress budget: vocal separation is the slow part, so it owns most of the bar.
_SYNC_TO = 14                     # percent reached after offset detection
_SEP_FROM, _SEP_TO = 14, 78       # percent range covered by separation
_ALIGN_FROM, _ALIGN_TO = 78, 99   # percent range covered by per-line alignment


def align_stream(audio_path: str, lines: List[dict]):
    """Generator yielding progress dicts, ending with a final lyrics payload.

    Events: {"phase","percent","detail"} during work, then
            {"phase":"done","percent":100,"lyrics":[...],"autoOffset":float}.
    """
    # 1) Detect & apply a global intro/timing offset so per-line windows land on
    #    the real singing (e.g. music videos with a long spoken intro).
    yield {"phase": "syncing", "percent": 3,
           "detail": "Finding where the lyrics start…"}
    offset = detect_offset(audio_path, lines)
    if offset:
        lines = [{**ln, "time": max(0.0, float(ln["time"]) + offset)} for ln in lines]
        yield {"phase": "syncing", "percent": _SYNC_TO,
               "detail": f"Shifted lyrics by {offset:+.1f}s to match this video"}
    else:
        yield {"phase": "syncing", "percent": _SYNC_TO,
               "detail": "No intro offset needed"}

    # 2) Separate vocals.
    yield {"phase": "separating", "percent": _SEP_FROM,
           "detail": "Loading model & isolating vocals…"}

    vocals = None
    for kind, val in _separate_iter(audio_path):
        if kind == "progress":
            pct = _SEP_FROM + int(val * (_SEP_TO - _SEP_FROM))
            yield {"phase": "separating", "percent": pct,
                   "detail": "Isolating vocals from the music…"}
        else:
            vocals = val

    total_sec = vocals.size(1) / SR
    model, tokenizer, aligner = _load_fa()

    out = []
    n = len(lines)
    total_words = sum(len((ln.get("text") or "").split()) for ln in lines)

    # Announce what we're about to do so the user sees the scope of the work.
    yield {"phase": "aligning", "percent": _ALIGN_FROM,
           "detail": f"Found {total_words} words across {n} lines — "
                     f"placing each on the vocals…"}

    done_words = 0
    placed_words = 0     # words we actually pinned to a timestamp
    for i, line in enumerate(lines):
        text = (line.get("text") or "").strip()
        start = float(line["time"])
        if i + 1 < n:
            end = float(lines[i + 1]["time"])
        else:
            end = min(total_sec, start + LAST_LINE_SECONDS)
        end = max(end, start + 0.3)

        disp_words = text.split()
        words = (
            _align_line(vocals, total_sec, disp_words, start, end,
                        model, tokenizer, aligner)
            if disp_words else []
        )
        out.append({"time": round(start, 3), "text": text, "words": words})

        done_words += len(disp_words)
        placed_words += len(words)
        pct = _ALIGN_FROM + int((i + 1) / n * (_ALIGN_TO - _ALIGN_FROM))
        yield {"phase": "aligning", "percent": pct,
               "detail": f"Aligning words… {done_words}/{total_words} "
                         f"({placed_words} matched) · line {i + 1}/{n}"}

    yield {"phase": "done", "percent": 100, "lyrics": out, "autoOffset": offset}


def align(audio_path: str, lines: List[dict]) -> List[dict]:
    """Return lyric lines with per-word times (drains align_stream)."""
    result: List[dict] = []
    for ev in align_stream(audio_path, lines):
        if ev.get("phase") == "done":
            result = ev["lyrics"]
    return result
