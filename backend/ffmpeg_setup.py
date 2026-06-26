"""Make sure the bundled/installed `ffmpeg` is findable by this process.

ffmpeg is required by yt-dlp (to extract mp3) and by Demucs (to read audio for
alignment). On Windows it's commonly installed via winget but only added to the
*user* PATH, so a terminal opened before the install — or any non-login shell —
won't see it. We locate it and prepend its folder to this process's PATH so the
server works regardless of how it was launched.
"""

from __future__ import annotations

import glob
import os
import shutil


def ensure_ffmpeg_on_path() -> bool:
    """Return True if ffmpeg is now resolvable; add its dir to PATH if needed."""
    if shutil.which("ffmpeg"):
        return True

    candidates = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        winget = os.path.join(local, "Microsoft", "WinGet")
        # Real binary inside the installed package, then the winget shim folder.
        candidates += glob.glob(
            os.path.join(winget, "Packages", "Gyan.FFmpeg*", "**", "bin"),
            recursive=True,
        )
        candidates.append(os.path.join(winget, "Links"))

    for d in candidates:
        if os.path.isfile(os.path.join(d, "ffmpeg.exe")):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            return True

    return False
