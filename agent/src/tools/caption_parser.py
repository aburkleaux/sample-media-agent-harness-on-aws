"""Caption Parser Tool for the Render Agent.

Parses VTT and SRT caption files into structured CaptionCue arrays
with speaker identification, timing validation, and format auto-detection.

Validates: Requirements 3.2, 3.5, 3.6, 3.9, 3.11, 8.4
"""

import json
import logging
import math
import re
from typing import Optional

import requests
from strands import tool

from security import url_guard

#: A caption file is text; anything bigger than this is not one.
MAX_CAPTION_BYTES = 10 * 1024 * 1024

logger = logging.getLogger(__name__)

# --- Timestamp parsing ---

# VTT: HH:MM:SS.mmm --> HH:MM:SS.mmm (hours optional, dot for ms)
_VTT_TIMESTAMP_RE = re.compile(
    r"(?:(\d{1,2}):)?(\d{2}):(\d{2})\.(\d{3})"
)

# SRT: HH:MM:SS,mmm --> HH:MM:SS,mmm (comma for ms)
_SRT_TIMESTAMP_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)

# VTT timing line: timestamp --> timestamp
_VTT_TIMING_RE = re.compile(
    r"(?:(\d{1,2}):)?(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(?:(\d{1,2}):)?(\d{2}):(\d{2})\.(\d{3})"
)

# SRT timing line: timestamp --> timestamp
_SRT_TIMING_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)

# VTT speaker tag: <v Speaker Name>text</v> or <v Speaker Name>text
_VTT_SPEAKER_RE = re.compile(r"<v\s+([^>]+)>(.*?)(?:</v>|$)", re.DOTALL)

# SRT/VTT speaker prefix: [Speaker]: text or Speaker: text
_BRACKET_SPEAKER_RE = re.compile(r"^\[([^\]]+)\]:\s*(.+)$", re.DOTALL)
_PREFIX_SPEAKER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 _-]{0,48}):\s+(.+)$", re.DOTALL)


def _parse_vtt_timestamp(h: Optional[str], m: str, s: str, ms: str) -> Optional[float]:
    """Convert VTT timestamp components to seconds."""
    try:
        hours = int(h) if h else 0
        minutes = int(m)
        seconds = int(s)
        millis = int(ms)
        result = hours * 3600 + minutes * 60 + seconds + millis / 1000.0
        if math.isnan(result) or math.isinf(result):
            return None
        return result
    except (ValueError, TypeError):
        return None


def _parse_srt_timestamp(h: str, m: str, s: str, ms: str) -> Optional[float]:
    """Convert SRT timestamp components to seconds."""
    try:
        hours = int(h)
        minutes = int(m)
        seconds = int(s)
        millis = int(ms)
        result = hours * 3600 + minutes * 60 + seconds + millis / 1000.0
        if math.isnan(result) or math.isinf(result):
            return None
        return result
    except (ValueError, TypeError):
        return None


def _extract_speaker_from_vtt_text(text: str) -> tuple[str, str]:
    """Extract speaker from VTT <v> tags. Returns (speaker, clean_text)."""
    match = _VTT_SPEAKER_RE.search(text)
    if match:
        speaker = match.group(1).strip()
        clean_text = match.group(2).strip()
        return speaker, clean_text
    return "", text


def _extract_speaker_from_prefix(text: str) -> tuple[str, str]:
    """Extract speaker from [Speaker]: or Speaker: prefix. Returns (speaker, clean_text)."""
    # Try bracketed format first: [Speaker]: text
    match = _BRACKET_SPEAKER_RE.match(text)
    if match:
        return match.group(1).strip(), match.group(2).strip()

    # Try prefix format: Speaker: text
    match = _PREFIX_SPEAKER_RE.match(text)
    if match:
        return match.group(1).strip(), match.group(2).strip()

    return "", text


def _detect_format(content: str) -> str:
    """Auto-detect caption format from content.

    - If content starts with 'WEBVTT' → 'vtt'
    - If first non-empty lines match SRT pattern (numeric index, timestamp with comma) → 'srt'
    - Default to 'vtt'
    """
    stripped = content.strip()

    if stripped.startswith("WEBVTT"):
        return "vtt"

    # Check for SRT pattern: first non-empty line is a number,
    # followed by a timestamp line with commas
    lines = stripped.split("\n")
    non_empty = [line.strip() for line in lines if line.strip()]
    if len(non_empty) >= 2:
        # First line should be a numeric index
        if non_empty[0].isdigit():
            # Second line should contain SRT-style timestamp (with comma)
            if _SRT_TIMING_RE.search(non_empty[1]):
                return "srt"

    return "vtt"


def _parse_vtt_content(content: str) -> tuple[list[dict], list[dict]]:
    """Parse VTT content into cues and warnings."""
    cues = []
    warnings = []
    cue_index = 0

    lines = content.split("\n")
    i = 0

    # Skip WEBVTT header and any metadata
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("WEBVTT"):
            i += 1
            # Skip header lines until first blank line
            while i < len(lines) and lines[i].strip():
                i += 1
            break
        i += 1

    # Parse cues
    while i < len(lines):
        line = lines[i].strip()

        # Skip empty lines
        if not line:
            i += 1
            continue

        # Check if this is a timing line
        timing_match = _VTT_TIMING_RE.search(line)

        if not timing_match:
            # Could be a cue identifier (optional in VTT), skip it
            i += 1
            # Check next line for timing
            if i < len(lines):
                timing_match = _VTT_TIMING_RE.search(lines[i].strip())
                if timing_match:
                    line = lines[i].strip()
                    i += 1
                else:
                    continue
            else:
                continue
        else:
            i += 1

        if timing_match:
            # Parse timestamps
            start = _parse_vtt_timestamp(
                timing_match.group(1), timing_match.group(2),
                timing_match.group(3), timing_match.group(4)
            )
            end = _parse_vtt_timestamp(
                timing_match.group(5), timing_match.group(6),
                timing_match.group(7), timing_match.group(8)
            )

            # Handle malformed timestamps
            if start is None or end is None:
                warnings.append({
                    "code": "MALFORMED_TIMESTAMP",
                    "message": f"Cue at index {cue_index} has malformed timestamp, skipping",
                    "index": cue_index,
                })
                cue_index += 1
                # Skip text lines until next blank line
                while i < len(lines) and lines[i].strip():
                    i += 1
                continue

            # Collect text lines until blank line
            text_lines = []
            while i < len(lines) and lines[i].strip():
                text_lines.append(lines[i].strip())
                i += 1

            full_text = " ".join(text_lines)

            # Extract speaker from <v> tag
            speaker, clean_text = _extract_speaker_from_vtt_text(full_text)

            # If no <v> tag, try prefix format
            if not speaker:
                speaker, clean_text = _extract_speaker_from_prefix(clean_text)

            cues.append({
                "start": start,
                "end": end,
                "speaker": speaker,
                "text": clean_text,
            })
            cue_index += 1

    return cues, warnings


def _parse_srt_content(content: str) -> tuple[list[dict], list[dict]]:
    """Parse SRT content into cues and warnings."""
    cues = []
    warnings = []
    cue_index = 0

    # Split into blocks separated by blank lines
    blocks = re.split(r"\n\s*\n", content.strip())

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        lines = block.split("\n")

        # SRT block format:
        # 1. Numeric index
        # 2. Timestamp line
        # 3+ Text lines

        # Skip the numeric index line
        line_idx = 0
        if lines[line_idx].strip().isdigit():
            line_idx += 1

        if line_idx >= len(lines):
            continue

        # Parse timing line
        timing_match = _SRT_TIMING_RE.search(lines[line_idx].strip())
        if not timing_match:
            # Not a valid SRT block
            continue

        start = _parse_srt_timestamp(
            timing_match.group(1), timing_match.group(2),
            timing_match.group(3), timing_match.group(4)
        )
        end = _parse_srt_timestamp(
            timing_match.group(5), timing_match.group(6),
            timing_match.group(7), timing_match.group(8)
        )

        # Handle malformed timestamps
        if start is None or end is None:
            warnings.append({
                "code": "MALFORMED_TIMESTAMP",
                "message": f"Cue at index {cue_index} has malformed timestamp, skipping",
                "index": cue_index,
            })
            cue_index += 1
            continue

        line_idx += 1

        # Collect text lines
        text_lines = []
        while line_idx < len(lines):
            text_lines.append(lines[line_idx].strip())
            line_idx += 1

        full_text = " ".join(text_lines)

        # Extract speaker from [Speaker]: or Speaker: prefix
        speaker, clean_text = _extract_speaker_from_prefix(full_text)

        cues.append({
            "start": start,
            "end": end,
            "speaker": speaker,
            "text": clean_text,
        })
        cue_index += 1

    return cues, warnings


def _validate_cues(
    cues: list[dict], warnings: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Validate cues: start < end, non-empty text, non-empty speaker.

    Invalid cues are discarded and added to warnings.
    Cues without explicit speaker get 'Speaker 1' assigned.
    """
    valid_cues = []

    for idx, cue in enumerate(cues):
        start = cue["start"]
        end = cue["end"]
        speaker = cue["speaker"]
        text = cue["text"]

        # Check start < end
        if start >= end:
            warnings.append({
                "code": "INVALID_TIMING",
                "message": f"Cue at index {idx} has start ({start}) >= end ({end}), discarding",
                "index": idx,
            })
            continue

        # Check non-empty text
        if not text or not text.strip():
            warnings.append({
                "code": "EMPTY_TEXT",
                "message": f"Cue at index {idx} has empty text, discarding",
                "index": idx,
            })
            continue

        # Assign default speaker if empty
        if not speaker or not speaker.strip():
            speaker = "Speaker 1"

        # After assignment, validate speaker is non-empty (should always be true now)
        if not speaker.strip():
            warnings.append({
                "code": "EMPTY_SPEAKER",
                "message": f"Cue at index {idx} has empty speaker, discarding",
                "index": idx,
            })
            continue

        valid_cues.append({
            "start": start,
            "end": end,
            "speaker": speaker.strip(),
            "text": text.strip(),
        })

    return valid_cues, warnings


def parse_caption_content(
    content: str, format: str = "auto"
) -> dict:
    """Core parsing logic — parses caption content and returns structured result.

    Args:
        content: Raw VTT or SRT text content
        format: 'vtt', 'srt', or 'auto' (detect from content)

    Returns:
        Dict with cues, warnings, and format_detected
    """
    # Detect format if auto
    if format == "auto":
        detected_format = _detect_format(content)
    else:
        detected_format = format

    # Parse based on format
    if detected_format == "vtt":
        cues, warnings = _parse_vtt_content(content)
    elif detected_format == "srt":
        cues, warnings = _parse_srt_content(content)
    else:
        return {
            "cues": [],
            "warnings": [{"code": "UNKNOWN_FORMAT", "message": f"Unknown format: {format}", "index": -1}],
            "format_detected": format,
        }

    # Validate cues
    valid_cues, warnings = _validate_cues(cues, warnings)

    return {
        "cues": valid_cues,
        "warnings": warnings,
        "format_detected": detected_format,
    }


@tool
def caption_parser(
    content: str = "",
    url: str = "",
    format: str = "auto",
) -> str:
    """Parse VTT/SRT caption files into structured CaptionCue array with speaker identification.

    Accepts either raw caption text content or a URL to fetch the caption file from.
    Supports VTT and SRT formats with auto-detection.

    Args:
        content: Raw VTT or SRT text content (provide this OR url)
        url: URL to fetch caption file from (provide this OR content)
        format: 'vtt', 'srt', or 'auto' (detect from content)

    Returns:
        JSON with cues array [{start, end, speaker, text}], warnings, and format_detected
    """
    # Fetch content from URL if not provided directly
    if not content and url:
        # Model-supplied url: through the shared SSRF guard (https, public addresses only).
        try:
            fetched = url_guard.fetch(url, max_bytes=MAX_CAPTION_BYTES, timeout=10.0,
                                      raise_for_status=False)
            if not (200 <= fetched.status < 300):
                return json.dumps({
                    "cues": [],
                    "warnings": [{
                        "code": "FETCH_ERROR",
                        "message": f"HTTP {fetched.status} fetching {url}",
                        "index": -1,
                    }],
                    "format_detected": "unknown",
                })
            content = fetched.text()
        except (requests.Timeout, TimeoutError):
            return json.dumps({
                "cues": [],
                "warnings": [{
                    "code": "FETCH_TIMEOUT",
                    "message": f"Timeout fetching {url}",
                    "index": -1,
                }],
                "format_detected": "unknown",
            })
        except Exception as e:
            return json.dumps({
                "cues": [],
                "warnings": [{
                    "code": "FETCH_ERROR",
                    "message": f"Error fetching {url}: {str(e)}",
                    "index": -1,
                }],
                "format_detected": "unknown",
            })

    if not content:
        return json.dumps({
            "cues": [],
            "warnings": [{"code": "NO_INPUT", "message": "No content or url provided", "index": -1}],
            "format_detected": "unknown",
        })

    result = parse_caption_content(content, format)
    return json.dumps(result)
