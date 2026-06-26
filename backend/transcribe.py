"""AI fallback: transcribe sung audio to timed lines with faster-whisper.

Only used when LRCLIB has no synced lyrics. The model is loaded lazily and
cached for the process lifetime (first run downloads ~0.5 GB for "small").
"""

from __future__ import annotations

from typing import List, Optional

from .lrc import Line

_MODEL = None
_MODEL_SIZE = "small"


def _get_model():
    global _MODEL
    if _MODEL is None:
        # Imported lazily so the server starts without the (heavy) dependency
        # being touched until the fallback is actually needed.
        from faster_whisper import WhisperModel

        # Prefer GPU (float16) when CUDA is available; CTranslate2's CUDA libs
        # can be missing on Windows, so fall back to CPU int8 if it fails.
        try:
            import torch
            if torch.cuda.is_available():
                _MODEL = WhisperModel(_MODEL_SIZE, device="cuda", compute_type="float16")
                return _MODEL
        except Exception:
            pass
        _MODEL = WhisperModel(_MODEL_SIZE, device="cpu", compute_type="int8")
    return _MODEL


def transcribe(audio_path: str, language: Optional[str] = None) -> List[Line]:
    """Return time-synced lines transcribed from the audio."""
    model = _get_model()
    segments, _info = model.transcribe(
        audio_path,
        language=language,
        vad_filter=True,           # skip long instrumental/silent gaps
        beam_size=5,
    )

    lines: List[Line] = []
    for seg in segments:
        text = (seg.text or "").strip()
        if text:
            lines.append(Line(time=round(float(seg.start), 3), text=text))
    return lines
