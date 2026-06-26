"""Parse LRC timestamped lyrics into the shared [{time, text}] shape."""

from __future__ import annotations

import re
from typing import List, TypedDict


class Line(TypedDict):
    time: float   # seconds
    text: str


# Matches one or more leading timestamps like [01:23.45] or [01:23] (LRC allows
# several timestamps on a single line for repeated lines).
_TS = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")


def parse_lrc(lrc_text: str) -> List[Line]:
    """Convert raw LRC text into a time-sorted list of {time, text}."""
    lines: List[Line] = []
    for raw in (lrc_text or "").splitlines():
        stamps = list(_TS.finditer(raw))
        if not stamps:
            continue
        text = raw[stamps[-1].end():].strip()
        for m in stamps:
            minutes = int(m.group(1))
            seconds = int(m.group(2))
            frac = m.group(3) or "0"
            # Normalize fractional part to milliseconds regardless of digit count.
            millis = int(frac.ljust(3, "0")[:3])
            t = minutes * 60 + seconds + millis / 1000.0
            lines.append(Line(time=round(t, 3), text=text))

    lines.sort(key=lambda x: x["time"])
    return lines
