"""Read a duration out of an ISO-BMFF (MP4/MOV) container header.

Why this exists
---------------
`media_probe` deliberately does not inspect media — there is no ffprobe in the render
runtime — so it returns `duration: None`. Nothing else supplies one for video either:
peak extraction only handles WAV. The result is that any video render whose sidecars
do not state a duration falls back to a fixed 600s hint, so a 30-second clip draws on
a ten-minute timeline and every marker sits in the first 5% of it.

The duration is already in the file. ISO-BMFF stores it in the `mvhd` box inside
`moov` as a timescale plus a duration count, which needs a few hundred bytes rather
than a decode. Fetching a byte range is far cheaper than downloading the media, and
this is pure stdlib.

`moov` sits at the front of a faststart file and at the end otherwise, so both ends
are worth a look before giving up.
"""
from __future__ import annotations

import struct
from typing import Optional

__all__ = ["parse_mvhd_duration", "find_mvhd_duration"]

# A generous ceiling: longer than any plausible single asset here, and it rejects the
# nonsense that comes out of misparsing a box length.
_MAX_PLAUSIBLE_SECONDS = 24 * 3600.0


def parse_mvhd_duration(data: bytes) -> Optional[float]:
    """Find an `mvhd` box in `data` and return its duration in seconds.

    Scans for the box type rather than walking the tree, because a partial range read
    may start mid-container. The version/timescale/duration layout is checked for
    self-consistency so a chance occurrence of the bytes "mvhd" cannot yield a
    duration.
    """
    if not data:
        return None
    start = 0
    while True:
        idx = data.find(b"mvhd", start)
        if idx < 0:
            return None
        start = idx + 4
        body = idx + 4
        # version(1) + flags(3)
        if body + 4 > len(data):
            continue
        version = data[body]
        p = body + 4
        try:
            if version == 1:
                # creation(8) modification(8) timescale(4) duration(8)
                if p + 28 > len(data):
                    continue
                timescale = struct.unpack(">I", data[p + 16:p + 20])[0]
                duration = struct.unpack(">Q", data[p + 20:p + 28])[0]
            elif version == 0:
                # creation(4) modification(4) timescale(4) duration(4)
                if p + 16 > len(data):
                    continue
                timescale = struct.unpack(">I", data[p + 8:p + 12])[0]
                duration = struct.unpack(">I", data[p + 12:p + 16])[0]
            else:
                continue
        except struct.error:
            continue
        if not timescale or duration in (0, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
            continue
        seconds = duration / float(timescale)
        if 0.0 < seconds <= _MAX_PLAUSIBLE_SECONDS:
            return round(seconds, 3)
    return None


def find_mvhd_duration(fetch_range, *, head_bytes: int = 96 * 1024,
                       tail_bytes: int = 512 * 1024) -> Optional[float]:
    """Locate a duration using ranged reads.

    `fetch_range(start, length)` should return up to `length` bytes from `start`, or
    None; a negative `start` means "from the end of the file", matching HTTP's
    suffix-range form. Returns None when neither end carries a readable `mvhd`, so
    callers keep whatever fallback they had.
    """
    try:
        head = fetch_range(0, head_bytes)
    except Exception:
        head = None
    if head:
        found = parse_mvhd_duration(head)
        if found is not None:
            return found
    # Not a faststart file: `moov` is after the media data.
    try:
        tail = fetch_range(-tail_bytes, tail_bytes)
    except Exception:
        tail = None
    if tail:
        return parse_mvhd_duration(tail)
    return None
